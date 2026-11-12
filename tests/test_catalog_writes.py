"""The 15 U4 write tools through the broker, written before the code (team Testing Posture).

Covers BR23.1 to BR23.8, BR21.4, BR20.4 and NFR2.1, NFR2.5, NFR2.6 and NFR3.1: a first call never
runs, each question is U2's question built from the fields BR23.1 lists, an Allow runs exactly
what the question showed (also when Telegram's titles and texts changed in between), a failing
read prepares nothing, the per-tool checks refuse before any question, and ask_again never
changes what is approved."""

from __future__ import annotations

from typing import Any

import pytest

from support.catalog import (
    BOB,
    CAROL,
    CLUB,
    FIRST,
    ME,
    MINE,
    TEAM_PINNED,
    WRITE_CALLS,
    answer,
    call,
    check,
    confirm,
    confirmed_run,
    put_file,
    question_text,
    write_calls,
)
from support.rpc import RawClient
from support.samples import ALICE, FAMILY, FORUM, TEAM
from telegram_mcp.broker.server import Broker
from telegram_mcp.gateway.fake import FakeGateway
from telegram_mcp.gateway.models import ChatRef, GatewayTimeout
from telegram_mcp.paths import FixedPaths

A = "«Alice» (user, id 101)"
F = "«Family» (group, id -202)"
T = "«Team» (supergroup, id -1000000000303)"
B = "«Builders» (forum, id -1000000000505)"
M1 = "#1 Alice, 2026-11-11 09:00 UTC:\nthe first message"
M5 = "#5 Bob, 2026-11-11 09:00 UTC:\nthe plan"

# tool, arguments, the question after its [tg-<id>] prefix, the Telegram write it runs.
WRITES: list[tuple[str, dict[str, Any], str, tuple[str, dict[str, Any]]]] = [
    (
        "tg_messages_edit",
        {"chat": ALICE.id, "message_id": MINE, "text": "fixed text"},
        f"Edit your message 2 in {A}?\nNow:\nmy own message\nNew:\nfixed text",
        ("edit", {"chat_id": ALICE.id, "message_id": MINE, "text": "fixed text"}),
    ),
    (
        "tg_messages_delete",
        {"chat": ALICE.id, "message_ids": [FIRST], "for_everyone": False},
        f"Delete 1 message(s) in {A} only for you?\n{M1}",
        ("delete", {"chat_id": ALICE.id, "message_ids": [FIRST], "for_everyone": False}),
    ),
    (
        "tg_messages_forward",
        {"from_chat": ALICE.id, "message_ids": [FIRST], "to_chat": FAMILY.id},
        f"Forward 1 message(s) from {A} to {F}?\n{M1}",
        ("forward", {"from_chat": ALICE.id, "message_ids": [FIRST], "to_chat": FAMILY.id}),
    ),
    (
        "tg_messages_react",
        {"chat": ALICE.id, "message_id": FIRST, "emoji": "👍"},
        f"React 👍 to message 1 in {A}?\n{M1}",
        ("react", {"chat_id": ALICE.id, "message_id": FIRST, "emoji": "👍"}),
    ),
    (
        "tg_messages_pin",
        {"chat": ALICE.id, "message_id": FIRST},
        f"Pin message 1 in {A}?\n{M1}",
        ("pin", {"chat_id": ALICE.id, "message_id": FIRST}),
    ),
    (
        "tg_messages_unpin",
        {"chat": TEAM.id, "message_id": TEAM_PINNED},
        f"Unpin message 5 in {T}?\n{M5}",
        ("unpin", {"chat_id": TEAM.id, "message_id": TEAM_PINNED}),
    ),
    (
        "tg_topics_create",
        {"chat": FORUM.id, "title": "Plans"},
        f"Create the topic «Plans» in {B}?",
        ("topic_create", {"chat_id": FORUM.id, "title": "Plans"}),
    ),
    (
        "tg_chats_create",
        {"type": "channel", "title": "Trip news"},
        "Create a new channel «Trip news»?\nMembers: no other members",
        ("create_channel", {"title": "Trip news"}),
    ),
    (
        "tg_chats_join",
        {"link_or_username": "@open_club"},
        "Join «Open Club» (supergroup, id -1000000000606)?",
        ("join", {"username": "open_club", "invite_hash": None}),
    ),
    (
        "tg_chats_leave",
        {"chat": FAMILY.id},
        f"Leave {F}?",
        ("leave", {"chat_id": FAMILY.id}),
    ),
    (
        "tg_chats_edit",
        {"chat": FAMILY.id, "title": "Family 2026", "description": "Our family"},
        f"Change {F}?\ntitle: Family → Family 2026\ndescription: (none) → Our family",
        (
            "edit_chat",
            {
                "chat_id": FAMILY.id,
                "title": "Family 2026",
                "description": "Our family",
                "photo_name": None,
            },
        ),
    ),
    (
        "tg_invites_create",
        {"chat": FAMILY.id, "usage_limit": 3},
        f"Create an invite link for {F}?\nExpires: never. Usage limit: 3.",
        ("invite_link", {"chat_id": FAMILY.id, "expire_seconds": None, "usage_limit": 3}),
    ),
    (
        "tg_profile_edit",
        {"bio": "New bio"},
        "Change your own profile?\nbio: (none) → New bio",
        (
            "edit_own_profile",
            {
                "first_name": None,
                "last_name": None,
                "bio": "New bio",
                "username": None,
                "photo_name": None,
            },
        ),
    ),
]
TOOLS = [entry[0] for entry in WRITES]


