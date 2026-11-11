"""The Telethon gateway: construction and error mapping with a stubbed client, no network
(NFR6.1 to NFR6.3, NFR5.1, NFR9.1)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import errors
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
    TelegramError,
)
from telegram_mcp.gateway.telethon_gateway import TelethonGateway

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
        return SimpleNamespace(phone_code_hash="hash")

    async def sign_in(self, *args: Any, **kwargs: Any) -> None:
        await self._maybe_fail("sign_in")

    async def get_me(self) -> Any:
        return types.User(id=7, first_name="Ada", last_name="L", username="ada")

    async def get_dialogs(self, **options: Any) -> list[Any]:
        await self._maybe_fail("get_dialogs")
        return self.dialogs[: options.get("limit") or None]

    async def get_entity(self, peer: int) -> Any:
        await self._maybe_fail("get_entity")
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


async def test_resolve_chat_scans_recent_chats_once_then_reports_not_found() -> None:
    gateway, client = make_gateway()
    with pytest.raises(ChatNotFound):
        await gateway.resolve_chat(12345)
    assert client.connects == 1
    me = await gateway.me()
    assert (me.name, me.username) == ("Ada L", "ada")
    assert gateway.export_session() == "saved-session"
