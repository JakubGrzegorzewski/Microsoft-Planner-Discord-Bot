"""Runtime configuration.

Everything comes from environment variables. For local runs they are read from a `.env`
file next to this module (copy `.env.example`); in Docker, Compose passes the same file in
with `env_file`. Nothing secret is hard-coded anywhere in the project.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from dotenv import load_dotenv

GUID_RE = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
PLANNER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{28}$")

# Graph has no "web link" field for Planner tasks, so the link is built from the URL the
# Planner web app uses. If Microsoft changes that format, override TASK_URL_TEMPLATE.
DEFAULT_TASK_URL_TEMPLATE = (
    "https://planner.cloud.microsoft/webui/plan/{plan_id}/view/board/task/{task_id}?tid={tenant_id}"
)

AUTH_MODES = ("app", "delegated")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
MIN_POLL_INTERVAL = 15
MIN_CACHE_TTL = 10


class ConfigError(Exception):
    """The configuration is unusable. The message lists every problem found."""


@dataclass(frozen=True)
class Config:
    # Secrets are excluded from repr() so they can never end up in a log line.
    discord_token: str = field(repr=False)
    client_secret: Optional[str] = field(repr=False)
    tenant_id: str
    client_id: str
    auth_mode: str
    default_plan_id: str
    group_id: Optional[str]
    default_bucket: Optional[str]
    notify_channel_id: int
    guild_id: Optional[int]
    allowed_role_ids: frozenset[int]
    poll_interval: int
    cache_ttl: int
    data_dir: Path
    log_level: str
    task_url_template: str
    warnings: tuple[str, ...] = ()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "planner_bot.sqlite3"

    @property
    def token_cache_path(self) -> Path:
        return self.data_dir / "msal_token_cache.json"

    @property
    def heartbeat_path(self) -> Path:
        return self.data_dir / "heartbeat"


def _clean(value: Optional[str]) -> str:
    """Trim whitespace and one pair of surrounding quotes (some .env loaders keep them)."""
    if value is None:
        return ""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def load_config(
    env: Optional[Mapping[str, str]] = None,
    *,
    require_discord: bool = True,
    require_plan: bool = True,
) -> Config:
    """Read and validate the configuration.

    `require_discord=False` is for the helper scripts that only talk to Microsoft Graph.
    """
    if env is None:
        # Real environment variables win over the file, so Docker/systemd settings are respected.
        load_dotenv(Path(__file__).resolve().parent / ".env")
        env = os.environ

    problems: list[str] = []
    warnings: list[str] = []

    def get(name: str) -> str:
        return _clean(env.get(name))

    def required(name: str, hint: str) -> str:
        value = get(name)
        if not value:
            problems.append(f"{name} is missing ({hint})")
        return value

    def snowflake(name: str, value: str) -> Optional[int]:
        if not value:
            return None
        if not value.isdigit():
            problems.append(f"{name} must be a numeric Discord ID, got {value!r}")
            return None
        return int(value)

    def integer(name: str, default: int, minimum: int) -> int:
        raw = get(name)
        if not raw:
            return default
        try:
            number = int(raw)
        except ValueError:
            problems.append(f"{name} must be a whole number of seconds, got {raw!r}")
            return default
        if number < minimum:
            problems.append(f"{name} must be at least {minimum} seconds, got {number}")
            return default
        return number

    # --- Microsoft Entra / Graph ------------------------------------------------------
    tenant_id = required("TENANT_ID", "Directory (tenant) ID of the app registration")
    if tenant_id and not GUID_RE.match(tenant_id):
        problems.append(
            "TENANT_ID must be the Directory (tenant) ID, a GUID such as 11111111-2222-3333-4444-555555555555"
        )

    client_id = required("CLIENT_ID", "Application (client) ID of the app registration")
    if client_id and not GUID_RE.match(client_id):
        problems.append("CLIENT_ID must be the Application (client) ID, a GUID")

    auth_mode = (get("AUTH_MODE") or "app").lower()
    if auth_mode not in AUTH_MODES:
        problems.append(f"AUTH_MODE must be one of {', '.join(AUTH_MODES)}, got {auth_mode!r}")

    client_secret = get("CLIENT_SECRET") or None
    if auth_mode == "app" and not client_secret:
        problems.append("CLIENT_SECRET is missing (the Value of a client secret; required when AUTH_MODE=app)")

    # --- Planner ----------------------------------------------------------------------
    default_plan_id = get("DEFAULT_PLAN_ID")
    if require_plan and not default_plan_id:
        problems.append("DEFAULT_PLAN_ID is missing (the ID of the plan used when a command names none)")
    if default_plan_id:
        if re.search(r"[\s/:?]", default_plan_id):
            problems.append("DEFAULT_PLAN_ID must be only the plan ID, not a URL")
        elif GUID_RE.match(default_plan_id):
            warnings.append(
                "DEFAULT_PLAN_ID looks like a GUID, which is how Premium plans are identified. "
                "The Graph Planner API only reaches basic plans (28-character IDs)."
            )
        elif not PLANNER_ID_RE.match(default_plan_id):
            warnings.append("DEFAULT_PLAN_ID doesn't look like a Planner plan ID (28 letters, digits, '-' or '_').")

    group_id = get("GROUP_ID") or None
    if group_id and not GUID_RE.match(group_id):
        problems.append("GROUP_ID must be the Microsoft 365 group's Object ID, a GUID")

    default_bucket = get("DEFAULT_BUCKET") or None

    task_url_template = get("TASK_URL_TEMPLATE") or DEFAULT_TASK_URL_TEMPLATE
    try:
        task_url_template.format(plan_id="p", task_id="t", tenant_id="x")
    except (KeyError, IndexError, ValueError):
        problems.append("TASK_URL_TEMPLATE may only use the placeholders {plan_id}, {task_id} and {tenant_id}")

    # --- Discord ----------------------------------------------------------------------
    discord_token = get("DISCORD_TOKEN")
    notify_channel_id: Optional[int] = None
    if require_discord:
        if not discord_token:
            problems.append("DISCORD_TOKEN is missing (the bot token from the Discord developer portal)")
        channel_raw = required("NOTIFY_CHANNEL_ID", "ID of the channel for completion notifications")
        notify_channel_id = snowflake("NOTIFY_CHANNEL_ID", channel_raw)
    else:
        notify_channel_id = snowflake("NOTIFY_CHANNEL_ID", get("NOTIFY_CHANNEL_ID"))

    guild_id = snowflake("DISCORD_GUILD_ID", get("DISCORD_GUILD_ID"))

    role_ids: set[int] = set()
    for part in re.split(r"[,\s]+", get("ALLOWED_ROLE_IDS")):
        if not part:
            continue
        if part.isdigit():
            role_ids.add(int(part))
        else:
            problems.append(f"ALLOWED_ROLE_IDS must be a comma-separated list of role IDs, got {part!r}")
    if require_discord and not role_ids:
        warnings.append(
            "ALLOWED_ROLE_IDS is empty, so nobody can use the commands yet. "
            "List the role IDs that may use the bot (the server's own ID stands for @everyone)."
        )

    # --- Behaviour --------------------------------------------------------------------
    poll_interval = integer("POLL_INTERVAL_SECONDS", 60, MIN_POLL_INTERVAL)
    cache_ttl = integer("CACHE_TTL_SECONDS", 300, MIN_CACHE_TTL)
    data_dir = Path(get("DATA_DIR") or "data").expanduser()

    log_level = (get("LOG_LEVEL") or "INFO").upper()
    if log_level not in LOG_LEVELS:
        problems.append(f"LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}, got {log_level!r}")

    if problems:
        raise ConfigError("Configuration problems:\n" + "\n".join(f"  - {p}" for p in problems))

    return Config(
        discord_token=discord_token,
        client_secret=client_secret,
        tenant_id=tenant_id.lower(),
        client_id=client_id.lower(),
        auth_mode=auth_mode,
        default_plan_id=default_plan_id,
        group_id=group_id.lower() if group_id else None,
        default_bucket=default_bucket,
        notify_channel_id=notify_channel_id or 0,
        guild_id=guild_id,
        allowed_role_ids=frozenset(role_ids),
        poll_interval=poll_interval,
        cache_ttl=cache_ttl,
        data_dir=data_dir,
        log_level=log_level,
        task_url_template=task_url_template,
        warnings=tuple(warnings),
    )