@pytest.mark.parametrize(("tool", "args", "question", "write"), WRITES, ids=TOOLS)
async def test_a_first_call_never_runs(
    claude: RawClient,
    world: FakeGateway,
    tool: str,
    args: dict[str, Any],
    question: str,
    write: Any,
) -> None:
    refused = await call(claude, tool, args)
    assert refused["ok"] is False
    assert refused["error"]["code"] == "needs_confirmation"
    assert write_calls(world, WRITE_CALLS) == []


@pytest.mark.parametrize(("tool", "args", "question", "write"), WRITES, ids=TOOLS)
async def test_the_check_asks_the_exact_question(
    approval_hook: RawClient, tool: str, args: dict[str, Any], question: str, write: Any
) -> None:
    asked = await check(approval_hook, tool, args)
    assert asked["decision"] == "ask", asked
    assert question_text(asked) == question
    assert asked["options"] == ["Allow", "Deny"]


@pytest.mark.parametrize(("tool", "args", "question", "write"), WRITES, ids=TOOLS)
async def test_an_allow_runs_exactly_what_was_asked_once(
    approval_hook: RawClient,
    claude: RawClient,
    world: FakeGateway,
    tool: str,
    args: dict[str, Any],
    question: str,
    write: tuple[str, dict[str, Any]],
) -> None:
    done = await confirmed_run(approval_hook, claude, tool, args)
    assert done["ok"] is True, done
    assert write_calls(world, WRITE_CALLS) == [write]
    again = await call(claude, tool, args)
    assert again["error"]["code"] == "needs_confirmation"
    assert len(write_calls(world, WRITE_CALLS)) == 1
    assert world.read_acknowledgements() == []


async def test_the_admin_question_names_the_user_and_the_changed_rights(
    approval_hook: RawClient, claude: RawClient, world: FakeGateway
) -> None:
    args = {"chat": TEAM.id, "user": CAROL, "rights": {"pin_messages": True}}
    asked = await check(approval_hook, "tg_admins_set", args)
    text = question_text(asked)
    assert text.startswith(f"Change the admin rights of «Carol» (id {CAROL}) in {T}?\n")
    assert "pin_messages: on (changes)" in text.splitlines()
    assert "add_admins: off" in text.splitlines()
    assert await answer(approval_hook, asked) == "confirmed"
    done = await call(claude, "tg_admins_set", args)
    assert done["ok"] is True
    ((name, sent),) = write_calls(world, WRITE_CALLS)
    assert name == "set_admin_rights"
    assert sent["user_id"] == CAROL
    assert sent["rights"]["pin_messages"] is True
    assert sum(sent["rights"].values()) == 1


