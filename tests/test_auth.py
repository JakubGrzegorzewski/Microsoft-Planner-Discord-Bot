"""Token providers. MSAL itself is replaced by small fakes, so no sign-in service is contacted."""

from __future__ import annotations

import asyncio
import json
import os
import stat

import pytest

import auth
from config import load_config
from graph_client import GraphAuthError
from helpers import async_test
from test_config import VALID

TENANT = "99999999-8888-4777-8666-555555555555"
CLIENT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class FakeCache:
    """Follows msal.SerializableTokenCache: (de)serialising clears has_state_changed."""

    def __init__(self) -> None:
        self.data: dict = {}
        self.has_state_changed = False

    def deserialize(self, state: str) -> None:
        self.data = json.loads(state) if state else {}
        self.has_state_changed = False

    def serialize(self) -> str:
        self.has_state_changed = False
        return json.dumps(self.data)


class FakeConfidentialApp:
    created: list["FakeConfidentialApp"] = []
    results: list = []

    def __init__(self, client_id, authority=None, client_credential=None):
        self.client_id, self.authority, self.client_credential = client_id, authority, client_credential
        FakeConfidentialApp.created.append(self)

    def acquire_token_for_client(self, scopes):
        self.scopes = scopes
        result = FakeConfidentialApp.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class FakePublicApp:
    created: list["FakePublicApp"] = []
    silent_results: list = []
    device_flow: dict = {"user_code": "ABC123", "message": "Open https://microsoft.com/devicelogin and enter ABC123"}

    def __init__(self, client_id, authority=None, token_cache=None):
        self.client_id, self.authority, self.cache = client_id, authority, token_cache
        self.calls: list[tuple] = []
        FakePublicApp.created.append(self)

    def get_accounts(self):
        return [{"username": name} for name in self.cache.data.get("accounts", [])]

    def acquire_token_silent_with_error(self, scopes, account=None, force_refresh=False):
        self.calls.append(("silent", tuple(scopes), account["username"], force_refresh))
        result = FakePublicApp.silent_results.pop(0)
        if isinstance(result, dict) and "access_token" in result:
            self.cache.data["rotations"] = self.cache.data.get("rotations", 0) + 1  # a new refresh token arrived
            self.cache.has_state_changed = True
        return result

    def initiate_device_flow(self, scopes=None):
        self.calls.append(("device", tuple(scopes)))
        return dict(FakePublicApp.device_flow)

    def acquire_token_by_device_flow(self, flow):
        return self._signed_in()

    def acquire_token_interactive(self, scopes, prompt=None):
        self.calls.append(("browser", tuple(scopes), prompt))
        return self._signed_in()

    def _signed_in(self):
        self.cache.data["accounts"] = ["planner-bot@contoso.com"]
        self.cache.has_state_changed = True
        return {
            "access_token": "user-token",
            "expires_in": 3600,
            "id_token_claims": {"preferred_username": "planner-bot@contoso.com"},
        }


@pytest.fixture(autouse=True)
def fake_msal(monkeypatch):
    FakeConfidentialApp.created, FakeConfidentialApp.results = [], []
    FakePublicApp.created, FakePublicApp.silent_results = [], []
    monkeypatch.setattr(auth.msal, "ConfidentialClientApplication", FakeConfidentialApp)
    monkeypatch.setattr(auth.msal, "PublicClientApplication", FakePublicApp)
    monkeypatch.setattr(auth.msal, "SerializableTokenCache", FakeCache)


def token(value: str, expires_in: int = 3600) -> dict:
    return {"access_token": value, "expires_in": expires_in, "token_type": "Bearer"}


# --------------------------------------------------------------------------- app-only


@async_test
async def test_app_token_is_requested_once_and_reused():
    FakeConfidentialApp.results = [token("t1")]
    provider = auth.AppTokenProvider(TENANT, CLIENT, "secret")

    assert [await provider.get_token() for _ in range(3)] == ["t1", "t1", "t1"]

    app = FakeConfidentialApp.created[0]
    assert len(FakeConfidentialApp.created) == 1
    assert app.authority == f"https://login.microsoftonline.com/{TENANT}"
    assert (app.client_id, app.client_credential) == (CLIENT, "secret")
    assert app.scopes == ["https://graph.microsoft.com/.default"]


@async_test
async def test_app_token_is_renewed_before_it_expires():
    FakeConfidentialApp.results = [token("short-lived", expires_in=200), token("t2")]
    provider = auth.AppTokenProvider(TENANT, CLIENT, "secret")
    assert await provider.get_token() == "short-lived"  # under five minutes left: not reused
    assert await provider.get_token() == "t2"
    assert await provider.get_token() == "t2"


