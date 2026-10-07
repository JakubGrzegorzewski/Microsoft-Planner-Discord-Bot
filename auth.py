"""Access tokens for Microsoft Graph, obtained with MSAL.

Two ways of signing in are supported, chosen with AUTH_MODE:

* ``app`` (default): the bot signs in as itself with the client secret (client credentials
  flow). Needs *application* permissions with admin consent. Nothing to log in to and no
  refresh token that can expire.
* ``delegated``: the bot acts as one signed-in user (a service account). Somebody signs in
  once with ``python auth.py login``; MSAL's token cache, including the refresh token, is
  kept in DATA_DIR and renewed silently from then on.

MSAL is a blocking library, so every call into it runs in a worker thread.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional, Sequence

import msal

from config import Config, ConfigError, load_config
from graph_client import GraphAuthError

log = logging.getLogger(__name__)

APP_SCOPES = ["https://graph.microsoft.com/.default"]
# Delegated permissions the bot asks for. MSAL adds offline_access (the refresh token) itself.
DELEGATED_SCOPES = ["Tasks.ReadWrite", "Group.Read.All", "User.ReadBasic.All"]

# Tokens are replaced this many seconds before they expire.
REFRESH_MARGIN = 300.0

# What the most common Entra error codes mean for whoever runs the bot.
_AADSTS_HINTS = {
    7000215: "CLIENT_SECRET is wrong. Copy the secret's Value (not its Secret ID) from the app registration.",
    7000222: "The client secret has expired. Create a new one in the app registration and update CLIENT_SECRET.",
    700016: "CLIENT_ID was not found in this tenant. Check CLIENT_ID and TENANT_ID.",
    90002: "TENANT_ID was not found. Use the Directory (tenant) ID of your organisation.",
    65001: "Consent has not been granted. Grant admin consent for the app's Graph permissions.",
    7000218: "Enable 'Allow public client flows' under Authentication in the app registration.",
    700082: "The saved sign-in expired after a long period without use. Run `python auth.py login` again.",
    50173: "The saved sign-in was revoked (for example by a password change). Run `python auth.py login` again.",
    50076: "The account must complete multi-factor authentication again. Run `python auth.py login`.",
    50079: "The account must set up multi-factor authentication first. Then run `python auth.py login`.",
    53003: "A Conditional Access policy blocked the sign-in. Ask your Entra administrator to allow it.",
}


class AuthError(GraphAuthError):
    """A token could not be obtained. The message says what to fix.

    It is a GraphAuthError so that callers of the Graph client have one family of
    exceptions to handle, whether Graph or the sign-in in front of it failed.
    """


def authority_for(tenant_id: str) -> str:
    return f"https://login.microsoftonline.com/{tenant_id}"


def _explain(result: Optional[dict[str, Any]], fallback: str) -> str:
    """Turn an MSAL error result into one actionable sentence."""
    if not result:
        return fallback
    for code in result.get("error_codes") or []:
        if code in _AADSTS_HINTS:
            return _AADSTS_HINTS[code]
    description = (result.get("error_description") or "").strip().splitlines()
    first_line = description[0] if description else ""
    return f"{result.get('error', 'error')}: {first_line}" if first_line else fallback


class _TokenProvider:
    """Caches the current access token and renews it shortly before it expires."""

    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._expires_at = 0.0
        self._acquired_at = float("-inf")
        self._lock = asyncio.Lock()

    async def get_token(self, *, force_refresh: bool = False) -> str:
        if not force_refresh and self._is_usable():
            return self._token  # type: ignore[return-value]
        async with self._lock:
            now = time.monotonic()
            # Another request may have renewed the token while this one waited for the lock.
            just_renewed = now - self._acquired_at < 5.0
            if self._is_usable() and (not force_refresh or just_renewed):
                return self._token  # type: ignore[return-value]
            try:
                result = await asyncio.to_thread(self._acquire, force_refresh)
            except (OSError, ValueError) as exc:
                # requests' network errors are OSErrors; MSAL raises ValueError when it
                # cannot validate the tenant. Anything else is a bug and should surface as one.
                raise AuthError(f"Microsoft sign-in could not be completed ({exc.__class__.__name__}: {exc})") from exc
            if not result or "access_token" not in result:
                raise AuthError(_explain(result, self._failure_hint()))
            self._token = result["access_token"]
            self._acquired_at = time.monotonic()
            self._expires_at = self._acquired_at + float(result.get("expires_in", 3600))
            return self._token

    def _is_usable(self) -> bool:
        return self._token is not None and time.monotonic() < self._expires_at - REFRESH_MARGIN

    def _acquire(self, force_refresh: bool) -> Optional[dict[str, Any]]:
        raise NotImplementedError

    def _failure_hint(self) -> str:
        return "Microsoft sign-in returned no token."


class AppTokenProvider(_TokenProvider):
    """App-only tokens (client credentials flow)."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str) -> None:
        super().__init__()
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._app: Any = None

    def _acquire(self, force_refresh: bool) -> Optional[dict[str, Any]]:
        # MSAL keeps app tokens in the application object's cache. Starting from a new
        # object is the dependable way to get a token Graph has not seen before.
        if self._app is None or force_refresh:
            self._app = msal.ConfidentialClientApplication(
                self._client_id,
                authority=authority_for(self._tenant_id),
                client_credential=self._client_secret,
            )
        return self._app.acquire_token_for_client(scopes=APP_SCOPES)

    def _failure_hint(self) -> str:
        return "App sign-in failed. Check TENANT_ID, CLIENT_ID and CLIENT_SECRET."


