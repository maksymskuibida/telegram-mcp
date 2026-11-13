"""The scope choice before a delete and before a group creation, written before the code (team
Testing Posture; the person's plan-page notes A-1 and A-4).

A delete with no ``for_everyone`` and a group with no ``group_kind`` are answered
``needs_choice``: nothing is prepared and nothing runs, and the hook's deny reason dictates the
choice question with the last choice first (marked "(last used)") or, with none yet, the less
destructive option first. Once the argument is given the usual question names the scope. Only an
Allowed run stores the choice, in ``choices.json`` (mode 0600) in the data folder; a missing or
broken file means no last choice."""

from __future__ import annotations

import json
import os
import stat
from typing import Any

import pytest

from support.brokers import FakeClock
from support.catalog import (
    BOB,
    CAROL,
    FIRST,
    answer,
    call,
    check,
    confirmed_run,
    question_text,
    write_calls,
)
from support.rpc import RawClient
from support.samples import ALICE
from telegram_mcp.broker.client import BrokerClient
from telegram_mcp.broker.server import Broker
from telegram_mcp.choices import CHOICES_NAME
from telegram_mcp.gateway.fake import FakeGateway
from telegram_mcp.hooks import approval_pre
from telegram_mcp.paths import FixedPaths

DELETE = "tg_messages_delete"
CREATE = "tg_chats_create"
DELETE_ARGS = {"chat": ALICE.id, "message_ids": [FIRST]}
GROUP_ARGS = {"type": "group", "title": "Trip", "members": [BOB, CAROL]}
DELETE_QUESTION = '"Delete these messages only for you or for everyone?"'
GROUP_QUESTION = '"Create a basic group or a supergroup?"'


def choices_file(paths: FixedPaths) -> Any:
    return paths.data_dir / CHOICES_NAME


def options_in(message: str) -> list[str]:
    """The option labels a needs_choice message dictates, in order."""
    part = message.split("options ", 1)[1].split(", in that order", 1)[0]
    return [label.strip().strip('"') for label in part.split(" and ")]


async def needs_choice(hook: RawClient, tool: str, args: dict[str, Any]) -> str:
    refused = await check(hook, tool, args)
    assert (refused["decision"], refused["code"]) == ("refuse", "needs_choice"), refused
    message: str = refused["message"]
    return message


async def test_a_delete_without_a_scope_asks_for_the_choice_first(
    approval_hook: RawClient, claude: RawClient, broker: Broker, world: FakeGateway
) -> None:
    message = await needs_choice(approval_hook, DELETE, DELETE_ARGS)
    assert DELETE_QUESTION in message
    assert options_in(message) == ["Only for you", "For everyone"]
    assert "for_everyone" in message
    assert "call this tool again" in message
    assert broker.gate.open_count() == 0
    refused = await call(claude, DELETE, DELETE_ARGS)
    assert refused["error"]["code"] == "needs_choice"
    assert write_calls(world, {"delete"}) == []


async def test_a_group_without_a_kind_asks_for_the_choice_first(
    approval_hook: RawClient, claude: RawClient, broker: Broker, world: FakeGateway
) -> None:
    message = await needs_choice(approval_hook, CREATE, GROUP_ARGS)
    assert GROUP_QUESTION in message
    assert options_in(message) == ["Basic group", "Supergroup"]
    assert "group_kind" in message
    assert broker.gate.open_count() == 0
    refused = await call(claude, CREATE, GROUP_ARGS)
    assert refused["error"]["code"] == "needs_choice"
    assert write_calls(world, {"create_group"}) == []


async def test_a_channel_needs_no_choice(approval_hook: RawClient) -> None:
    asked = await check(approval_hook, CREATE, {"type": "channel", "title": "News"})
    assert asked["decision"] == "ask"


async def test_a_kind_is_only_for_groups(approval_hook: RawClient) -> None:
    refused = await check(
        approval_hook, CREATE, {"type": "channel", "title": "News", "group_kind": "basic"}
    )
    assert (refused["decision"], refused["code"]) == ("refuse", "invalid_arguments")
    wrong = await check(approval_hook, CREATE, {**GROUP_ARGS, "group_kind": "mega"})
    assert wrong["code"] == "invalid_arguments"