@async_test
async def test_forced_refresh_starts_from_a_new_msal_application():
    FakeConfidentialApp.results = [token("t1"), token("t2")]
    provider = auth.AppTokenProvider(TENANT, CLIENT, "secret")
    assert await provider.get_token() == "t1"
    provider._acquired_at -= 60  # the token has been in use for a while when Graph rejects it

    assert await provider.get_token(force_refresh=True) == "t2"
    assert len(FakeConfidentialApp.created) == 2


@async_test
async def test_a_burst_of_401s_causes_one_refresh_not_many():
    FakeConfidentialApp.results = [token("t1"), token("t2")]
    provider = auth.AppTokenProvider(TENANT, CLIENT, "secret")
    await provider.get_token()
    provider._acquired_at -= 60

    tokens = await asyncio.gather(*(provider.get_token(force_refresh=True) for _ in range(5)))

    assert set(tokens) == {"t2"} and FakeConfidentialApp.results == []


@async_test
async def test_concurrent_first_requests_share_one_sign_in():
    FakeConfidentialApp.results = [token("t1")]
    provider = auth.AppTokenProvider(TENANT, CLIENT, "secret")
    assert set(await asyncio.gather(*(provider.get_token() for _ in range(5)))) == {"t1"}


@async_test
async def test_sign_in_errors_are_explained():
    provider = auth.AppTokenProvider(TENANT, CLIENT, "wrong")

    FakeConfidentialApp.results = [
        {
            "error": "invalid_client",
            "error_codes": [7000215],
            "error_description": "AADSTS7000215: Invalid client secret provided.\r\nTrace ID: x",
        }
    ]
    with pytest.raises(auth.AuthError, match="CLIENT_SECRET is wrong") as wrong_secret:
        await provider.get_token()
    assert isinstance(wrong_secret.value, GraphAuthError)  # one family of errors for Graph callers

    FakeConfidentialApp.results = [
        {"error": "invalid_client", "error_codes": [7000222], "error_description": "expired"}
    ]
    with pytest.raises(auth.AuthError, match="client secret has expired"):
        await provider.get_token()

    FakeConfidentialApp.results = [
        {"error": "something_new", "error_codes": [1], "error_description": "AADSTS1: Details.\nTrace"}
    ]
    with pytest.raises(auth.AuthError, match="something_new: AADSTS1: Details."):
        await provider.get_token()

    FakeConfidentialApp.results = [OSError("Name or service not known")]
    with pytest.raises(auth.AuthError, match="Microsoft sign-in could not be completed"):
        await provider.get_token()

    FakeConfidentialApp.results = [None]
    with pytest.raises(auth.AuthError, match="Check TENANT_ID, CLIENT_ID and CLIENT_SECRET"):
        await provider.get_token()

    FakeConfidentialApp.results = [KeyError("a bug, not a sign-in problem")]
    with pytest.raises(KeyError):
        await provider.get_token()

    FakeConfidentialApp.results = [token("works-again")]
    assert await provider.get_token() == "works-again"


# --------------------------------------------------------------------------- delegated


def write_cache(path, accounts):
    path.write_text(json.dumps({"accounts": accounts}), encoding="utf-8")


@async_test
async def test_delegated_needs_a_login_first(tmp_path):
    provider = auth.DelegatedTokenProvider(TENANT, CLIENT, tmp_path / "cache.json")
    with pytest.raises(auth.AuthError, match="python auth.py login"):
        await provider.get_token()


@async_test
async def test_delegated_token_comes_from_the_saved_login_and_the_cache_is_written_back(tmp_path):
    path = tmp_path / "cache.json"
    write_cache(path, ["planner-bot@contoso.com"])
    FakePublicApp.silent_results = [token("u1")]
    provider = auth.DelegatedTokenProvider(TENANT, CLIENT, path)

    assert await provider.get_token() == "u1"
    assert await provider.get_token() == "u1"

    app = FakePublicApp.created[0]
    assert app.calls == [
        ("silent", ("Tasks.ReadWrite", "Group.Read.All", "User.ReadBasic.All"), "planner-bot@contoso.com", False)
    ]
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["rotations"] == 1  # the renewed refresh token reached the disk
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600  # readable by the bot's user only
    assert not (tmp_path / "cache.json.tmp").exists()


@async_test
async def test_delegated_picks_up_a_new_login_without_a_restart(tmp_path):
    path = tmp_path / "cache.json"
    write_cache(path, ["old-account@contoso.com"])
    FakePublicApp.silent_results = [token("u1", expires_in=10), token("u2")]
    provider = auth.DelegatedTokenProvider(TENANT, CLIENT, path)
    assert await provider.get_token() == "u1"

    write_cache(path, ["new-account@contoso.com"])  # someone ran `python auth.py login` again
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))

    assert await provider.get_token() == "u2"
    assert FakePublicApp.created[-1].calls[-1][2] == "new-account@contoso.com"


