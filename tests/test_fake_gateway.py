"""The fake gateway every other test uses (C3 FakeGateway, BR9.3)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from support.samples import ALICE, FORUM, TEAM
from telegram_mcp.gateway.fake import FAKE_RECORD_NAME, FakeGateway
from telegram_mcp.gateway.models import (
    BadCode,
    BadPassword,
    ChatNotFound,
    ChatRef,
    FloodWait,
    GatewayTimeout,
    NotAuthorized,
    OutcomeUnknown,
    PasswordNeeded,
    SentCodeInfo,
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
    assert await fake.sign_in_phone("+10000000000") == SentCodeInfo("app")
    assert await fake.resend_code("+10000000000") == SentCodeInfo("sms")
    assert fake.call_names() == ["sign_in_phone", "resend_code"]
    with pytest.raises(BadCode):
        await fake.sign_in_code("00000")
    with pytest.raises(PasswordNeeded):
        await fake.sign_in_code(fake.login_code)
    with pytest.raises(BadPassword):
        await fake.sign_in_password("wrong")
    me = await fake.sign_in_password("pw")
    assert me == fake.account
    assert fake.authorized


OWN = ChatRef(id=4242, name="Test Person", type="user")
# A chat that claims the mark: only the gateway's own-id check decides it (NFR4.4).
TITLED_SAVED = ChatRef(id=555, name="Saved Messages", type="user", saved_messages=True)


async def test_your_own_chat_is_marked_and_named_saved_messages() -> None:
    fake = FakeGateway(chats=[OWN, TITLED_SAVED, ALICE])
    page, _ = await fake.list_chats(None, 50, None)
    shown = {chat.id: (chat.name, chat.saved_messages) for chat in page}
    assert shown == {
        OWN.id: ("Saved Messages", True),
        TITLED_SAVED.id: ("Saved Messages", False),
        ALICE.id: ("Alice", False),
    }
    own = await fake.resolve_chat(OWN.id)
    assert (own.name, own.saved_messages) == ("Saved Messages", True)
    titled = await fake.resolve_chat(TITLED_SAVED.id)
    assert (titled.name, titled.saved_messages) == ("Saved Messages", False)


async def test_nothing_is_marked_when_your_own_id_cannot_be_read() -> None:
    fake = FakeGateway(chats=[OWN, ALICE], own_id_known=False)
    page, _ = await fake.list_chats(None, 50, None)
    assert [(chat.name, chat.saved_messages) for chat in page] == [
        ("Test Person", False),
        ("Alice", False),
    ]
    assert not (await fake.resolve_chat(OWN.id)).saved_messages


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


# Delays and failing connects, for the broker's scheduling tests ----------------------------


async def test_a_delay_is_awaited_with_the_fakes_sleep_before_the_call_goes_on(
    fake_gateway: FakeGateway,
) -> None:
    waited: list[float] = []

    async def record(seconds: float) -> None:
        waited.append(seconds)

    fake_gateway.sleep = record
    fake_gateway.script("list_chats", delay=12.5)
    fake_gateway.script("resolve_chat", delay=3, error=FloodWait(7))
    page, _ = await fake_gateway.list_chats(None, 50, None)
    assert len(page) == 5
    with pytest.raises(FloodWait):
        await fake_gateway.resolve_chat(ALICE.id)
    await fake_gateway.list_chats(None, 50, None)
    assert waited == [12.5, 3]


async def test_a_connect_can_fail_a_given_number_of_times_then_succeed(
    fake_gateway: FakeGateway,
) -> None:
    fake_gateway.script("connect", error=GatewayTimeout(), times=3)
    for _ in range(3):
        with pytest.raises(GatewayTimeout):
            await fake_gateway.connect()
        assert fake_gateway.connected is False
    await fake_gateway.connect()
    assert fake_gateway.connected is True
    assert fake_gateway.call_names() == ["connect"] * 4


async def test_a_real_delay_holds_the_call_and_the_file_form_reads_it(home: Path) -> None:
    config = home / "fake-gateway.json"
    config.write_text(json.dumps({"scripts": {"list_chats": {"delay": 0.2, "times": 1}}}))
    fake = FakeGateway.from_file(config)
    started = time.monotonic()
    await fake.list_chats(None, 50, None)
    assert time.monotonic() - started >= 0.2
    started = time.monotonic()
    await fake.list_chats(None, 50, None)
    assert time.monotonic() - started < 0.1
