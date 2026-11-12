"""A client that cannot show the question box (BR27.2), written before the code: every U4 write
and the mark tool are refused unsupported_client before any Telegram call, at the broker and in
the MCP server; tg_messages_read with mark_read runs as a read with a note."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from support.catalog import call
from support.rpc import RawClient
from support.samples import ALICE
from telegram_mcp.broker.client import is_write
from telegram_mcp.broker.server import Broker
from telegram_mcp.gateway.fake import FakeGateway
from telegram_mcp.paths import FixedPaths

U4_WRITES_AND_MARK = [
    "tg_messages_edit",
    "tg_messages_delete",
    "tg_messages_forward",
    "tg_messages_react",
    "tg_messages_pin",
    "tg_messages_unpin",
    "tg_topics_create",
    "tg_chats_create",
    "tg_chats_join",
    "tg_chats_leave",
    "tg_chats_edit",
    "tg_invites_create",
    "tg_admins_set",
    "tg_profile_edit",
    "tg_files_send",
    "tg_chats_mark_read",
]


@pytest.fixture
async def other_client(broker: Broker, paths: FixedPaths) -> AsyncIterator[RawClient]:
    client = await RawClient.connect(paths, client_name="some-other-client")
    try:
        yield client
    finally:
        await client.close()


@pytest.mark.parametrize("tool", U4_WRITES_AND_MARK)
async def test_every_write_and_the_mark_are_refused_for_another_client(
    other_client: RawClient, world: FakeGateway, tool: str
) -> None:
    world.calls.clear()
    refused = await call(other_client, tool, {"chat": ALICE.id})
    assert refused["error"]["code"] == "unsupported_client"
    assert world.calls == []


@pytest.mark.parametrize("tool", U4_WRITES_AND_MARK)
def test_the_mcp_server_treats_every_write_and_the_mark_as_writes(tool: str) -> None:
    assert is_write(tool)


def test_the_read_tools_are_not_writes() -> None:
    assert not is_write("tg_messages_read")
    assert not is_write("tg_media_download")
    assert not is_write("tg_chats_resolve")


async def test_reading_with_mark_read_in_another_client_reads_with_a_note(
    other_client: RawClient, world: FakeGateway
) -> None:
    world.add_message(ALICE.id, "unread")
    done = await call(other_client, "tg_messages_read", {"chat": ALICE.id, "mark_read": True})
    assert done["ok"] is True
    assert done["data"]["marked"] is False
    assert done["data"]["mark_note"] == "marking is not supported in this client"
    assert world.read_acknowledgements() == []