@async_test
async def test_delegated_errors_say_to_sign_in_again(tmp_path):
    path = tmp_path / "cache.json"
    write_cache(path, ["planner-bot@contoso.com"])
    provider = auth.DelegatedTokenProvider(TENANT, CLIENT, path)

    FakePublicApp.silent_results = [None]
    with pytest.raises(auth.AuthError, match="Run `python auth.py login` again"):
        await provider.get_token()

    FakePublicApp.silent_results = [{"error": "invalid_grant", "error_codes": [700082], "error_description": "expired"}]
    with pytest.raises(auth.AuthError, match="expired after a long period without use"):
        await provider.get_token()


@async_test
async def test_a_damaged_token_cache_is_reported_as_such(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text("{ not json", encoding="utf-8")
    provider = auth.DelegatedTokenProvider(TENANT, CLIENT, path)
    with pytest.raises(auth.AuthError, match="can't be read. Delete the file"):
        await provider.get_token()


@async_test
async def test_forced_refresh_is_passed_to_msal(tmp_path):
    path = tmp_path / "cache.json"
    write_cache(path, ["planner-bot@contoso.com"])
    FakePublicApp.silent_results = [token("u1"), token("u2")]
    provider = auth.DelegatedTokenProvider(TENANT, CLIENT, path)
    await provider.get_token()
    provider._acquired_at -= 60
    assert await provider.get_token(force_refresh=True) == "u2"
    assert FakePublicApp.created[0].calls[-1][3] is True


# --------------------------------------------------------------------------- login command


def delegated_config(tmp_path):
    return load_config({**VALID, "AUTH_MODE": "delegated", "DATA_DIR": str(tmp_path)})


def test_device_code_login_saves_the_cache(tmp_path, capsys):
    config = delegated_config(tmp_path)

    account = auth.login(config)

    assert account == "planner-bot@contoso.com"
    assert "enter ABC123" in capsys.readouterr().out
    assert json.loads(config.token_cache_path.read_text(encoding="utf-8"))["accounts"] == ["planner-bot@contoso.com"]

    FakePublicApp.silent_results = [token("u1")]
    assert auth.status(config) == "Signed in as planner-bot@contoso.com. A Graph token can be obtained."
    FakePublicApp.silent_results = [{"error": "invalid_grant", "error_codes": [50173], "error_description": "revoked"}]
    assert "no token could be obtained: The saved sign-in was revoked" in auth.status(config)


def test_browser_login(tmp_path):
    config = delegated_config(tmp_path)
    assert auth.login(config, use_browser=True) == "planner-bot@contoso.com"
    assert FakePublicApp.created[-1].calls == [
        ("browser", ("Tasks.ReadWrite", "Group.Read.All", "User.ReadBasic.All"), "select_account")
    ]


def test_login_explains_a_blocked_device_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(
        FakePublicApp,
        "device_flow",
        {"error": "unauthorized_client", "error_codes": [7000218], "error_description": "not allowed"},
    )
    with pytest.raises(auth.AuthError, match="Allow public client flows"):
        auth.login(delegated_config(tmp_path))


def test_status_and_provider_selection(tmp_path):
    delegated = delegated_config(tmp_path)
    assert "Nobody is signed in" in auth.status(delegated)
    assert isinstance(auth.build_token_provider(delegated), auth.DelegatedTokenProvider)

    app_only = load_config(VALID)
    assert "client secret" in auth.status(app_only)
    assert isinstance(auth.build_token_provider(app_only), auth.AppTokenProvider)


def test_command_line(tmp_path, monkeypatch, capsys):
    for key in list(os.environ):
        if key in VALID or key in ("AUTH_MODE", "DATA_DIR", "GROUP_ID"):
            monkeypatch.delenv(key)
    for key, value in {**VALID, "AUTH_MODE": "delegated", "DATA_DIR": str(tmp_path)}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(auth, "load_config", lambda **options: load_config(dict(os.environ), **options))

    assert auth.main(["login"]) == 0
    assert "Signed in as planner-bot@contoso.com" in capsys.readouterr().out
    FakePublicApp.silent_results = [token("u1")]
    assert auth.main(["status"]) == 0

    monkeypatch.setenv("TENANT_ID", "not-a-guid")
    assert auth.main(["status"]) == 1
    assert "TENANT_ID must be" in capsys.readouterr().err
