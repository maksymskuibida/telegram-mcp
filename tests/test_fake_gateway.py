"""The fake gateway every other test uses (C3 FakeGateway, BR9.3)."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
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
    QrExpired,
    SentCodeInfo,
    TelegramError,
    TopicNotFound,
    UnreadState,
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


# QR login and log-out ---------------------------------------------------------------------


async def test_qr_tokens_are_handed_out_in_order_with_their_expiry() -> None:
    now = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
    fake = FakeGateway(authorized=False, qr_tokens=["tg://login?token=AAAA"], clock=lambda: now)
    first = await fake.sign_in_qr()
    second = await fake.sign_in_qr()
    assert first.url == "tg://login?token=AAAA"
    assert second.url.startswith("tg://login?token=FAKE")
    assert first.expires_at == now + timedelta(seconds=30)
    assert "AAAA" not in repr(first)
    assert fake.call_names() == ["sign_in_qr", "sign_in_qr"]


async def test_an_unscanned_wait_expires_and_lets_time_pass() -> None:
    waited: list[float] = []
    fake = FakeGateway(authorized=False, qr_scan_on_wait=3, qr_time_passes=waited.append)
    for timeout in (30.0, 12.5):
        with pytest.raises(QrExpired):
            await fake.wait_qr(timeout)
    assert waited == [30.0, 12.5]
    assert await fake.wait_qr(30.0) == fake.account
    assert fake.authorized
    assert ("wait_qr", {"timeout_seconds": 12.5}) in fake.calls


async def test_nobody_scans_when_no_scan_is_scripted() -> None:
    fake = FakeGateway(authorized=False)
    with pytest.raises(QrExpired):
        await fake.wait_qr(1.0)
    assert not fake.authorized


async def test_a_scan_on_an_account_with_a_password_asks_for_it() -> None:
    fake = FakeGateway(authorized=False, password="pw", qr_scan_on_wait=1)
    with pytest.raises(PasswordNeeded):
        await fake.wait_qr(30.0)
    assert not fake.authorized
    assert await fake.sign_in_password("pw") == fake.account


async def test_qr_calls_and_log_out_can_be_scripted_to_fail() -> None:
    fake = FakeGateway()
    fake.script("sign_in_qr", error=FloodWait(42))
    fake.script("log_out", error=TelegramError("AUTH_KEY_UNREGISTERED"))
    with pytest.raises(FloodWait):
        await fake.sign_in_qr()
    with pytest.raises(TelegramError):
        await fake.log_out()
    assert fake.authorized


@pytest.mark.parametrize("confirmed", [True, False])
async def test_log_out_returns_its_scripted_result(confirmed: bool) -> None:
    fake = FakeGateway(log_out_result=confirmed)
    assert await fake.log_out() is confirmed
    assert fake.authorized is not confirmed
    assert fake.call_names() == ["log_out"]


# U2: seeded messages, unread state and marking (BR13.1, BR14.1, BR14.2) -----------------------


def seeded(fake: FakeGateway) -> list[int]:
    fake.topics[FORUM.id] = {9: "Ideas", 10: "Off topic"}
    ids = [fake.add_message(ALICE.id, f"m{n}").id for n in range(3)]
    ids.append(fake.add_message(FORUM.id, "in ideas", topic_id=9).id)
    ids.append(fake.add_message(FORUM.id, "off topic", topic_id=10).id)
    return ids


async def test_unread_state_counts_a_chat_and_a_topic(fake_gateway: FakeGateway) -> None:
    ids = seeded(fake_gateway)
    assert await fake_gateway.unread_state(ALICE.id, None) == UnreadState(3, ids[2])
    assert await fake_gateway.unread_state(FORUM.id, 9) == UnreadState(1, ids[3], "Ideas")
    assert await fake_gateway.unread_state(FORUM.id, None) == UnreadState(2, ids[4])
    assert await fake_gateway.unread_state(TEAM.id, None) == UnreadState(0, 0)


async def test_mark_read_marks_up_to_max_id_and_nothing_beyond(fake_gateway: FakeGateway) -> None:
    ids = seeded(fake_gateway)
    await fake_gateway.mark_read(ALICE.id, ids[1], None)
    assert await fake_gateway.unread_state(ALICE.id, None) == UnreadState(1, ids[2])
    await fake_gateway.mark_read(FORUM.id, ids[3], 9)
    assert (await fake_gateway.unread_state(FORUM.id, 9)).unread == 0
    assert (await fake_gateway.unread_state(FORUM.id, 10)).unread == 1
    assert fake_gateway.read_acknowledgements() == [
        {"chat_id": ALICE.id, "max_id": ids[1], "topic_id": None},
        {"chat_id": FORUM.id, "max_id": ids[3], "topic_id": 9},
    ]


async def test_reads_and_fetches_never_acknowledge_reading(fake_gateway: FakeGateway) -> None:
    ids = seeded(fake_gateway)
    assert [m.id for m in await fake_gateway.get_messages(ALICE.id, [ids[0], 999])] == [ids[0]]
    # Newest first, as Telegram returns a history page (U4 BR22.3).
    assert [m.text for m in await fake_gateway.read_messages(ALICE.id, 2)] == ["m2", "m1"]
    older = await fake_gateway.read_messages(ALICE.id, 5, before_id=ids[1])
    assert [m.id for m in older] == [ids[0]]
    assert [m.id for m in await fake_gateway.read_messages(FORUM.id, 5, topic_id=9)] == [ids[3]]
    await fake_gateway.unread_state(ALICE.id, None)
    await fake_gateway.list_chats(None, 50, None)
    await fake_gateway.record_write("tg_chats_leave", approval_id="x")
    assert fake_gateway.read_acknowledgements() == []
    assert (await fake_gateway.unread_state(ALICE.id, None)).unread == 3


async def test_an_unknown_chat_or_topic_is_reported(fake_gateway: FakeGateway) -> None:
    seeded(fake_gateway)
    with pytest.raises(ChatNotFound):
        await fake_gateway.unread_state(99999, None)
    with pytest.raises(TopicNotFound):
        await fake_gateway.unread_state(FORUM.id, 404)
    with pytest.raises(TopicNotFound):
        await fake_gateway.mark_read(FORUM.id, 1, 404)


async def test_the_new_calls_can_be_scripted(fake_gateway: FakeGateway) -> None:
    seeded(fake_gateway)
    fake_gateway.script("unread_state", result=UnreadState(-1, 0))
    assert (await fake_gateway.unread_state(ALICE.id, None)).unread == -1
    fake_gateway.script("mark_read", error=FloodWait(3))
    with pytest.raises(FloodWait):
        await fake_gateway.mark_read(ALICE.id, 1, None)
    fake_gateway.authorized = False
    with pytest.raises(NotAuthorized):
        await fake_gateway.unread_state(ALICE.id, None)


async def test_mark_all_read_seeds_without_recording(fake_gateway: FakeGateway) -> None:
    seeded(fake_gateway)
    fake_gateway.mark_all_read()
    assert (await fake_gateway.unread_state(FORUM.id, 9)).unread == 0
    assert fake_gateway.read_acknowledgements() == []
