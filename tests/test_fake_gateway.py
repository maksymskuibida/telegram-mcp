"""The fake gateway every other test uses (C3 FakeGateway, BR9.3)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from support.samples import ALICE, FORUM, TEAM
from telegram_mcp.gateway.fake import FAKE_RECORD_NAME, FakeGateway
from telegram_mcp.gateway.models import (
    BadCode,
    BadPassword,
    ChatNotFound,
    FloodWait,
    NotAuthorized,
    OutcomeUnknown,
    PasswordNeeded,
)


async def test_records_every_call_in_order_and_pages_by_folder(fake_gateway: FakeGateway) -> None:
    page, cursor = await fake_gateway.list_chats(folder_id=1, limit=1, offset=None)
    assert page == [TEAM]
    assert cursor is not None
    page, cursor = await fake_gateway.list_chats(folder_id=1, limit=1, offset=cursor)
    assert page == [FORUM]
    assert cursor is None
    await fake_gateway.resolve_chat(ALICE.id)
    assert fake_gateway.call_names() == ["list_chats", "list_chats", "resolve_chat"]


async def test_send_is_recorded_and_reads_never_acknowledge(fake_gateway: FakeGateway) -> None:
    sent = await fake_gateway.send_text(ALICE.id, "  hello\n", reply_to=7, topic_id=None)
    await fake_gateway.list_chats(None, 50, None)
    assert sent.chat_id == ALICE.id
    assert fake_gateway.sent[0].text == "  hello\n"
    assert fake_gateway.sent[0].reply_to == 7
    assert not any("read" in name or "ack" in name for name in fake_gateway.call_names())


async def test_scripted_failures_apply_the_given_number_of_times(fake_gateway: FakeGateway) -> None:
    fake_gateway.script("list_chats", error=FloodWait(120))
    with pytest.raises(FloodWait) as waited:
        await fake_gateway.list_chats(None, 50, None)
    assert waited.value.seconds == 120
    page, _ = await fake_gateway.list_chats(None, 50, None)
    assert len(page) == 5
    with pytest.raises(ChatNotFound):
        await fake_gateway.resolve_chat(999)


async def test_a_hanging_call_never_returns(fake_gateway: FakeGateway) -> None:
    fake_gateway.script("resolve_chat", hang=True)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(fake_gateway.resolve_chat(ALICE.id), 0.05)


async def test_not_authorized_and_drop_after_send(fake_gateway: FakeGateway) -> None:
    fake_gateway.script("send_text", drop_after_send=True)
    with pytest.raises(OutcomeUnknown):
        await fake_gateway.send_text(ALICE.id, "once", None, None)
    assert len(fake_gateway.sent) == 1
    fake_gateway.authorized = False
    assert await fake_gateway.is_authorized() is False
    with pytest.raises(NotAuthorized):
        await fake_gateway.list_chats(None, 50, None)


async def test_sign_in_with_code_and_password() -> None:
    fake = FakeGateway(authorized=False, password="pw")
    await fake.sign_in_phone("+10000000000")
    with pytest.raises(BadCode):
        await fake.sign_in_code("00000")
    with pytest.raises(PasswordNeeded):
        await fake.sign_in_code(fake.login_code)
    with pytest.raises(BadPassword):
        await fake.sign_in_password("wrong")
    me = await fake.sign_in_password("pw")
    assert me == fake.account
    assert fake.authorized


async def test_from_file_builds_chats_scripts_and_a_call_record(home: Path) -> None:
    config = home / "fake-gateway.json"
    config.write_text(
        json.dumps(
            {
                "chats": [{"id": 1, "name": "One", "type": "user"}],
                "scripts": {"send_text": {"error": "telegram_error:PEER_FLOOD", "times": None}},
            }
        )
    )
    fake = FakeGateway.from_file(config)
    assert (await fake.resolve_chat(1)).name == "One"
    for _ in range(2):
        with pytest.raises(Exception, match="PEER_FLOOD"):
            await fake.send_text(1, "x", None, None)
    lines = (home / FAKE_RECORD_NAME).read_text().splitlines()
    assert [json.loads(line)["call"] for line in lines] == [
        "resolve_chat",
        "send_text",
        "send_text",
    ]
