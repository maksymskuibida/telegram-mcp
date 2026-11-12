"""An in-memory Telegram gateway for tests. It records every call, in order, and can be told
to fail. Only ``mark_read`` changes read state; no read call acknowledges reading (C3
FakeGateway, BR13.1)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from telegram_mcp.gateway.models import (
    BadCode,
    BadPassword,
    ChatNotFound,
    ChatRef,
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
    TopicNotFound,
    UnreadState,
    decode_cursor,
    encode_cursor,
    mark_saved_messages,
)

FAKE_CONFIG_NAME = "fake-gateway.json"
FAKE_RECORD_NAME = "fake-gateway-calls.jsonl"
READ_ACKNOWLEDGING_CALLS = frozenset({"mark_read"})


@dataclass
class Script:
    """What a scripted call does instead of succeeding. ``times`` None means every call.
    ``delay`` is awaited (with the fake's ``sleep``) before the call goes on or fails;
    ``result`` replaces the answer of a call that supports it (a malformed unread state)."""

    error: GatewayError | None = None
    hang: bool = False
    drop_after_send: bool = False
    times: int | None = 1
    delay: float = 0.0
    result: Any = None


@dataclass(frozen=True)
class FakeMessage:
    """A message seeded into a fake chat; ids grow per fake, so newer means a higher id."""

    id: int
    chat_id: int
    sender: str
    date: datetime
    text: str
    media: str | None = None
    topic_id: int | None = None


@dataclass
class SentText:
    message: SentMessage
    text: str
    reply_to: int | None
    topic_id: int | None


@dataclass
class FakeGateway:
    chats: list[ChatRef] = field(default_factory=list)
    authorized: bool = True
    account: Me = field(default_factory=lambda: Me(id=4242, name="Test Person", username="tester"))
    login_code: str = "12345"
    password: str | None = None
    session_after_login: str = "fake-session"
    sent_code: SentCodeInfo = field(default_factory=lambda: SentCodeInfo("app"))
    resent_code: SentCodeInfo = field(default_factory=lambda: SentCodeInfo("sms"))
    own_id_known: bool = True
    """False stands for a gateway that could not read the account's own id: no chat is marked."""
    record_path: Path | None = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    sent: list[SentText] = field(default_factory=list)
    scripts: dict[str, list[Script]] = field(default_factory=dict)
    connected: bool = False
    sleep: Callable[[float], Awaitable[None]] = field(default_factory=lambda: asyncio.sleep)
    """How a scripted delay waits; tests swap in a sleep that steps a fake clock."""
    qr_tokens: list[str] = field(default_factory=list)
    """The QR login links to hand out, in order; past the end, fake links are made up."""
    qr_lifetime_seconds: float = 30.0
    qr_scan_on_wait: int | None = None
    """The wait (1-based) during which a phone scans the code; None means nobody scans."""
    qr_time_passes: Callable[[float], None] | None = None
    """Told how long each wait that ends unscanned lasted, so a test clock can move on."""
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    log_out_result: bool = True
    qr_issued: int = 0
    qr_waits: int = 0
    messages: list[FakeMessage] = field(default_factory=list)
    """Seeded messages, oldest first. Their ids are the read state's unit."""
    topics: dict[int, dict[int, str]] = field(default_factory=dict)
    """Forum topics: chat id -> topic id -> title."""
    read_upto: dict[tuple[int, int | None], int] = field(default_factory=dict)
    """The newest message id read, per chat (topic None) and per forum topic."""
    _next_message_id: int = 1000
    _next_seeded_id: int = 0

    def script(
        self,
        call: str,
        *,
        error: GatewayError | None = None,
        hang: bool = False,
        drop_after_send: bool = False,
        times: int | None = 1,
        delay: float = 0.0,
        result: Any = None,
    ) -> None:
        """Make the next ``times`` calls of ``call`` wait ``delay`` seconds, then raise, hang,
        drop after sending, go on, or return ``result``."""
        self.scripts.setdefault(call, []).append(
            Script(
                error=error,
                hang=hang,
                drop_after_send=drop_after_send,
                times=times,
                delay=delay,
                result=result,
            )
        )

    # Seeded messages and read state (U2) ------------------------------------------------------

    def add_message(
        self,
        chat_id: int,
        text: str,
        *,
        sender: str = "Alice",
        topic_id: int | None = None,
        media: str | None = None,
        date: datetime | None = None,
    ) -> FakeMessage:
        """Seed an incoming message; it is unread until ``mark_read`` covers its id."""
        self._next_seeded_id += 1
        message = FakeMessage(
            id=self._next_seeded_id,
            chat_id=chat_id,
            sender=sender,
            date=date or datetime(2026, 11, 11, 9, 0, tzinfo=UTC),
            text=text,
            media=media,
            topic_id=topic_id,
        )
        self.messages.append(message)
        return message

    def mark_all_read(self) -> None:
        """Seed every chat as read so far, without recording a call (test set-up only)."""
        for message in self.messages:
            for key in ((message.chat_id, None), (message.chat_id, message.topic_id)):
                self.read_upto[key] = max(self.read_upto.get(key, 0), message.id)

    def read_acknowledgements(self) -> list[dict[str, Any]]:
        """Every call that changed read state. Only ``mark_read`` ever does (BR13.1)."""
        return [args for name, args in self.calls if name in READ_ACKNOWLEDGING_CALLS]

    def _in(self, chat_id: int, topic_id: int | None) -> list[FakeMessage]:
        return [
            m
            for m in self.messages
            if m.chat_id == chat_id and (topic_id is None or m.topic_id == topic_id)
        ]

    def _require_topic(self, chat_id: int, topic_id: int | None) -> None:
        if topic_id is not None and topic_id not in self.topics.get(chat_id, {}):
            raise TopicNotFound()

    async def get_messages(self, chat_id: int, ids: list[int]) -> list[FakeMessage]:
        """Fetch messages by id for a question; marks nothing (test describers only)."""
        await self._enter("get_messages", chat_id=chat_id, ids=list(ids))
        self._require_authorized()
        return [m for m in self._in(chat_id, None) if m.id in ids]

    async def read_messages(
        self, chat_id: int, limit: int, topic_id: int | None, before_id: int | None = None
    ) -> list[FakeMessage]:
        """The newest ``limit`` messages, oldest first; marks nothing (test reads only)."""
        await self._enter(
            "read_messages", chat_id=chat_id, limit=limit, topic_id=topic_id, before_id=before_id
        )
        self._require_authorized()
        found = [m for m in self._in(chat_id, topic_id) if before_id is None or m.id < before_id]
        return found[-limit:]

    async def record_write(self, tool: str, **args: Any) -> None:
        """A test-only write records itself here so tests can count runs."""
        await self._enter("record_write", tool=tool, **args)
        self._require_authorized()

    async def unread_state(self, chat_id: int, topic_id: int | None) -> UnreadState:
        script = await self._enter("unread_state", chat_id=chat_id, topic_id=topic_id)
        self._require_authorized()
        if script is not None and script.result is not None:
            return cast(UnreadState, script.result)
        if not any(chat.id == chat_id for chat in self.chats):
            raise ChatNotFound()
        self._require_topic(chat_id, topic_id)
        upto = self.read_upto.get((chat_id, None), 0)
        if topic_id is not None:
            upto = max(upto, self.read_upto.get((chat_id, topic_id), 0))
        inside = self._in(chat_id, topic_id)
        unread = sum(1 for m in inside if m.id > upto)
        title = self.topics[chat_id][topic_id] if topic_id is not None else None
        return UnreadState(
            unread=unread, newest_id=max((m.id for m in inside), default=0), topic_title=title
        )

    async def mark_read(self, chat_id: int, max_id: int, topic_id: int | None) -> None:
        await self._enter("mark_read", chat_id=chat_id, max_id=max_id, topic_id=topic_id)
        self._require_authorized()
        self._require_topic(chat_id, topic_id)
        key = (chat_id, topic_id)
        self.read_upto[key] = max(self.read_upto.get(key, 0), max_id)

    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]

    async def _enter(self, name: str, **args: Any) -> Script | None:
        self.calls.append((name, args))
        if self.record_path is not None:
            with self.record_path.open("a", encoding="utf-8") as record:
                record.write(json.dumps({"call": name, "args": args}, default=str) + "\n")
        script = self._take_script(name)
        if script is not None:
            if script.delay > 0:
                await self.sleep(script.delay)
            if script.hang:
                await asyncio.Event().wait()
            if script.error is not None:
                raise script.error
        return script

    def _take_script(self, name: str) -> Script | None:
        queue = self.scripts.get(name)
        if not queue:
            return None
        script = queue[0]
        if script.times is not None:
            script.times -= 1
            if script.times <= 0:
                queue.pop(0)
        return script

    async def connect(self) -> None:
        await self._enter("connect")
        self.connected = True

    async def disconnect(self) -> None:
        await self._enter("disconnect")
        self.connected = False

    async def is_authorized(self) -> bool:
        await self._enter("is_authorized")
        return self.authorized

    async def sign_in_phone(self, phone: str) -> SentCodeInfo:
        await self._enter("sign_in_phone", phone=phone)
        return self.sent_code

    async def resend_code(self, phone: str) -> SentCodeInfo:
        await self._enter("resend_code", phone=phone)
        return self.resent_code

    async def sign_in_code(self, code: str) -> Me:
        await self._enter("sign_in_code")
        if code != self.login_code:
            raise BadCode()
        if self.password is not None:
            raise PasswordNeeded()
        self.authorized = True
        return self.account

    async def sign_in_password(self, password: str) -> Me:
        await self._enter("sign_in_password")
        if password != self.password:
            raise BadPassword()
        self.authorized = True
        return self.account

    async def sign_in_qr(self) -> QrToken:
        await self._enter("sign_in_qr")
        index = self.qr_issued
        self.qr_issued += 1
        if index < len(self.qr_tokens):
            url = self.qr_tokens[index]
        else:
            url = f"tg://login?token=FAKE{index:04d}"
        expires = self.clock() + timedelta(seconds=self.qr_lifetime_seconds)
        return QrToken(url=url, expires_at=expires)

    async def wait_qr(self, timeout_seconds: float) -> Me:
        await self._enter("wait_qr", timeout_seconds=timeout_seconds)
        self.qr_waits += 1
        if self.qr_scan_on_wait is None or self.qr_waits < self.qr_scan_on_wait:
            if self.qr_time_passes is not None:
                self.qr_time_passes(timeout_seconds)
            raise QrExpired()
        if self.password is not None:
            raise PasswordNeeded()
        self.authorized = True
        return self.account

    async def log_out(self) -> bool:
        await self._enter("log_out")
        if self.log_out_result:
            self.authorized = False
        return self.log_out_result

    def export_session(self) -> str:
        return self.session_after_login

    async def me(self) -> Me:
        await self._enter("me")
        self._require_authorized()
        return self.account

    async def list_chats(
        self, folder_id: int | None, limit: int, offset: str | None
    ) -> tuple[list[ChatRef], str | None]:
        await self._enter("list_chats", folder_id=folder_id, limit=limit, offset=offset)
        self._require_authorized()
        visible = [c for c in self.chats if folder_id is None or folder_id in c.folder_ids]
        start = decode_cursor(offset)[0] if offset else 0
        page = [mark_saved_messages(c, self._own_id()) for c in visible[start : start + limit]]
        more = start + limit < len(visible)
        return page, encode_cursor(start + limit, 0, 0) if more else None

    async def resolve_chat(self, chat_id: int) -> ChatRef:
        await self._enter("resolve_chat", chat_id=chat_id)
        self._require_authorized()
        for chat in self.chats:
            if chat.id == chat_id:
                return mark_saved_messages(chat, self._own_id())
        raise ChatNotFound()

    def _own_id(self) -> int | None:
        return self.account.id if self.own_id_known else None

    async def send_text(
        self, chat_id: int, text: str, reply_to: int | None, topic_id: int | None
    ) -> SentMessage:
        script = await self._enter(
            "send_text", chat_id=chat_id, text=text, reply_to=reply_to, topic_id=topic_id
        )
        self._require_authorized()
        self._next_message_id += 1
        message = SentMessage(id=self._next_message_id, chat_id=chat_id, date=datetime.now(UTC))
        self.sent.append(SentText(message, text, reply_to, topic_id))
        if script is not None and script.drop_after_send:
            raise OutcomeUnknown()
        return message

    def _require_authorized(self) -> None:
        if not self.authorized:
            raise NotAuthorized()

    @classmethod
    def from_file(cls, path: Path) -> FakeGateway:
        """Build a fake from a JSON file (used when the broker runs in a test subprocess).
        Every call is appended to ``fake-gateway-calls.jsonl`` beside it."""
        spec: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        fake = cls(
            chats=[_chat_from_json(c) for c in spec.get("chats", [])],
            authorized=bool(spec.get("authorized", True)),
            record_path=path.with_name(FAKE_RECORD_NAME),
        )
        for call, how in spec.get("scripts", {}).items():
            fake.script(
                call,
                error=_error_from_name(how.get("error")),
                hang=bool(how.get("hang", False)),
                drop_after_send=bool(how.get("drop_after_send", False)),
                times=how.get("times"),
                delay=float(how.get("delay", 0.0)),
            )
        return fake


def _chat_from_json(raw: dict[str, Any]) -> ChatRef:
    return ChatRef(
        id=int(raw["id"]),
        name=str(raw["name"]),
        type=raw["type"],
        unread=int(raw.get("unread", 0)),
        muted=bool(raw.get("muted", False)),
        last_preview=raw.get("last_preview"),
        folder_ids=tuple(raw.get("folder_ids", ())),
    )


def _error_from_name(name: str | None) -> GatewayError | None:
    if name is None:
        return None
    kind, _, detail = name.partition(":")
    if kind == "flood_wait":
        return FloodWait(int(detail or "0"))
    if kind == "telegram_error":
        return TelegramError(detail or "error")
    simple: dict[str, type[GatewayError]] = {
        "not_authorized": NotAuthorized,
        "timeout": GatewayTimeout,
        "outcome_unknown": OutcomeUnknown,
        "chat_not_found": ChatNotFound,
        "code_expired": CodeExpired,
    }
    return simple[kind]()
