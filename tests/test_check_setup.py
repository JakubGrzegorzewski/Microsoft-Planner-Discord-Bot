"""check_setup.py: what the setup check reports for a working and for a broken configuration."""

from __future__ import annotations

import base64
import json

import check_setup
from config import load_config
from helpers import async_test, make_world
from test_config import VALID


def fake_jwt(**claims) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


class Tokens:
    def __init__(self, world, token: str) -> None:
        world.graph.valid_tokens.add(token)
        self._token = token

    async def get_token(self, *, force_refresh: bool = False) -> str:
        return self._token


async def run_check(world, token: str, **settings) -> tuple[int, check_setup.Report]:
    config = load_config({**VALID, "DEFAULT_PLAN_ID": world.plan_id, **settings}, require_discord=False)
    report = check_setup.Report()
    world.client._tokens = Tokens(world, token)
    return await check_setup._check(config, world.client._tokens, world.client, report), report


@async_test
async def test_a_working_setup_is_reported_in_full(capsys):
    world = make_world()
    world.graph.add_task(world.plan_id, "Open task")
    world.graph.add_task(world.plan_id, "Done task", percent=100)
    token = fake_jwt(roles=["Tasks.ReadWrite.All", "GroupMember.Read.All", "User.ReadBasic.All"])

    code, report = await run_check(world, token)

    output = capsys.readouterr().out
    assert code == 0 and (report.problems, report.warnings) == (0, 0)
    assert "Permissions in the token: GroupMember.Read.All, Tasks.ReadWrite.All, User.ReadBasic.All" in output
    assert f"{world.plan_id}  Sprint Board  <- DEFAULT_PLAN_ID" in output
    assert "2 task(s), 1 completed" in output
    assert "Buckets: To do, In progress, Done" in output
    assert "Labels:  Bug (category1), Frontend (category3), Needs review, urgent (category5)" in output
    assert "3 member(s) with names" in output and "Anna Kowalska  <anna.kowalska@contoso.com>" in output
    assert "Everything the bot needs from Microsoft 365 works." in output
    assert all(request.method == "GET" for request in world.graph.requests)  # the check only reads


@async_test
async def test_missing_permissions_and_nameless_members_are_pointed_out(capsys):
    world = make_world()
    for member in world.graph.members:
        member.update(displayName=None, mail=None, userPrincipalName=None)

    code, report = await run_check(world, fake_jwt(roles=["Tasks.ReadWrite.All", "GroupMember.Read.All"]))

    output = capsys.readouterr().out
    assert code == 1 and report.problems == 1 and report.warnings == 1
    assert "WARN  Nothing in the token allows the bot to read members' names and emails" in output
    assert "FAIL  Members were returned without names or emails; grant User.ReadBasic.All" in output
    assert "1 problem(s) to fix before the bot will work." in output


@async_test
async def test_refused_access_and_wrong_ids_are_explained(capsys):
    world = make_world()
    world.graph.fail_next(
        403, path=r"/planner/plans", body={"error": {"code": "", "message": "Insufficient privileges."}}
    )
    code, _ = await run_check(world, fake_jwt(roles=[]))
    output = capsys.readouterr().out
    assert code == 1
    assert (
        "FAIL  Graph refused access to Planner (Insufficient privileges.); grant Tasks.ReadWrite.All and admin consent"
        in output
    )

    wrong_plan = make_world()
    code, _ = await run_check(
        wrong_plan, fake_jwt(roles=["Tasks.ReadWrite.All"]), DEFAULT_PLAN_ID="doesNotExist0000000000000000"
    )
    assert code == 1 and "DEFAULT_PLAN_ID wasn't found" in capsys.readouterr().out


@async_test
async def test_listing_plans_by_group_when_the_plan_id_is_not_known_yet(capsys):
    world = make_world()
    config = load_config(
        {key: value for key, value in VALID.items() if key != "DEFAULT_PLAN_ID"} | {"GROUP_ID": world.graph.group_id},
        require_discord=False,
        require_plan=False,
    )
    tokens = Tokens(world, "opaque-token")
    world.client._tokens = tokens

    code = await check_setup._check(config, tokens, world.client, check_setup.Report())

    output = capsys.readouterr().out
    assert code == 0
    assert "(The token's permissions could not be read" in output
    assert f"{world.plan_id}  Sprint Board" in output and "Marketing" in output
    assert "WARN  DEFAULT_PLAN_ID is not set yet. Copy one of the IDs above into .env" in output


@async_test
async def test_a_default_plan_from_another_group_is_flagged(capsys):
    world = make_world()
    elsewhere = world.graph.add_plan("Finance", group_id="33333333-aaaa-4bbb-8ccc-444444444444")

    code, _ = await run_check(
        world, fake_jwt(roles=["Tasks.ReadWrite.All"]), DEFAULT_PLAN_ID=elsewhere, GROUP_ID=world.graph.group_id
    )

    assert code == 1
    assert "FAIL  DEFAULT_PLAN_ID is not one of this group's plans" in capsys.readouterr().out


def test_token_claims_never_raise():
    assert check_setup.token_claims("not-a-jwt") == {}
    assert check_setup.token_claims("a.!!!.c") == {}
    assert check_setup.granted_permissions({"roles": ["A"], "scp": "B C"}) == {"A", "B", "C"}
