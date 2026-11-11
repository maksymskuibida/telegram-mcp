"""The Telethon gateway: construction and error mapping with a stubbed client, no network
(NFR6.1 to NFR6.3, NFR5.1, NFR9.1)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import errors, functions
from telethon.tl import types

from telegram_mcp.gateway.models import (
    BadCode,
    BadPassword,
    ChatNotFound,
    CodeExpired,
    FloodWait,
    GatewayError,
    GatewayTimeout,
    NotAuthorized,
    OutcomeUnknown,
    PasswordNeeded,
    SentCodeInfo,
    TelegramError,
)
from telegram_mcp.gateway.telethon_gateway import TelethonGateway, sent_code_info

API_HASH = "0" * 32


class StubClient:
    """Stands in for TelegramClient. ``fail`` maps a method name to an exception to raise,
    or to the string "hang"."""

    def __init__(self, session: Any, api_id: int, api_hash: str, **options: Any) -> None:
        self.options = options
        self.connected = False
        self.connects = 0
        self.fail: dict[str, BaseException | str] = {}
        self.dialogs: list[Any] = []
        self.sent: list[dict[str, Any]] = []
        self.entities: dict[int, Any] = {}
        self.me_calls = 0
        self.code_requests: list[str] = []
        self.requests: list[Any] = []
        self.sign_ins: list[dict[str, Any]] = []
        self.session = SimpleNamespace(save=lambda: "saved-session")

    async def _maybe_fail(self, name: str) -> None:
        how = self.fail.pop(name, None)
        if how == "hang":
            await asyncio.Event().wait()
        if isinstance(how, BaseException):
            raise how

    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connects += 1
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def is_user_authorized(self) -> bool:
        await self._maybe_fail("is_user_authorized")
        return True

    async def send_code_request(self, phone: str) -> Any:
        self.code_requests.append(phone)
        return types.auth.SentCode(
            type=types.auth.SentCodeTypeApp(length=5),
            phone_code_hash="hash",
            next_type=types.auth.CodeTypeSms(),
            timeout=60,
        )

    async def __call__(self, request: Any) -> Any:
        await self._maybe_fail("call")
        self.requests.append(request)
        return types.auth.SentCode(
            type=types.auth.SentCodeTypeSms(length=5), phone_code_hash="hash-2"
        )

    async def sign_in(self, *args: Any, **kwargs: Any) -> None:
        await self._maybe_fail("sign_in")
        self.sign_ins.append({"args": args, **kwargs})

    async def get_me(self) -> Any:
        self.me_calls += 1
        await self._maybe_fail("get_me")
        return types.User(id=7, first_name="Ada", last_name="L", username="ada")

    async def get_dialogs(self, **options: Any) -> list[Any]:
        await self._maybe_fail("get_dialogs")
        return self.dialogs[: options.get("limit") or None]

    async def get_entity(self, peer: int) -> Any:
        await self._maybe_fail("get_entity")
        if peer in self.entities:
            return self.entities[peer]
        raise ValueError("Could not find the input entity")

    async def get_input_entity(self, peer: int) -> Any:
        return types.InputPeerEmpty()

    async def send_message(self, chat: int, text: str, **options: Any) -> Any:
        await self._maybe_fail("send_message")
        self.sent.append({"chat": chat, "text": text, **options})
        return SimpleNamespace(id=55, date=datetime(2026, 1, 1, tzinfo=UTC))


def make_gateway(timeout: float = 30.0) -> tuple[TelethonGateway, StubClient]:
    holder: list[StubClient] = []

    def factory(*args: Any, **kwargs: Any) -> StubClient:
        holder.append(StubClient(*args, **kwargs))
        return holder[0]

    gateway = TelethonGateway("", 1, API_HASH, client_factory=factory, call_timeout=timeout)
    return gateway, holder[0]


async def test_the_real_client_never_retries_or_sleeps_on_its_own() -> None:
    gateway = TelethonGateway("", 1, API_HASH)
    client: Any = gateway._client
    assert client._request_retries == 1
    assert client.flood_sleep_threshold == 0
    assert client._timeout == 30
    assert not client.is_connected()


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (errors.FloodWaitError(request=None, capture=10), FloodWait),
        (errors.AuthKeyUnregisteredError(request=None), NotAuthorized),
        (errors.RPCError(request=None, message="CHAT_WRITE_FORBIDDEN"), TelegramError),
        (errors.PhoneCodeInvalidError(request=None), BadCode),
        (errors.PhoneCodeExpiredError(request=None), CodeExpired),
        (errors.SessionPasswordNeededError(request=None), PasswordNeeded),
        (errors.PasswordHashInvalidError(request=None), BadPassword),
    ],
)
async def test_telegram_errors_are_mapped(
    raised: BaseException, expected: type[GatewayError]
) -> None:
    gateway, client = make_gateway()
    client.fail["sign_in"] = raised
    with pytest.raises(expected) as mapped:
        await gateway.sign_in_code("12345")
    if isinstance(mapped.value, FloodWait):
        assert mapped.value.seconds == 10
    if isinstance(mapped.value, TelegramError):
        assert mapped.value.reason == "CHAT_WRITE_FORBIDDEN"


@pytest.mark.parametrize("failure", ["hang", ConnectionError("dropped")])
async def test_a_send_that_times_out_or_drops_is_outcome_unknown(failure: Any) -> None:
    gateway, client = make_gateway(timeout=0.05)
    client.fail["send_message"] = failure
    with pytest.raises(OutcomeUnknown):
        await gateway.send_text(1, "hi", None, None)


async def test_reads_time_out_and_a_dropped_connection_reconnects_on_the_next_call() -> None:
    gateway, client = make_gateway(timeout=0.05)
    client.fail["get_dialogs"] = "hang"
    with pytest.raises(GatewayTimeout):
        await gateway.list_chats(None, 50, None)
    client.fail["get_dialogs"] = ConnectionError("dropped")
    with pytest.raises(TelegramError):
        await gateway.list_chats(None, 50, None)
    client.connected = False
    chats, _ = await gateway.list_chats(None, 50, None)
    assert chats == []
    assert client.connects == 2


def dialog(entity: Any, *, name: str, text: str | None, muted: bool, folder: int | None) -> Any:
    now = datetime.now(UTC)
    mute_until = now + timedelta(days=1) if muted else None
    return SimpleNamespace(
        id=entity.id if isinstance(entity, types.User) else -entity.id,
        name=name,
        entity=entity,
        unread_count=3,
        message=SimpleNamespace(id=9, message=text) if text is not None else None,
        date=now,
        folder_id=folder,
        dialog=SimpleNamespace(notify_settings=SimpleNamespace(mute_until=mute_until)),
    )


async def test_list_chats_maps_dialogs_and_send_text_never_parses_markup() -> None:
    gateway, client = make_gateway()
    photo = types.ChatPhotoEmpty()
    client.dialogs = [
        dialog(
            types.User(id=1, first_name="Ann"), name="Ann", text="x" * 300, muted=True, folder=1
        ),
        dialog(
            types.Chat(id=2, title="G", photo=photo, participants_count=2, date=None, version=1),
            name="G",
            text=None,
            muted=False,
            folder=None,
        ),
        dialog(
            types.Channel(id=3, title="S", photo=photo, date=None, megagroup=True),
            name="S",
            text="hi",
            muted=False,
            folder=None,
        ),
        dialog(
            types.Channel(id=4, title="F", photo=photo, date=None, megagroup=True, forum=True),
            name="F",
            text="hi",
            muted=False,
            folder=None,
        ),
        dialog(
            types.Channel(id=5, title="C", photo=photo, date=None, broadcast=True),
            name="C",
            text="hi",
            muted=False,
            folder=None,
        ),
    ]
    chats, cursor = await gateway.list_chats(None, 5, None)
    assert [c.type for c in chats] == ["user", "group", "supergroup", "forum", "channel"]
    assert chats[0].muted
    assert not chats[1].muted
    assert chats[0].last_preview == "x" * 200
    assert chats[0].folder_ids == (1,)
    assert cursor is not None
    assert cursor.startswith("v1.")
    more, _ = await gateway.list_chats(None, 5, cursor)
    assert len(more) == 5
    sent = await gateway.send_text(1, "*not bold*", None, 77)
    assert sent.id == 55
    assert client.sent[0]["parse_mode"] is None
    assert client.sent[0]["reply_to"] == 77


@pytest.mark.parametrize(
    ("reply_to", "topic_id", "target"), [(5, None, 5), (None, 77, 77), (None, None, None)]
)
async def test_send_text_passes_a_reply_or_the_topics_top_message_as_reply_to(
    reply_to: int | None, topic_id: int | None, target: int | None
) -> None:
    # A forum topic is addressed by replying to its top message, whose id is the topic id.
    gateway, client = make_gateway()
    await gateway.send_text(-1001, "hi", reply_to, topic_id)
    assert client.sent == [{"chat": -1001, "text": "hi", "reply_to": target, "parse_mode": None}]


async def test_sign_in_phone_says_where_the_code_went_and_resend_uses_its_hash() -> None:
    gateway, client = make_gateway()
    sent = await gateway.sign_in_phone("+15550001234")
    assert sent == SentCodeInfo("app", next_delivery="sms", timeout_seconds=60)
    resent = await gateway.resend_code("+15550001234")
    assert resent == SentCodeInfo("sms")
    request = client.requests[0]
    assert isinstance(request, functions.auth.ResendCodeRequest)
    assert (request.phone_number, request.phone_code_hash) == ("+15550001234", "hash")
    await gateway.sign_in_code("12345")
    assert client.sign_ins[0]["phone_code_hash"] == "hash-2"


async def test_a_refused_resend_is_a_telegram_error() -> None:
    gateway, client = make_gateway()
    await gateway.sign_in_phone("+15550001234")
    client.fail["call"] = errors.RPCError(request=None, message="SEND_CODE_UNAVAILABLE")
    with pytest.raises(TelegramError, match="SEND_CODE_UNAVAILABLE"):
        await gateway.resend_code("+15550001234")


@pytest.mark.parametrize(
    ("sent", "expected"),
    [
        (
            types.auth.SentCode(
                type=types.auth.SentCodeTypeEmailCode(email_pattern="a***@e.com", length=6),
                phone_code_hash="h",
            ),
            SentCodeInfo("email", email_pattern="a***@e.com"),
        ),
        (
            types.auth.SentCode(
                type=types.auth.SentCodeTypeCall(length=5),
                phone_code_hash="h",
                next_type=types.auth.CodeTypeFlashCall(),
                timeout=120,
            ),
            SentCodeInfo("call", next_delivery="flash_call", timeout_seconds=120),
        ),
        (
            types.auth.SentCode(
                type=types.auth.SentCodeTypeFlashCall(pattern="+1*"),
                phone_code_hash="h",
                next_type=types.auth.CodeTypeMissedCall(),
            ),
            SentCodeInfo("flash_call", next_delivery="missed_call"),
        ),
        (
            types.auth.SentCode(
                type=types.auth.SentCodeTypeMissedCall(prefix="+1", length=4),
                phone_code_hash="h",
                next_type=types.auth.CodeTypeCall(),
            ),
            SentCodeInfo("missed_call", next_delivery="call"),
        ),
        (
            types.auth.SentCode(
                type=types.auth.SentCodeTypeFragmentSms(url="https://fragment.com", length=5),
                phone_code_hash="h",
                next_type=types.auth.CodeTypeFragmentSms(),
            ),
            SentCodeInfo("fragment", next_delivery="fragment"),
        ),
        (
            types.auth.SentCode(
                type=types.auth.SentCodeTypeSetUpEmailRequired(), phone_code_hash="h"
            ),
            SentCodeInfo("other"),
        ),
        (SimpleNamespace(phone_code_hash="h"), SentCodeInfo("other")),
    ],
)
def test_every_sent_code_type_is_mapped(sent: Any, expected: SentCodeInfo) -> None:
    assert sent_code_info(sent) == expected


def user_dialog(user_id: int, name: str) -> Any:
    return dialog(
        types.User(id=user_id, first_name=name), name=name, text=None, muted=False, folder=None
    )


async def test_your_own_chat_is_marked_from_your_own_id_once_per_connection() -> None:
    gateway, client = make_gateway()
    # The stub's own account has id 7; id 8 is a contact who titled themselves "Saved Messages".
    client.dialogs = [user_dialog(7, "Ada L"), user_dialog(8, "Saved Messages")]
    chats, _ = await gateway.list_chats(None, 50, None)
    assert [(c.id, c.name, c.saved_messages) for c in chats] == [
        (7, "Saved Messages", True),
        (8, "Saved Messages", False),
    ]
    client.entities = {7: types.User(id=7, first_name="Ada"), 8: types.User(id=8, first_name="S")}
    own = await gateway.resolve_chat(7)
    assert (own.name, own.saved_messages) == ("Saved Messages", True)
    assert not (await gateway.resolve_chat(8)).saved_messages
    assert client.me_calls == 1
    client.connected = False
    await gateway.list_chats(None, 50, None)
    assert client.me_calls == 2


async def test_nothing_is_marked_when_your_own_id_cannot_be_read() -> None:
    gateway, client = make_gateway()
    client.dialogs = [user_dialog(7, "Ada L")]
    client.fail["get_me"] = errors.RPCError(request=None, message="INTERNAL")
    chats, _ = await gateway.list_chats(None, 50, None)
    assert [(c.name, c.saved_messages) for c in chats] == [("Ada L", False)]


async def test_resolve_chat_scans_recent_chats_once_then_reports_not_found() -> None:
    gateway, client = make_gateway()
    with pytest.raises(ChatNotFound):
        await gateway.resolve_chat(12345)
    assert client.connects == 1
    me = await gateway.me()
    assert (me.name, me.username) == ("Ada L", "ada")
    assert gateway.export_session() == "saved-session"
