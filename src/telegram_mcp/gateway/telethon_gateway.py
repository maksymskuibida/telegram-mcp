"""The real gateway, on Telethon. This is the only module that imports Telethon (ADR-004).

The client never retries or sleeps on its own (NFR6.2), every call has a 30 s limit (NFR6.1),
and a dropped connection is re-established on the next call (NFR6.3)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from telethon import TelegramClient, errors, utils
from telethon.sessions import StringSession
from telethon.tl import types

from telegram_mcp.gateway.models import (
    PREVIEW_LIMIT,
    BadCode,
    BadPassword,
    ChatNotFound,
    ChatRef,
    ChatType,
    CodeExpired,
    FloodWait,
    GatewayError,
    GatewayTimeout,
    Me,
    NotAuthorized,
    OutcomeUnknown,
    PasswordNeeded,
    SentMessage,
    TelegramError,
    decode_cursor,
    encode_cursor,
)

CALL_TIMEOUT = 30.0
CLIENT_OPTIONS: dict[str, Any] = {
    "request_retries": 1,
    "flood_sleep_threshold": 0,
    "timeout": 30,
}
RESOLVE_DIALOG_SCAN = 200

T = TypeVar("T")


class TelethonGateway:
    def __init__(
        self,
        session: str,
        api_id: int,
        api_hash: str,
        *,
        client_factory: Callable[..., Any] = TelegramClient,
        call_timeout: float = CALL_TIMEOUT,
    ) -> None:
        self._client = client_factory(StringSession(session), api_id, api_hash, **CLIENT_OPTIONS)
        self._timeout = call_timeout
        self._phone: str | None = None
        self._phone_code_hash: str | None = None

    async def _call(self, make: Callable[[], Awaitable[T]], *, write: bool = False) -> T:
        """Run one Telegram call with the 30 s limit, mapping every failure to a gateway error.
        For a write, any failure after the connection is up is ``OutcomeUnknown``."""
        await self._ensure_connected()
        try:
            return await asyncio.wait_for(make(), self._timeout)
        except GatewayError:
            raise
        except errors.RPCError as error:
            raise _map_rpc_error(error) from error
        except (TimeoutError, ConnectionError, OSError) as error:
            if write:
                raise OutcomeUnknown() from error
            if isinstance(error, TimeoutError):
                raise GatewayTimeout() from error
            raise TelegramError("the connection to Telegram was lost") from error

    async def _ensure_connected(self) -> None:
        if self._client.is_connected():
            return
        try:
            await asyncio.wait_for(self._client.connect(), self._timeout)
        except TimeoutError as error:
            raise GatewayTimeout() from error
        except (ConnectionError, OSError) as error:
            raise TelegramError("could not connect to Telegram") from error

    async def connect(self) -> None:
        await self._ensure_connected()

    async def disconnect(self) -> None:
        if self._client.is_connected():
            await asyncio.wait_for(self._client.disconnect(), self._timeout)

    async def is_authorized(self) -> bool:
        return bool(await self._call(self._client.is_user_authorized))

    async def sign_in_phone(self, phone: str) -> None:
        sent = await self._call(lambda: self._client.send_code_request(phone))
        self._phone = phone
        self._phone_code_hash = sent.phone_code_hash

    async def sign_in_code(self, code: str) -> Me:
        phone, code_hash = self._phone, self._phone_code_hash
        await self._call(lambda: self._client.sign_in(phone, code, phone_code_hash=code_hash))
        return await self.me()

    async def sign_in_password(self, password: str) -> Me:
        await self._call(lambda: self._client.sign_in(password=password))
        return await self.me()

    def export_session(self) -> str:
        return str(self._client.session.save())

    async def me(self) -> Me:
        user = await self._call(self._client.get_me)
        if user is None:
            raise NotAuthorized()
        return Me(id=int(user.id), name=utils.get_display_name(user), username=user.username)

    async def list_chats(
        self, folder_id: int | None, limit: int, offset: str | None
    ) -> tuple[list[ChatRef], str | None]:
        options: dict[str, Any] = {"limit": limit, "folder": folder_id}
        if offset:
            date_ts, message_id, peer_id = decode_cursor(offset)
            options["offset_date"] = datetime.fromtimestamp(date_ts, UTC)
            options["offset_id"] = message_id
            options["offset_peer"] = await self._call(
                lambda: self._client.get_input_entity(peer_id)
            )
        dialogs = await self._call(lambda: self._client.get_dialogs(**options))
        chats = [_chat_from_dialog(d) for d in dialogs]
        next_offset = None
        if len(dialogs) >= limit and dialogs:
            last = dialogs[-1]
            stamp = int(last.date.timestamp()) if last.date else 0
            message_id = int(last.message.id) if last.message else 0
            next_offset = encode_cursor(stamp, message_id, int(last.id))
        return chats, next_offset

    async def resolve_chat(self, chat_id: int) -> ChatRef:
        try:
            entity = await self._call(lambda: self._client.get_entity(chat_id))
        except (ValueError, TelegramError):
            # The entity cache of a fresh session is empty; fill it from recent chats once.
            await self._call(lambda: self._client.get_dialogs(limit=RESOLVE_DIALOG_SCAN))
            try:
                entity = await self._call(lambda: self._client.get_entity(chat_id))
            except (ValueError, TelegramError) as error:
                raise ChatNotFound() from error
        return ChatRef(
            id=int(utils.get_peer_id(entity)),
            name=utils.get_display_name(entity),
            type=_chat_type(entity),
        )

    async def send_text(
        self, chat_id: int, text: str, reply_to: int | None, topic_id: int | None
    ) -> SentMessage:
        target = reply_to if reply_to is not None else topic_id
        message = await self._call(
            lambda: self._client.send_message(chat_id, text, reply_to=target, parse_mode=None),
            write=True,
        )
        return SentMessage(id=int(message.id), chat_id=chat_id, date=message.date)


_SIGN_IN_ERRORS: tuple[tuple[type[Exception], type[GatewayError]], ...] = (
    (errors.SessionPasswordNeededError, PasswordNeeded),
    (errors.PhoneCodeExpiredError, CodeExpired),
    (errors.PhoneCodeInvalidError, BadCode),
    (errors.PhoneCodeEmptyError, BadCode),
    (errors.PasswordHashInvalidError, BadPassword),
)


def _map_rpc_error(error: Any) -> GatewayError:
    """Telegram's own errors, mapped. Sign-in errors first: some are Unauthorized subclasses."""
    for telethon_error, ours in _SIGN_IN_ERRORS:
        if isinstance(error, telethon_error):
            return ours()
    if isinstance(error, errors.FloodWaitError):
        return FloodWait(int(error.seconds))
    if isinstance(error, errors.UnauthorizedError | errors.AuthKeyError):
        return NotAuthorized()
    return TelegramError(str(error.message))


def _chat_type(entity: Any) -> ChatType:
    if isinstance(entity, types.User):
        return "user"
    if isinstance(entity, types.Chat | types.ChatForbidden):
        return "group"
    if getattr(entity, "forum", False):
        return "forum"
    if getattr(entity, "megagroup", False):
        return "supergroup"
    return "channel"


def _chat_from_dialog(dialog: Any) -> ChatRef:
    settings = getattr(dialog.dialog, "notify_settings", None)
    mute_until = getattr(settings, "mute_until", None)
    muted = isinstance(mute_until, datetime) and mute_until > datetime.now(UTC)
    text = dialog.message.message if dialog.message is not None else None
    preview = text[:PREVIEW_LIMIT] if text else None
    folder = getattr(dialog, "folder_id", None)
    return ChatRef(
        id=int(dialog.id),
        name=str(dialog.name or ""),
        type=_chat_type(dialog.entity),
        unread=int(dialog.unread_count or 0),
        muted=muted,
        last_preview=preview,
        folder_ids=(int(folder),) if folder is not None else (),
    )
