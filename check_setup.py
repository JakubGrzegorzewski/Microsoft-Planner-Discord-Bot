"""Check the Microsoft 365 side of the setup without starting the Discord bot.

    python check_setup.py

The script signs in exactly as the bot will, shows which Graph permissions the token
carries, and lists the plans, buckets, labels and group members the bot will work with.
It only reads; nothing in Planner is changed. Use it to find your plan ID too: set GROUP_ID
(and leave DEFAULT_PLAN_ID empty) to list every plan of the group with its ID.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from typing import Any

from auth import build_token_provider
from config import Config, ConfigError, load_config
from graph_client import GraphClient, GraphError, GraphForbidden
from matching import name_key
from planner_service import PlannerService, UserError

# Any one permission of a group is enough for the capability named on the left.
APP_PERMISSIONS = {
    "read and write Planner": ("Tasks.ReadWrite.All",),
    "list the group's members": (
        "GroupMember.Read.All",
        "Group.Read.All",
        "Group.ReadWrite.All",
        "Directory.Read.All",
    ),
    "read members' names and emails": ("User.ReadBasic.All", "User.Read.All", "Directory.Read.All"),
}
DELEGATED_PERMISSIONS = {
    "read and write Planner": ("Tasks.ReadWrite", "Group.ReadWrite.All"),
    "list the group's members": (
        "GroupMember.Read.All",
        "Group.Read.All",
        "Group.ReadWrite.All",
        "Directory.Read.All",
    ),
    "read members' names and emails": ("User.ReadBasic.All", "User.Read.All", "Directory.Read.All"),
}


def token_claims(token: str) -> dict[str, Any]:
    """Read the claims of an access token (for display only; nothing is trusted from them)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims if isinstance(claims, dict) else {}
    except (IndexError, ValueError):
        return {}


def granted_permissions(claims: dict[str, Any]) -> set[str]:
    roles = claims.get("roles") or []
    scopes = str(claims.get("scp") or "").split()
    return {*roles, *scopes}


class Report:
    def __init__(self) -> None:
        self.problems = 0
        self.warnings = 0

    def ok(self, text: str) -> None:
        print(f"  OK    {text}")

    def warn(self, text: str) -> None:
        self.warnings += 1
        print(f"  WARN  {text}")

    def fail(self, text: str) -> None:
        self.problems += 1
        print(f"  FAIL  {text}")


async def run(config: Config) -> int:
    report = Report()
    provider = build_token_provider(config)
    graph = GraphClient(provider)
    await graph.start()
    try:
        return await _check(config, provider, graph, report)
    finally:
        await graph.close()


async def _check(config: Config, provider: Any, graph: GraphClient, report: Report) -> int:
    print(f"Sign-in ({'app-only' if config.auth_mode == 'app' else 'delegated'})")
    try:
        token = await provider.get_token()
    except GraphError as exc:
        report.fail(str(exc))
        return _finish(report)
    report.ok("A Microsoft Graph token was issued")

    granted = granted_permissions(token_claims(token))
    if granted:
        print(f"        Permissions in the token: {', '.join(sorted(granted))}")
        expected = APP_PERMISSIONS if config.auth_mode == "app" else DELEGATED_PERMISSIONS
        for capability, alternatives in expected.items():
            if not granted & set(alternatives):
                report.warn(f"Nothing in the token allows the bot to {capability} (one of: {', '.join(alternatives)})")
    else:
        print("        (The token's permissions could not be read; the checks below show what works.)")

    planner = PlannerService(
        graph,
        default_plan_id=config.default_plan_id,
        tenant_id=config.tenant_id,
        group_id=config.group_id,
        default_bucket=config.default_bucket,
        task_url_template=config.task_url_template,
    )

    print("\nGroup and plans")
    try:
        group_id = await planner.group_id()
        plans = await planner.plans()
    except GraphForbidden as exc:
        hint = (
            "grant Tasks.ReadWrite.All and admin consent"
            if config.auth_mode == "app"
            else "the signed-in account must be a member of the group, and Tasks.ReadWrite must be consented"
        )
        report.fail(f"Graph refused access to Planner ({exc.message}); {hint}")
        return _finish(report)
    except (GraphError, UserError) as exc:
        report.fail(str(exc))
        return _finish(report)
    report.ok(f"Group {group_id} has {len(plans)} plan(s)")
    for plan in plans:
        marker = "  <- DEFAULT_PLAN_ID" if plan.id == config.default_plan_id else ""
        print(f"        {plan.id}  {plan.title}{marker}")
    if config.default_plan_id and all(plan.id != config.default_plan_id for plan in plans):
        report.fail("DEFAULT_PLAN_ID is not one of this group's plans")
    if not config.default_plan_id:
        report.warn("DEFAULT_PLAN_ID is not set yet. Copy one of the IDs above into .env")

    for plan in plans:
        print(f"\nPlan “{plan.title}”")
        try:
            buckets = await planner.buckets(plan.id)
            labels = await planner.labels(plan.id)
            tasks = await planner.tasks(plan.id)
        except GraphError as exc:
            report.fail(f"Could not read the plan: {exc}")
            continue
        done = sum(1 for task in tasks if int(task.get("percentComplete") or 0) >= 100)
        report.ok(f"{len(tasks)} task(s), {done} completed")
        print(f"        Buckets: {', '.join(bucket.name for bucket in buckets) or '(none)'}")
        named = ", ".join(f"{name} ({slot})" for slot, name in labels.names.items())
        print(f"        Labels:  {named or '(no named labels yet; /label create adds one)'}")
        is_default_plan = plan.id == config.default_plan_id
        if config.default_bucket and is_default_plan:
            wanted = name_key(config.default_bucket)
            if not any(wanted in (name_key(bucket.id), name_key(bucket.name)) for bucket in buckets):
                report.warn(f"DEFAULT_BUCKET “{config.default_bucket}” is not a bucket of the default plan")

    print("\nGroup members (people who can be assigned)")
    try:
        members = await planner.members()
    except GraphForbidden as exc:
        report.fail(
            f"Graph refused to list the group's members ({exc.message}); "
            "grant GroupMember.Read.All (or Group.Read.All) and admin consent"
        )
    except GraphError as exc:
        report.fail(str(exc))
    else:
        with_names = [member for member in members if member.display_name or member.email]
        if not members:
            report.warn("The group has no members that are users")
        elif not with_names:
            report.fail(
                "Members were returned without names or emails; grant User.ReadBasic.All "
                "(or User.Read.All) and admin consent so assignees can be found by name"
            )
        else:
            report.ok(f"{len(with_names)} member(s) with names")
            for member in with_names[:5]:
                print(f"        {member.display_name or '(no display name)'}  <{member.email or 'no email'}>")
            if len(with_names) > 5:
                print(f"        ... and {len(with_names) - 5} more")

    return _finish(report)


def _finish(report: Report) -> int:
    print()
    if report.problems:
        print(f"{report.problems} problem(s) to fix before the bot will work.")
        return 1
    if report.warnings:
        print(f"Working, with {report.warnings} warning(s) above.")
    else:
        print("Everything the bot needs from Microsoft 365 works.")
    return 0


def main() -> int:
    try:
        config = load_config(require_discord=False, require_plan=False)
        if not config.default_plan_id and not config.group_id:
            raise ConfigError("Set DEFAULT_PLAN_ID, or set GROUP_ID to list that group's plans and their IDs.")
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    try:
        return asyncio.run(run(config))
    except GraphError as exc:  # anything not handled above, e.g. a network failure
        print(f"  FAIL  {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