async def test_the_admin_question_shows_the_current_rights(
    approval_hook: RawClient,
) -> None:
    args = {"chat": TEAM.id, "user": BOB, "rights": {"pin_messages": True}}
    text = question_text(await check(approval_hook, "tg_admins_set", args))
    assert "pin_messages: on" in text.splitlines()
    assert "invite_users: off (changes)" in text.splitlines()


async def test_a_join_by_invite_link_names_the_link(
    approval_hook: RawClient, claude: RawClient, world: FakeGateway
) -> None:
    world.invite_targets["AbCdEf"] = CLUB.id
    args = {"link_or_username": "https://t.me/+AbCdEf"}
    asked = await check(approval_hook, "tg_chats_join", args)
    assert question_text(asked) == "Join the chat at https://t.me/+AbCdEf?"
    await confirm(approval_hook, "tg_chats_join", args)
    done = await call(claude, "tg_chats_join", args)
    assert done["ok"] is True
    assert write_calls(world, {"join"}) == [("join", {"username": None, "invite_hash": "AbCdEf"})]


async def test_a_file_send_shows_the_resolved_file_and_sends_it(
    approval_hook: RawClient, claude: RawClient, world: FakeGateway, paths: FixedPaths
) -> None:
    path = put_file(paths, "report.pdf", b"report")
    args = {"chat": ALICE.id, "path": str(path), "caption": "Q3"}
    asked = await check(approval_hook, "tg_files_send", args)
    assert question_text(asked) == (
        f"Send this file to {A}?\nFile: {path}\nName: report.pdf, 0.0 KB (6 bytes)\nQ3"
    )
    await confirm(approval_hook, "tg_files_send", args)
    done = await call(claude, "tg_files_send", args)
    assert done["ok"] is True, done
    assert [(f.chat_id, f.name, f.caption, f.data) for f in world.sent_files] == [
        (ALICE.id, "report.pdf", "Q3", b"report")
    ]


async def test_an_edit_sends_the_approved_text_after_telegram_changed(
    approval_hook: RawClient, claude: RawClient, world: FakeGateway
) -> None:
    args = {"chat": ALICE.id, "message_id": MINE, "text": "fixed text"}
    await confirm(approval_hook, "tg_messages_edit", args)
    world.chats[0] = ChatRef(id=ALICE.id, name="Mallory", type="user")
    await world.edit(ALICE.id, MINE, "changed meanwhile")
    world.calls.clear()
    done = await call(claude, "tg_messages_edit", args)
    assert done["ok"] is True
    assert write_calls(world, WRITE_CALLS) == [
        ("edit", {"chat_id": ALICE.id, "message_id": MINE, "text": "fixed text"})
    ]


async def test_a_forward_runs_against_the_approved_chats_by_id(
    approval_hook: RawClient, claude: RawClient, world: FakeGateway
) -> None:
    args = {"from_chat": ALICE.id, "message_ids": [FIRST], "to_chat": FAMILY.id}
    await confirm(approval_hook, "tg_messages_forward", args)
    world.chats = [ChatRef(id=c.id, name="renamed", type=c.type) for c in world.chats]
    world.calls.clear()
    done = await call(claude, "tg_messages_forward", args)
    assert done["ok"] is True
    # No new lookup at run time: only the broker's session check and the forward itself.
    assert [name for name, _ in world.calls if name != "is_authorized"] == ["forward"]


DESCRIBER_READS: list[tuple[str, dict[str, Any], str]] = [
    ("tg_messages_edit", {"chat": ALICE.id, "message_id": MINE, "text": "x"}, "read_messages"),
    (
        "tg_messages_delete",
        {"chat": ALICE.id, "message_ids": [FIRST], "for_everyone": True},
        "read_messages",
    ),
    (
        "tg_messages_forward",
        {"from_chat": ALICE.id, "message_ids": [FIRST], "to_chat": FAMILY.id},
        "read_messages",
    ),
    ("tg_messages_react", {"chat": ALICE.id, "message_id": FIRST, "emoji": "👍"}, "read_messages"),
    ("tg_messages_pin", {"chat": ALICE.id, "message_id": FIRST}, "read_messages"),
    ("tg_messages_unpin", {"chat": ALICE.id, "message_id": FIRST}, "read_messages"),
    ("tg_topics_create", {"chat": FORUM.id, "title": "x"}, "resolve_chat"),
    ("tg_chats_join", {"link_or_username": "open_club"}, "chat_info"),
    ("tg_chats_leave", {"chat": FAMILY.id}, "resolve_chat"),
    ("tg_chats_edit", {"chat": FAMILY.id, "title": "x"}, "chat_info"),
    ("tg_invites_create", {"chat": FAMILY.id}, "resolve_chat"),
    ("tg_admins_set", {"chat": TEAM.id, "user": CAROL, "rights": {}}, "user_profile"),
    ("tg_profile_edit", {"first_name": "x"}, "own_profile"),
    ("tg_files_send", {"chat": ALICE.id, "path": "report.pdf"}, "resolve_chat"),
]


