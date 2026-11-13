"""The two marking paths of U4 on U2's broker path, written before the code (team Testing
Posture). Covers BR24.1, BR24.2 and U2's BR13: nothing unread is allowed and marks nothing,
something unread asks once, and the mark never goes past the newest message counted or the
newest message tg_messages_read returned."""

from __future__ import annotations

import pytest

from support.catalog import answer, call, check, confirm, question_text
from support.rpc import RawClient
from support.samples import ALICE, FORUM
from telegram_mcp.gateway.fake import FakeGateway

MARK = "tg_chats_mark_read"
READ = "tg_messages_read"
A = "«Alice» (user, id 101)"


def two_unread(world: FakeGateway) -> list[int]:
    return [world.add_message(ALICE.id, f"new {n}", sender="Alice").id for n in (1, 2)]


async def test_a_mark_with_nothing_unread_runs_and_marks_nothing(
    approval_hook: RawClient, claude: RawClient, world: FakeGateway
) -> None:
    assert (await check(approval_hook, MARK, {"chat": ALICE.id}))["decision"] == "allow"
    done = await call(claude, MARK, {"chat": ALICE.id})
    assert done["data"] == {"chat_id": ALICE.id, "topic_id": None, "marked": 0, "up_to": None}
    assert world.read_acknowledgements() == []


async def test_a_mark_with_unread_messages_asks_once_and_marks_up_to_the_newest_counted(
    approval_hook: RawClient, claude: RawClient, world: FakeGateway
) -> None:
    ids = two_unread(world)
    asked = await check(approval_hook, MARK, {"chat": ALICE.id})
    assert question_text(asked) == f"Mark 2 unread message(s) in {A} as read?"
    assert await answer(approval_hook, asked) == "confirmed"
    world.add_message(ALICE.id, "arrived after the question")
    done = await call(claude, MARK, {"chat": ALICE.id})
    assert done["data"] == {"chat_id": ALICE.id, "topic_id": None, "marked": 2, "up_to": ids[-1]}
    assert world.read_acknowledgements() == [
        {"chat_id": ALICE.id, "max_id": ids[-1], "topic_id": None}
    ]


async def test_a_mark_with_no_approval_and_unread_messages_needs_confirmation(
    claude: RawClient, world: FakeGateway
) -> None:
    two_unread(world)
    refused = await call(claude, MARK, {"chat": ALICE.id})
    assert refused["error"]["code"] == "needs_confirmation"
    assert world.read_acknowledgements() == []


async def test_a_forum_is_marked_one_topic_at_a_time(
    approval_hook: RawClient, claude: RawClient, world: FakeGateway
) -> None:
    newest = world.add_message(FORUM.id, "new idea", topic_id=9).id
    refused = await check(approval_hook, MARK, {"chat": FORUM.id})
    assert (refused["decision"], refused["code"]) == ("refuse", "invalid_arguments")
    args = {"chat": FORUM.id, "topic_id": 9}
    asked = await confirm(approval_hook, MARK, args)
    assert question_text(asked) == (
        "Mark 1 unread message(s) in «Builders» (forum, id -1000000000505) in topic «Ideas» "
        "(id 9) as read?"
    )
    done = await call(claude, MARK, args)
    assert done["data"]["up_to"] == newest
    assert world.read_acknowledgements() == [{"chat_id": FORUM.id, "max_id": newest, "topic_id": 9}]


async def test_reading_without_mark_read_asks_nothing_and_marks_nothing(
    approval_hook: RawClient, claude: RawClient, world: FakeGateway
) -> None:
    two_unread(world)
    assert (await check(approval_hook, READ, {"chat": ALICE.id}))["decision"] == "allow"
    done = await call(claude, READ, {"chat": ALICE.id})
    assert done["ok"] is True
    assert [m["id"] for m in done["data"]["messages"]][:2] == [8, 7]
    assert world.read_acknowledgements() == []


async def test_reading_with_mark_read_marks_up_to_the_newest_message_returned(
    approval_hook: RawClient, claude: RawClient, world: FakeGateway
) -> None:
    ids = two_unread(world)
    args = {"chat": ALICE.id, "before_id": ids[-1], "mark_read": True}
    asked = await check(approval_hook, READ, args)
    assert question_text(asked).startswith(
        f"After reading, mark {A} as read up to the newest message Claude reads (2 unread now)?"
    )
    assert await answer(approval_hook, asked) == "confirmed"
    done = await call(claude, READ, args)
    assert done["data"]["marked"] is True
    assert max(m["id"] for m in done["data"]["messages"]) == ids[0]
    assert world.read_acknowledgements() == [
        {"chat_id": ALICE.id, "max_id": ids[0], "topic_id": None}
    ]


async def test_a_declined_mark_still_returns_the_messages_unmarked(
    approval_hook: RawClient, claude: RawClient, world: FakeGateway
) -> None:
    two_unread(world)
    args = {"chat": ALICE.id, "limit": 1, "mark_read": True}
    asked = await check(approval_hook, READ, args)
    assert await answer(approval_hook, asked, "Deny") == "declined"
    done = await call(claude, READ, args)
    assert done["ok"] is True
    assert [m["id"] for m in done["data"]["messages"]] == [8]
    assert (done["data"]["marked"], done["data"]["mark_note"]) == (
        False,
        "not marked: the person declined",
    )
    assert world.read_acknowledgements() == []


@pytest.mark.parametrize("value", ["yes", 1])
async def test_mark_read_must_be_a_boolean(claude: RawClient, value: object) -> None:
    refused = await call(claude, READ, {"chat": ALICE.id, "mark_read": value})
    assert refused["error"]["code"] == "invalid_arguments"
