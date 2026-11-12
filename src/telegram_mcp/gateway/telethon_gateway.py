"""The real gateway, on Telethon. This is the only module that imports Telethon (ADR-004).

The client never retries or sleeps on its own (NFR6.2), every call has a 30 s limit (NFR6.1),
and a dropped connection is re-established on the next call (NFR6.3)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from telethon import TelegramClient, errors, functions, utils
from telethon.sessions import StringSession
from telethon.tl import types

from telegram_mcp.gateway.models import (
    PREVIEW_LIMIT,
    BadCode,
    BadPassword,
    ChatNotFound,
    ChatRef,
    ChatType,
    CodeDelivery,
    CodeExpired,
    FloodWait,
    GatewayError,
    GatewayTimeout,
    Me,
    NotAuthorized,
    OutcomeUnknown,
    PasswordNeeded,
    QrExpired,
    QrToken,
    SentCodeInfo,
    SentMessage,
    TelegramError,
    decode_cursor,
    encode_cursor,
    mark_saved_messages,
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
        # The account's own id, read once per connection to mark its Saved Messages (NFR4.4).
        self._own_id: int | None = None
        self._own_id_read = False
        self._qr: Any = None

    async def _call(
        self,
        make: Callable[[], Awaitable[T]],
        *,
        write: bool = False,
        extra_seconds: float = 0.0,
    ) -> T:
        """Run one Telegram call with the 30 s limit (plus ``extra_seconds`` for a call that
        waits on purpose, like a QR scan), mapping every failure to a gateway error. For a
        write, any failure after the connection is up is ``OutcomeUnknown``."""
        await self._ensure_connected()
        try:
            return await asyncio.wait_for(make(), self._timeout + extra_seconds)
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
        self._own_id_read = False
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

    async def sign_in_phone(self, phone: str) -> SentCodeInfo:
        sent = await self._call(lambda: self._client.send_code_request(phone))
        self._phone = phone
        self._phone_code_hash = sent.phone_code_hash
        return sent_code_info(sent)

    async def resend_code(self, phone: str) -> SentCodeInfo:
        """Ask for the code by the next way Telegram offered, with the code hash the first
        request returned; Telegram answers with a new hash for the new code."""
        code_hash = self._phone_code_hash or ""
        request = functions.auth.ResendCodeRequest(phone, code_hash)
        sent = await self._call(lambda: self._client(request))
        self._phone = phone
        self._phone_code_hash = getattr(sent, "phone_code_hash", None) or self._phone_code_hash
        return sent_code_info(sent)

    async def sign_in_code(self, code: str) -> Me:
        phone, code_hash = self._phone, self._phone_code_hash
        await self._call(lambda: self._client.sign_in(phone, code, phone_code_hash=code_hash))
        return await self.me()

    async def sign_in_password(self, password: str) -> Me:
        await self._call(lambda: self._client.sign_in(password=password))
        return await self.me()

    async def sign_in_qr(self) -> QrToken:
        """The first call starts Telethon's QR login; each later call asks for a new token."""

        async def token() -> Any:
            if self._qr is None:
                self._qr = await self._client.qr_login()
            else:
                await self._qr.recreate()
            return self._qr

        qr = await self._call(token)
        return QrToken(url=str(qr.url), expires_at=qr.expires)

    async def wait_qr(self, timeout_seconds: float) -> Me:
        qr = self._qr
        if qr is None:
            raise QrExpired()

        async def scanned() -> Any:
            try:
                return await qr.wait(timeout_seconds)
            except TimeoutError as error:
                raise QrExpired() from error

        user = await self._call(scanned, extra_seconds=max(0.0, timeout_seconds))
        return _me_from_user(user)

    async def log_out(self) -> bool:
        """``auth.logOut`` sent directly, so that a flood wait, an ended session and a timeout
        keep their own errors (Telethon's ``log_out()`` turns every refusal into False)."""
        await self._call(lambda: self._client(functions.auth.LogOutRequest()))
        return True

    def export_session(self) -> str:
        return str(self._client.session.save())

    async def me(self) -> Me:
        user = await self._call(self._client.get_me)
        if user is None:
            raise NotAuthorized()
        return _me_from_user(user)

    async def _account_id(self) -> int | None:
        """The logged-in account's own id, read with ``me()`` once per connection. None when
        it cannot be read, so that no chat is marked as the person's Saved Messages."""
        await self._ensure_connected()
        if not self._own_id_read:
            try:
                self._own_id = (await self.me()).id
            except GatewayError:
                return None
            self._own_id_read = True
        return self._own_id

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
        own_id = await self._account_id()
        chats = [mark_saved_messages(_chat_from_dialog(d), own_id) for d in dialogs]
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
        chat = ChatRef(
            id=int(utils.get_peer_id(entity)),
            name=utils.get_display_name(entity),
            type=_chat_type(entity),
        )
        return mark_saved_messages(chat, await self._account_id())

    async def send_text(
        self, chat_id: int, text: str, reply_to: int | None, topic_id: int | None
    ) -> SentMessage:
        target = reply_to if reply_to is not None else topic_id
        message = await self._call(
            lambda: self._client.send_message(chat_id, text, reply_to=target, parse_mode=None),
            write=True,
        )
        return SentMessage(id=int(message.id), chat_id=chat_id, date=message.date)


_SENT_TYPES: tuple[tuple[type[Any], CodeDelivery], ...] = (
    (types.auth.SentCodeTypeApp, "app"),
    (types.auth.SentCodeTypeSms, "sms"),
    (types.auth.SentCodeTypeSmsWord, "sms"),
    (types.auth.SentCodeTypeSmsPhrase, "sms"),
    (types.auth.SentCodeTypeFirebaseSms, "sms"),
    (types.auth.SentCodeTypeCall, "call"),
    (types.auth.SentCodeTypeFlashCall, "flash_call"),
    (types.auth.SentCodeTypeMissedCall, "missed_call"),
    (types.auth.SentCodeTypeEmailCode, "email"),
    (types.auth.SentCodeTypeFragmentSms, "fragment"),
)
_NEXT_TYPES: tuple[tuple[type[Any], CodeDelivery], ...] = (
    (types.auth.CodeTypeSms, "sms"),
    (types.auth.CodeTypeCall, "call"),
    (types.auth.CodeTypeFlashCall, "flash_call"),
    (types.auth.CodeTypeMissedCall, "missed_call"),
    (types.auth.CodeTypeFragmentSms, "fragment"),
)


def _delivery(value: Any, table: tuple[tuple[type[Any], CodeDelivery], ...]) -> CodeDelivery:
    for telethon_type, delivery in table:
        if isinstance(value, telethon_type):
            return delivery
    return "other"


def sent_code_info(sent: Any) -> SentCodeInfo:
    """Telegram's ``auth.SentCode`` as where the code went and the next way offered (BR1.10)."""
    code_type = getattr(sent, "type", None)
    next_type = getattr(sent, "next_type", None)
    pattern = getattr(code_type, "email_pattern", None)
    timeout = getattr(sent, "timeout", None)
    return SentCodeInfo(
        delivery=_delivery(code_type, _SENT_TYPES),
        email_pattern=str(pattern) if pattern else None,
        next_delivery=_delivery(next_type, _NEXT_TYPES) if next_type is not None else None,
        timeout_seconds=int(timeout) if isinstance(timeout, int) else None,
    )


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


def _me_from_user(user: Any) -> Me:
    return Me(id=int(user.id), name=utils.get_display_name(user), username=user.username)


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