@pytest.mark.parametrize(
    ("tool", "args", "read"), DESCRIBER_READS, ids=[e[0] for e in DESCRIBER_READS]
)
async def test_a_failing_read_refuses_and_prepares_nothing(
    approval_hook: RawClient,
    broker: Broker,
    world: FakeGateway,
    paths: FixedPaths,
    tool: str,
    args: dict[str, Any],
    read: str,
) -> None:
    put_file(paths, "report.pdf")
    world.script(read, error=GatewayTimeout(), times=None)
    refused = await check(approval_hook, tool, args)
    assert refused["decision"] == "refuse", refused
    assert refused["code"] == "telegram_timeout"
    assert broker.gate.open_count() == 0
    assert write_calls(world, WRITE_CALLS) == []


REFUSED: list[tuple[str, dict[str, Any], str]] = [
    # BR23.3
    ("tg_messages_edit", {"chat": ALICE.id, "message_id": FIRST, "text": "x"}, "not_allowed"),
    (
        "tg_messages_edit",
        {"chat": ALICE.id, "message_id": MINE, "text": "my own message"},
        "invalid_arguments",
    ),
    ("tg_messages_edit", {"chat": ALICE.id, "message_id": MINE, "text": ""}, "invalid_arguments"),
    (
        "tg_messages_edit",
        {"chat": ALICE.id, "message_id": MINE, "text": "x" * 4097},
        "invalid_arguments",
    ),
    ("tg_messages_edit", {"chat": ALICE.id, "message_id": 99, "text": "x"}, "not_found"),
    # BR23.4, BR20.4
    (
        "tg_messages_delete",
        {"chat": ALICE.id, "message_ids": list(range(1, 22)), "for_everyone": True},
        "invalid_arguments",
    ),
    (
        "tg_messages_delete",
        {"chat": ALICE.id, "message_ids": [1, 1], "for_everyone": True},
        "invalid_arguments",
    ),
    (
        "tg_messages_delete",
        {"chat": ALICE.id, "message_ids": [], "for_everyone": True},
        "invalid_arguments",
    ),
    (
        "tg_messages_delete",
        {"chat": ALICE.id, "message_ids": [1, 77], "for_everyone": True},
        "not_found",
    ),
    (
        "tg_messages_forward",
        {"from_chat": ALICE.id, "message_ids": [4], "to_chat": FAMILY.id},
        "not_found",
    ),
    (
        "tg_messages_forward",
        {"from_chat": "Alice", "message_ids": [1], "to_chat": FAMILY.id},
        "invalid_arguments",
    ),
    # BR23.5
    (
        "tg_messages_react",
        {"chat": ALICE.id, "message_id": FIRST, "emoji": "x" * 17},
        "invalid_arguments",
    ),
    ("tg_topics_create", {"chat": FAMILY.id, "title": "x"}, "invalid_arguments"),
    ("tg_topics_create", {"chat": FORUM.id, "title": "x" * 129}, "invalid_arguments"),
    # BR23.6
    ("tg_chats_create", {"type": "secret", "title": "x"}, "invalid_arguments"),
    ("tg_chats_create", {"type": "channel", "title": ""}, "invalid_arguments"),
    ("tg_chats_leave", {"chat": ALICE.id}, "invalid_arguments"),
    ("tg_chats_leave", {"chat": ME}, "invalid_arguments"),
    ("tg_chats_edit", {"chat": FAMILY.id}, "invalid_arguments"),
    ("tg_chats_edit", {"chat": ALICE.id, "title": "x"}, "invalid_arguments"),
    ("tg_invites_create", {"chat": ALICE.id}, "invalid_arguments"),
    ("tg_admins_set", {"chat": FAMILY.id, "user": CAROL, "rights": {}}, "invalid_arguments"),
    (
        "tg_admins_set",
        {"chat": TEAM.id, "user": CAROL, "rights": {"rule_world": True}},
        "invalid_arguments",
    ),
    ("tg_admins_set", {"chat": TEAM.id, "user": 9999, "rights": {}}, "not_found"),
    ("tg_profile_edit", {}, "invalid_arguments"),
    ("tg_profile_edit", {"username": "ab"}, "invalid_arguments"),
    ("tg_profile_edit", {"first_name": ""}, "invalid_arguments"),
    ("tg_profile_edit", {"bio": "x" * 256}, "invalid_arguments"),
    # BR23.7
    ("tg_chats_join", {"link_or_username": "not a link!"}, "invalid_arguments"),
    ("tg_chats_join", {"link_or_username": "@team_chat"}, "invalid_arguments"),
    ("tg_chats_join", {"link_or_username": "@nobody_here"}, "not_found"),
    # BR23.8
    ("tg_invites_create", {"chat": FAMILY.id, "expire_seconds": 59}, "invalid_arguments"),
    ("tg_invites_create", {"chat": FAMILY.id, "usage_limit": 100000}, "invalid_arguments"),
    # BR20.4: closed schemas
    ("tg_chats_leave", {"chat": FAMILY.id, "extra": 1}, "invalid_arguments"),
    ("tg_messages_pin", {"chat": ALICE.id, "message_id": 1.0}, "invalid_arguments"),
    # BR21.1
    ("tg_chats_leave", {"chat": 999}, "not_found"),
    # BR25.1
    ("tg_files_send", {"chat": ALICE.id, "path": "/etc/hosts"}, "not_allowed"),
]


