"""A seeded fake account and a broker running the shipped catalogue, for the U4 tool tests.

The broker is started with its default registry (every curated tool), the fake gateway, a fake
clock and a sleep that steps that clock, so a flood wait never really waits."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from support.brokers import FakeClock
from support.rpc import RawClient
from support.samples import ALICE, FAMILY, FORUM, NEWS, TEAM, fake_secrets, sample_chats
from telegram_mcp.broker.server import Broker
from telegram_mcp.gateway.fake import ChatExtra, FakeGateway
from telegram_mcp.gateway.models import ChatRef
from telegram_mcp.paths import FixedPaths
from telegram_mcp.vault import SessionVault, StoredSession

PREFIX = "mcp__plugin_telegram-mcp_telegram__"
ME = 4242
SAVED = ChatRef(id=ME, name="Test Person", type="user")
CLUB = ChatRef(id=-1000000000606, name="Open Club", type="supergroup")
BOB, CAROL, ROBOT = 501, 502, 503

FIRST, MINE, PHOTO, FAMILY_NOTE, TEAM_PINNED, IDEA = 1, 2, 3, 4, 5, 6
PHOTO_BYTES = b"\x89PNG fake image bytes"


def seed(fake: FakeGateway) -> FakeGateway:
    """One account with a user chat, a group, a supergroup, a channel, a forum, Saved
    Messages and a public club it has not joined; messages 1 to 6 in that order."""
    fake.chats = [*sample_chats(), SAVED, CLUB]
    fake.extras[TEAM.id] = ChatExtra(username="team_chat", description="We build", my_role="admin")
    fake.extras[NEWS.id] = ChatExtra(username="news_channel", member_count=900)
    fake.extras[CLUB.id] = ChatExtra(username="open_club", my_role="none", member_count=50)
    fake.extras[FAMILY.id] = ChatExtra(member_count=4)
    fake.topics[FORUM.id] = {9: "Ideas"}
    fake.add_user(ALICE.id, "Alice", username="alice", is_contact=True, phone="+4900001")
    fake.add_user(BOB, "Bob", username="bob", is_contact=True, bio="Bob's bio")
    fake.add_user(CAROL, "Carol", username="carol")
    fake.add_user(ROBOT, "Robot", username="helper_bot", is_bot=True)
    fake.add_member(TEAM.id, BOB, "admin", {"pin_messages": True, "invite_users": True})
    fake.add_member(TEAM.id, CAROL)
    fake.add_member(TEAM.id, ROBOT)
    fake.add_message(ALICE.id, "the first message", sender="Alice", sender_id=ALICE.id)
    fake.add_message(ALICE.id, "my own message", outgoing=True)
    fake.add_message(
        ALICE.id,
        "a photo",
        sender="Alice",
        sender_id=ALICE.id,
        media="photo",
        file_name="holiday.jpg",
        content=PHOTO_BYTES,
    )
    fake.add_message(FAMILY.id, "family note", sender="Mum")
    fake.add_message(TEAM.id, "the plan", sender="Bob", sender_id=BOB, pinned=True)
    fake.add_message(FORUM.id, "an idea", sender="Bob", sender_id=BOB, topic_id=9)
    fake.mark_all_read()
    return fake


def logged_in() -> StoredSession:
    values = fake_secrets()
    stored = StoredSession(values.session, values.api_id, values.api_hash)
    SessionVault().save(stored)
    return stored


@pytest.fixture
def paths(home: Path) -> FixedPaths:
    return FixedPaths.from_home(home)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def world(fake_gateway: FakeGateway, clock: FakeClock) -> FakeGateway:
    fake_gateway.sleep = clock.sleep
    return seed(fake_gateway)


@pytest.fixture
async def broker(
    paths: FixedPaths, world: FakeGateway, clock: FakeClock
) -> AsyncIterator[Broker]:
    logged_in()
    started = Broker(
        paths,
        gateway_factory=lambda session, api_id, api_hash: world,
        clock=clock,
        sleep=clock.sleep,
    )
    await started.start()
    try:
        yield started
    finally:
        await started.stop()


@pytest.fixture
async def claude(broker: Broker, paths: FixedPaths) -> AsyncIterator[RawClient]:
    client = await RawClient.connect(paths, client_name="claude-code")
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture
async def approval_hook(broker: Broker, paths: FixedPaths) -> AsyncIterator[RawClient]:
    client = await RawClient.connect(paths, client="approval-hook", client_name=None)
    try:
        yield client
    finally:
        await client.close()


async def check(hook: RawClient, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = await hook.result("approval.check", {"tool": tool, "args": args})
    return result


async def answer(hook: RawClient, asked: dict[str, Any], pick: str = "Allow") -> str:
    result = await hook.result(
        "approval.answer",
        {
            "question": asked["question"],
            "options": asked["options"],
            "option_descriptions": asked["option_descriptions"],
            "multi_select": False,
            "pick": pick,
        },
    )
    return str(result["outcome"])


async def call(client: RawClient, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = await client.result("tool.call", {"tool": tool, "args": args})
    return result


async def confirm(hook: RawClient, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    asked = await check(hook, tool, args)
    assert asked["decision"] == "ask", asked
    assert await answer(hook, asked) == "confirmed"
    return asked


async def confirmed_run(
    hook: RawClient, client: RawClient, tool: str, args: dict[str, Any]
) -> dict[str, Any]:
    """Ask, Allow, call again: the way Claude Code runs a write."""
    await confirm(hook, tool, args)
    return await call(client, tool, args)


def question_text(asked: dict[str, Any]) -> str:
    """The question without its ``[tg-<id>] `` prefix."""
    text = str(asked["question"])
    assert text.startswith("[tg-")
    return text.split("] ", 1)[1]


def outbox(paths: FixedPaths) -> Path:
    folder = paths.home / "Downloads" / "telegram-mcp" / "outbox"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def downloads(paths: FixedPaths) -> Path:
    return paths.home / "Downloads" / "telegram-mcp"


def put_file(paths: FixedPaths, name: str, data: bytes = b"report") -> Path:
    path = outbox(paths) / name
    path.write_bytes(data)
    return path


def write_calls(fake: FakeGateway, names: set[str]) -> list[tuple[str, dict[str, Any]]]:
    return [(name, args) for name, args in fake.calls if name in names]


WRITE_CALLS = {
    "edit",
    "delete",
    "forward",
    "react",
    "pin",
    "unpin",
    "topic_create",
    "create_group",
    "create_channel",
    "join",
    "leave",
    "invite_link",
    "set_admin_rights",
    "edit_chat",
    "edit_own_profile",
    "send_file",
    "send_text",
    "mark_read",
}


def log_entries(paths: FixedPaths) -> list[dict[str, Any]]:
    return [json.loads(line) for line in paths.log.read_text(encoding="utf-8").splitlines()]


Check = Callable[[RawClient, str, dict[str, Any]], Any]