class TokenCacheFile:
    """MSAL's serialisable token cache, stored in a file only the bot's user can read."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.cache = msal.SerializableTokenCache()
        self._mtime: Optional[float] = None
        self._lock = threading.Lock()

    def load_if_changed(self) -> bool:
        """(Re)read the file if it changed on disk, e.g. after a new `auth.py login`."""
        with self._lock:
            try:
                mtime = self.path.stat().st_mtime
            except FileNotFoundError:
                return False
            if mtime == self._mtime:
                return False
            cache = msal.SerializableTokenCache()
            try:
                cache.deserialize(self.path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise AuthError(
                    f"The saved sign-in in {self.path} can't be read. Delete the file and run `python auth.py login`."
                ) from exc
            self.cache = cache
            self._mtime = mtime
            return True

    def save_if_changed(self) -> None:
        with self._lock:
            if not self.cache.has_state_changed:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            # Create the file private from the start: it holds a refresh token.
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(self.cache.serialize())  # serialize() also clears has_state_changed
            os.replace(temporary, self.path)
            self._mtime = self.path.stat().st_mtime


class DelegatedTokenProvider(_TokenProvider):
    """Tokens for the user who signed in with `python auth.py login`."""

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        cache_path: Path,
        scopes: Sequence[str] = DELEGATED_SCOPES,
    ) -> None:
        super().__init__()
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._scopes = list(scopes)
        self._cache_file = TokenCacheFile(cache_path)
        self._app: Any = None

    def _acquire(self, force_refresh: bool) -> Optional[dict[str, Any]]:
        if self._cache_file.load_if_changed() or self._app is None:
            self._app = msal.PublicClientApplication(
                self._client_id,
                authority=authority_for(self._tenant_id),
                token_cache=self._cache_file.cache,
            )
        accounts = self._app.get_accounts()
        if not accounts:
            raise AuthError("Nobody is signed in yet. Run `python auth.py login` once (see the README).")
        result = self._app.acquire_token_silent_with_error(
            self._scopes, account=accounts[0], force_refresh=force_refresh
        )
        self._cache_file.save_if_changed()
        return result

    def _failure_hint(self) -> str:
        return "The saved sign-in can no longer be used. Run `python auth.py login` again."


def build_token_provider(config: Config) -> _TokenProvider:
    if config.auth_mode == "delegated":
        return DelegatedTokenProvider(config.tenant_id, config.client_id, config.token_cache_path)
    if not config.client_secret:
        raise AuthError("CLIENT_SECRET is required when AUTH_MODE=app.")
    return AppTokenProvider(config.tenant_id, config.client_id, config.client_secret)


# --------------------------------------------------------------------------- command line


def login(config: Config, *, use_browser: bool = False) -> str:
    """One-time interactive sign-in for AUTH_MODE=delegated. Returns the account name."""
    cache_file = TokenCacheFile(config.token_cache_path)
    cache_file.load_if_changed()
    app = msal.PublicClientApplication(
        config.client_id, authority=authority_for(config.tenant_id), token_cache=cache_file.cache
    )
    if use_browser:
        # Opens the system browser; needs the redirect URI http://localhost on the app registration.
        result = app.acquire_token_interactive(DELEGATED_SCOPES, prompt="select_account")
    else:
        flow = app.initiate_device_flow(scopes=DELEGATED_SCOPES)
        if "user_code" not in flow:
            raise AuthError(_explain(flow, "Could not start the device-code sign-in."))
        print(flow["message"], flush=True)
        result = app.acquire_token_by_device_flow(flow)  # waits until the sign-in is completed
    if not result or "access_token" not in result:
        raise AuthError(_explain(result, "Sign-in did not complete."))
    cache_file.save_if_changed()
    claims = result.get("id_token_claims") or {}
    return claims.get("preferred_username") or claims.get("name") or "the selected account"


def status(config: Config) -> str:
    """Describe the saved sign-in without changing anything on Microsoft's side."""
    if config.auth_mode != "delegated":
        return "AUTH_MODE=app: the bot signs in with its client secret; there is no saved user sign-in."
    cache_file = TokenCacheFile(config.token_cache_path)
    cache_file.load_if_changed()
    app = msal.PublicClientApplication(
        config.client_id, authority=authority_for(config.tenant_id), token_cache=cache_file.cache
    )
    accounts = app.get_accounts()
    if not accounts:
        return f"Nobody is signed in (no usable token cache at {config.token_cache_path})."
    result = app.acquire_token_silent_with_error(DELEGATED_SCOPES, account=accounts[0])
    cache_file.save_if_changed()
    name = accounts[0].get("username", "unknown account")
    if result and "access_token" in result:
        return f"Signed in as {name}. A Graph token can be obtained."
    return f"Signed in as {name}, but no token could be obtained: {_explain(result, 'sign in again.')}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the bot's Microsoft 365 sign-in (AUTH_MODE=delegated).")
    commands = parser.add_subparsers(dest="command", required=True)
    login_parser = commands.add_parser("login", help="sign in once with the account the bot should act as")
    login_parser.add_argument(
        "--browser",
        action="store_true",
        help="sign in through a local browser instead of a device code (run this on a desktop PC)",
    )
    commands.add_parser("status", help="show which account is signed in")
    args = parser.parse_args(argv)

    try:
        config = load_config(require_discord=False, require_plan=False)
        if args.command == "login":
            account = login(config, use_browser=args.browser)
            print(f"Signed in as {account}. The token cache was saved to {config.token_cache_path}.")
            if config.auth_mode != "delegated":
                print("Note: AUTH_MODE is not 'delegated', so the bot will not use this sign-in yet.")
        else:
            print(status(config))
    except (ConfigError, AuthError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