@pytest.mark.parametrize(("tool", "args", "code"), REFUSED, ids=[e[0] for e in REFUSED])
async def test_per_tool_checks_refuse_before_any_question(
    approval_hook: RawClient,
    broker: Broker,
    tool: str,
    args: dict[str, Any],
    code: str,
) -> None:
    refused = await check(approval_hook, tool, args)
    assert refused["decision"] == "refuse", refused
    assert refused["code"] == code, refused
    assert broker.gate.open_count() == 0


async def test_ask_again_is_accepted_and_does_not_change_the_digest(
    approval_hook: RawClient, claude: RawClient, world: FakeGateway
) -> None:
    args = {"chat": FAMILY.id}
    asked = await check(approval_hook, "tg_chats_leave", {**args, "ask_again": True})
    assert asked["decision"] == "ask"
    again = await check(approval_hook, "tg_chats_leave", args)
    assert again["approval_id"] == asked["approval_id"]
    await answer(approval_hook, asked)
    done = await call(claude, "tg_chats_leave", {**args, "ask_again": True})
    assert done["ok"] is True
    assert write_calls(world, {"leave"}) == [("leave", {"chat_id": FAMILY.id})]


async def test_every_write_result_names_what_changed(
    approval_hook: RawClient, claude: RawClient
) -> None:
    created = await confirmed_run(
        approval_hook, claude, "tg_invites_create", {"chat": FAMILY.id, "expire_seconds": 3600}
    )
    assert created["data"]["link"].startswith("https://t.me/+")
    assert created["data"]["chat_id"] == FAMILY.id
    topic = await confirmed_run(
        approval_hook, claude, "tg_topics_create", {"chat": FORUM.id, "title": "New"}
    )
    assert topic["data"] == {"chat_id": FORUM.id, "topic_id": 10, "title": "New"}
    deleted = await confirmed_run(
        approval_hook,
        claude,
        "tg_messages_delete",
        {"chat": ALICE.id, "message_ids": [FIRST], "for_everyone": True},
    )
    assert deleted["data"] == {"chat_id": ALICE.id, "deleted": [FIRST], "for_everyone": True}