async def test_the_hook_dictates_the_choice_question(broker: Broker, paths: FixedPaths) -> None:
    client = BrokerClient(paths, "approval-hook", spawn=lambda: None)
    try:
        hook_input = {
            "tool_name": f"mcp__plugin_telegram-mcp_telegram__{DELETE}",
            "tool_input": DELETE_ARGS,
        }
        answer_ = await approval_pre(json.dumps(hook_input), client)
    finally:
        await client.close()
    output = answer_["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    reason = output["permissionDecisionReason"]
    assert reason.startswith("needs_choice: ")
    assert "AskUserQuestion" in reason
    assert DELETE_QUESTION in reason


async def test_with_the_scope_given_the_question_names_it(approval_hook: RawClient) -> None:
    everyone = await check(approval_hook, DELETE, {**DELETE_ARGS, "for_everyone": True})
    assert question_text(everyone).startswith(
        "Delete 1 message(s) in «Alice» (user, id 101) for everyone?"
    )
    basic = await check(approval_hook, CREATE, {**GROUP_ARGS, "group_kind": "basic"})
    assert question_text(basic) == (
        "Create a new basic group «Trip»?\nMembers:\n«Bob» (id 501)\n«Carol» (id 502)"
    )
    supergroup = await check(approval_hook, CREATE, {**GROUP_ARGS, "group_kind": "supergroup"})
    assert question_text(supergroup).startswith("Create a new supergroup «Trip»?")


async def test_group_members_are_named_in_one_batched_call(
    approval_hook: RawClient, world: FakeGateway
) -> None:
    world.calls.clear()
    await check(approval_hook, CREATE, {**GROUP_ARGS, "group_kind": "basic"})
    assert [args for name, args in world.calls if name == "user_profile"] == [
        {"users": [BOB, CAROL]}
    ]


async def test_an_allowed_delete_is_remembered_and_offered_first_next_time(
    approval_hook: RawClient, claude: RawClient, world: FakeGateway, paths: FixedPaths
) -> None:
    done = await confirmed_run(approval_hook, claude, DELETE, {**DELETE_ARGS, "for_everyone": True})
    assert done["ok"] is True
    assert write_calls(world, {"delete"}) == [
        ("delete", {"chat_id": ALICE.id, "message_ids": [FIRST], "for_everyone": True})
    ]
    stored = choices_file(paths)
    assert json.loads(stored.read_text()) == {"delete_scope": "everyone"}
    assert stat.S_IMODE(os.stat(stored).st_mode) == 0o600
    message = await needs_choice(approval_hook, DELETE, {"chat": ALICE.id, "message_ids": [2]})
    assert options_in(message) == ["For everyone (last used)", "Only for you"]


async def test_an_allowed_group_creation_is_remembered_with_its_kind(
    approval_hook: RawClient, claude: RawClient, world: FakeGateway, paths: FixedPaths
) -> None:
    await confirmed_run(approval_hook, claude, DELETE, {**DELETE_ARGS, "for_everyone": False})
    done = await confirmed_run(approval_hook, claude, CREATE, {**GROUP_ARGS, "group_kind": "basic"})
    assert done["data"]["type"] == "group"
    assert write_calls(world, {"create_group"}) == [
        ("create_group", {"title": "Trip", "members": [BOB, CAROL], "supergroup": False})
    ]
    assert json.loads(choices_file(paths).read_text()) == {
        "delete_scope": "me",
        "group_kind": "basic",
    }
    message = await needs_choice(approval_hook, CREATE, {**GROUP_ARGS, "title": "Trip 2"})
    assert options_in(message) == ["Basic group (last used)", "Supergroup"]
    await confirmed_run(
        approval_hook, claude, CREATE, {**GROUP_ARGS, "title": "Big", "group_kind": "supergroup"}
    )
    assert json.loads(choices_file(paths).read_text())["group_kind"] == "supergroup"
    message = await needs_choice(approval_hook, CREATE, {**GROUP_ARGS, "title": "Trip 3"})
    assert options_in(message) == ["Supergroup (last used)", "Basic group"]


async def test_a_denied_or_expired_action_stores_nothing(
    approval_hook: RawClient, claude: RawClient, paths: FixedPaths, clock: FakeClock
) -> None:
    asked = await check(approval_hook, DELETE, {**DELETE_ARGS, "for_everyone": True})
    assert await answer(approval_hook, asked, "Deny") == "declined"
    assert (await call(claude, DELETE, {**DELETE_ARGS, "for_everyone": True}))["ok"] is False
    expiring = await check(approval_hook, CREATE, {**GROUP_ARGS, "group_kind": "supergroup"})
    assert expiring["decision"] == "ask"
    clock.advance(121)
    done = await call(claude, CREATE, {**GROUP_ARGS, "group_kind": "supergroup"})
    assert done["ok"] is False
    assert not choices_file(paths).exists()


@pytest.mark.parametrize(
    "content",
    ["{not json", '{"delete_scope": "bogus"}', '["everyone"]', ""],
)
async def test_a_broken_choices_file_means_no_last_choice(
    approval_hook: RawClient, paths: FixedPaths, content: str
) -> None:
    choices_file(paths).write_text(content)
    message = await needs_choice(approval_hook, DELETE, DELETE_ARGS)
    assert options_in(message) == ["Only for you", "For everyone"]


async def test_an_unreadable_choices_file_means_no_last_choice(
    approval_hook: RawClient, paths: FixedPaths
) -> None:
    stored = choices_file(paths)
    stored.write_text('{"group_kind": "supergroup"}')
    os.chmod(stored, 0)
    try:
        message = await needs_choice(approval_hook, CREATE, GROUP_ARGS)
    finally:
        os.chmod(stored, 0o600)
    assert options_in(message) == ["Basic group", "Supergroup"]


async def test_ask_again_does_not_change_the_choice(approval_hook: RawClient) -> None:
    plain = await needs_choice(approval_hook, DELETE, DELETE_ARGS)
    again = await needs_choice(approval_hook, DELETE, {**DELETE_ARGS, "ask_again": True})
    assert plain == again


def test_the_descriptions_say_a_choice_question_may_come_first() -> None:
    from telegram_mcp.tools import TOOL_SPECS

    specs = {spec.name: spec for spec in TOOL_SPECS}
    for tool in (DELETE, CREATE):
        assert "choice question may come first" in specs[tool].description
    assert "default" not in specs[DELETE].input_schema["properties"]["for_everyone"]
    assert specs[CREATE].input_schema["properties"]["group_kind"] == {
        "enum": ["basic", "supergroup"],
        "description": "For a group: a basic group or a supergroup; leave it out to ask.",
    }
