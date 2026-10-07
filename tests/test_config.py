"""Configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from config import DEFAULT_TASK_URL_TEMPLATE, ConfigError, load_config

VALID = {
    "DISCORD_TOKEN": "discord-secret-token",
    "TENANT_ID": "99999999-8888-4777-8666-555555555555",
    "CLIENT_ID": "AAAAAAAA-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    "CLIENT_SECRET": "graph-secret~value",
    "DEFAULT_PLAN_ID": "xqQg5FS2LkCp935s-FIFm2QAFkHM",
    "NOTIFY_CHANNEL_ID": "123456789012345678",
    "ALLOWED_ROLE_IDS": "111, 222",
}


def env(**changes: str | None) -> dict[str, str]:
    values = dict(VALID)
    for key, value in changes.items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
    return values


def test_minimal_configuration_and_defaults():
    config = load_config(VALID)
    assert config.auth_mode == "app"
    assert config.client_id == "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    assert config.default_plan_id == "xqQg5FS2LkCp935s-FIFm2QAFkHM"
    assert config.notify_channel_id == 123456789012345678
    assert config.allowed_role_ids == frozenset({111, 222})
    assert (config.poll_interval, config.cache_ttl, config.log_level) == (60, 300, "INFO")
    assert config.group_id is None and config.guild_id is None and config.default_bucket is None
    assert config.task_url_template == DEFAULT_TASK_URL_TEMPLATE
    assert config.data_dir == Path("data")
    assert config.db_path == Path("data/planner_bot.sqlite3")
    assert config.token_cache_path == Path("data/msal_token_cache.json")
    assert config.warnings == ()


def test_secrets_never_appear_in_repr():
    text = repr(load_config(VALID))
    assert "discord-secret-token" not in text and "graph-secret" not in text
    assert "tenant_id" in text


def test_every_problem_is_reported_at_once():
    with pytest.raises(ConfigError) as caught:
        load_config(
            {"TENANT_ID": "contoso.onmicrosoft.com", "NOTIFY_CHANNEL_ID": "#general", "POLL_INTERVAL_SECONDS": "fast"}
        )
    message = str(caught.value)
    for expected in (
        "TENANT_ID must be the Directory (tenant) ID",
        "CLIENT_ID is missing",
        "CLIENT_SECRET is missing",
        "DEFAULT_PLAN_ID is missing",
        "DISCORD_TOKEN is missing",
        "NOTIFY_CHANNEL_ID must be a numeric Discord ID",
        "POLL_INTERVAL_SECONDS must be a whole number",
    ):
        assert expected in message


def test_optional_settings():
    config = load_config(
        env(
            GROUP_ID="11111111-AAAA-4BBB-8CCC-222222222222",
            DISCORD_GUILD_ID="42",
            DEFAULT_BUCKET="To do",
            POLL_INTERVAL_SECONDS="120",
            CACHE_TTL_SECONDS="30",
            DATA_DIR="/data",
            LOG_LEVEL="debug",
            TASK_URL_TEMPLATE="https://example.test/{plan_id}/{task_id}",
        )
    )
    assert config.group_id == "11111111-aaaa-4bbb-8ccc-222222222222"
    assert (config.guild_id, config.default_bucket, config.poll_interval, config.cache_ttl) == (42, "To do", 120, 30)
    assert config.data_dir == Path("/data") and config.log_level == "DEBUG"
    assert config.heartbeat_path == Path("/data/heartbeat")


def test_values_are_trimmed_and_unquoted():
    config = load_config(
        env(DISCORD_TOKEN='  "quoted-token"  ', DEFAULT_BUCKET="'To do'", ALLOWED_ROLE_IDS=" 5 ,6  7 ")
    )
    assert config.discord_token == "quoted-token"
    assert config.default_bucket == "To do"
    assert config.allowed_role_ids == frozenset({5, 6, 7})


def test_delegated_mode_needs_no_client_secret():
    config = load_config(env(AUTH_MODE="Delegated", CLIENT_SECRET=None))
    assert config.auth_mode == "delegated" and config.client_secret is None
    with pytest.raises(ConfigError, match="AUTH_MODE must be one of"):
        load_config(env(AUTH_MODE="certificate"))


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        (
            {"DEFAULT_PLAN_ID": "https://planner.cloud.microsoft/webui/plan/xqQg5FS2LkCp935s-FIFm2QAFkHM/view/board"},
            "not a URL",
        ),
        ({"ALLOWED_ROLE_IDS": "111, @admins"}, "ALLOWED_ROLE_IDS must be a comma-separated list of role IDs"),
        ({"POLL_INTERVAL_SECONDS": "5"}, "POLL_INTERVAL_SECONDS must be at least 15"),
        ({"CACHE_TTL_SECONDS": "0"}, "CACHE_TTL_SECONDS must be at least 10"),
        ({"GROUP_ID": "my-team"}, "GROUP_ID must be"),
        ({"LOG_LEVEL": "LOUD"}, "LOG_LEVEL must be one of"),
        ({"TASK_URL_TEMPLATE": "https://example.test/{plan}/{task_id}"}, "TASK_URL_TEMPLATE may only use"),
        ({"DISCORD_GUILD_ID": "my-server"}, "DISCORD_GUILD_ID must be a numeric Discord ID"),
    ],
)
def test_invalid_values(changes, expected):
    with pytest.raises(ConfigError, match=expected):
        load_config(env(**changes))


def test_warnings_for_things_that_start_but_will_not_work():
    premium = load_config(env(DEFAULT_PLAN_ID="ac152287-001b-4abc-a3b9-4517cd45935a"))
    assert any("Premium" in warning for warning in premium.warnings)

    odd = load_config(env(DEFAULT_PLAN_ID="short"))
    assert any("doesn't look like a Planner plan ID" in warning for warning in odd.warnings)

    nobody = load_config(env(ALLOWED_ROLE_IDS=None))
    assert nobody.allowed_role_ids == frozenset()
    assert any("nobody can use the commands" in warning for warning in nobody.warnings)


def test_helper_scripts_do_not_need_discord_settings():
    config = load_config(
        {
            "TENANT_ID": VALID["TENANT_ID"],
            "CLIENT_ID": VALID["CLIENT_ID"],
            "CLIENT_SECRET": "s",
            "GROUP_ID": "11111111-aaaa-4bbb-8ccc-222222222222",
        },
        require_discord=False,
        require_plan=False,
    )
    assert config.default_plan_id == "" and config.discord_token == "" and config.notify_channel_id == 0
    assert config.warnings == ()
