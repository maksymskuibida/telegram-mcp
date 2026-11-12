"""An in-memory Telegram gateway for tests. It records every call, in order, and can be told
to fail. Only ``mark_read`` changes read state; no read call acknowledges reading (C3
FakeGateway, BR13.1)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Literal, cast

from telegram_mcp.gateway.models import (
    BadCode,
    BadPassword,
    ChatDetails,
    ChatType,
    ChatNotFound,
    ChatRef,
    CodeExpired,
    FloodWait,
    FolderRef,
    GatewayError,
    GatewayTimeout,
    MediaRef,
    Me,
    MessageView,
    NotAuthorized,
    NotAUser,
    OutcomeUnknown,
    OwnProfile,
    PasswordNeeded,
    PersonInfo,
    QrExpired,
    QrToken,
    Role,
    SearchHit,
    Sender,
    SentCodeInfo,
    SentMessage,
    TelegramError,
    TooLarge,
    TopicInfo,
    TopicNotFound,
    UnreadState,
    UserNotFound,
    decode_cursor,
    encode_cursor,
    mark_saved_messages,
)

ADMIN_RIGHT_NAMES = (
    "change_info",
    "post_messages",
    "edit_messages",
    "delete_messages",
    "ban_users",
    "invite_users",
    "pin_messages",
    "add_admins",
    "manage_call",
    "anonymous",
    "manage_topics",
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
    after_bytes: int | None = None
    """For download_media: write this many bytes, then raise ``error``."""


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
    sender_id: int = 7001
    outgoing: bool = False
    edited: bool = False
    reply_to: int | None = None
    forwarded_from: str | None = None
    file_name: str | None = None
    media_size: int = 0
    pinned: bool = False

    def view(self) -> MessageView:
        media = None
        if self.media is not None:
            media = MediaRef(kind=self.media, size_bytes=self.media_size, file_name=self.file_name)
        return MessageView(
            id=self.id,
            chat_id=self.chat_id,
            date=self.date,
            sender=Sender(self.sender_id, self.sender),
            outgoing=self.outgoing,
            text=self.text,
            reply_to=self.reply_to,
            topic_id=self.topic_id,
            media=media,
            edited=self.edited,
            forwarded_from=self.forwarded_from,
        )


@dataclass
class ChatExtra:
    """What chat_info returns beyond a ChatRef, per seeded chat."""

    username: str | None = None
    description: str | None = None
    member_count: int | None = None
    my_role: Role = "member"


@dataclass
class SentFile:
    chat_id: int
    name: str
    caption: str
    data: bytes
    message: SentMessage


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
    folder_list: list[FolderRef] = field(
        default_factory=lambda: [FolderRef(0, "Main list"), FolderRef(1, "Archive")]
    )
    extras: dict[int, ChatExtra] = field(default_factory=dict)
    """Per chat: username, description, member count and the account's role."""
    users: dict[int, PersonInfo] = field(default_factory=dict)
    """Every user the account can see, contacts included."""
    members_of: dict[int, list[int]] = field(default_factory=dict)
    roles: dict[tuple[int, int], Literal["member", "admin", "creator"]] = field(
        default_factory=dict
    )
    rights: dict[tuple[int, int], dict[str, bool]] = field(default_factory=dict)
    profile: OwnProfile | None = None
    media_bytes: dict[tuple[int, int], bytes] = field(default_factory=dict)
    invite_targets: dict[str, int] = field(default_factory=dict)
    """Invite hashes this fake accepts, and the chat each one joins."""
    sent_files: list[SentFile] = field(default_factory=list)
    reactions: list[tuple[int, int, str]] = field(default_factory=list)
    invites: list[str] = field(default_factory=list)
    profile_photos: list[bytes] = field(default_factory=list)
    chat_photos: list[tuple[int, bytes]] = field(default_factory=list)
    _next_message_id: int = 1000
    _next_seeded_id: int = 0
    _next_chat_id: int = -1000000009000

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
        after_bytes: int | None = None,
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
                after_bytes=after_bytes,
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
        outgoing: bool = False,
        file_name: str | None = None,
        media_size: int = 0,
        content: bytes | None = None,
        sender_id: int = 7001,
        pinned: bool = False,
    ) -> FakeMessage:
        """Seed an incoming message; it is unread until ``mark_read`` covers its id.
        ``content`` is the media's bytes for a download."""
        self._next_seeded_id += 1
        if content is not None:
            media_size = len(content)
        message = FakeMessage(
            id=self._next_seeded_id,
            chat_id=chat_id,
            sender="Me" if outgoing else sender,
            date=date or datetime(2026, 11, 11, 9, 0, tzinfo=UTC),
            text=text,
            media=media,
            topic_id=topic_id,
            sender_id=self.account.id if outgoing else sender_id,
            outgoing=outgoing,
            file_name=file_name,
            media_size=media_size,
            pinned=pinned,
        )
        self.messages.append(message)
        if content is not None:
            self.media_bytes[(chat_id, message.id)] = content
        return message

    def add_user(
        self,
        user_id: int,
        name: str,
        *,
        username: str | None = None,
        phone: str | None = None,
        bio: str | None = None,
        is_bot: bool = False,
        is_contact: bool = False,
    ) -> PersonInfo:
        person = PersonInfo(
            id=user_id,
            name=name,
            username=username,
            phone=phone,
            bio=bio,
            is_bot=is_bot,
            is_contact=is_contact,
        )
        self.users[user_id] = person
        return person

    def add_member(
        self,
        chat_id: int,
        user_id: int,
        role: Literal["member", "admin", "creator"] = "member",
        rights: dict[str, bool] | None = None,
    ) -> None:
        self.members_of.setdefault(chat_id, []).append(user_id)
        self.roles[(chat_id, user_id)] = role
        if rights is not None:
            self.rights[(chat_id, user_id)] = dict(rights)

    def message(self, chat_id: int, message_id: int) -> FakeMessage | None:
        for item in self.messages:
            if item.chat_id == chat_id and item.id == message_id:
                return item
        return None

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
        self,
        chat_id: int,
        limit: int,
        before_id: int | None = None,
        topic_id: int | None = None,
        ids: list[int] | None = None,
    ) -> list[MessageView]:
        """A page newest first, or the messages with ``ids``; marks nothing (C3)."""
        await self._enter(
            "read_messages",
            chat_id=chat_id,
            limit=limit,
            before_id=before_id,
            topic_id=topic_id,
            ids=list(ids) if ids is not None else None,
        )
        self._require_authorized()
        self._require_chat(chat_id)
        self._require_topic(chat_id, topic_id)
        inside = self._in(chat_id, topic_id)
        if ids is not None:
            found = [m for m in inside if m.id in ids]
        else:
            found = [m for m in inside if before_id is None or m.id < before_id]
        found.sort(key=lambda m: m.id, reverse=True)
        return [m.view() for m in found[:limit]]

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

    async def _enter(self, name: str, /, **args: Any) -> Script | None:
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
            if script.error is not None and script.after_bytes is None:
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

    # U4 reads (none acknowledges reading) --------------------------------------------------

    def _chat(self, chat_id: int) -> ChatRef:
        for chat in self.chats:
            if chat.id == chat_id:
                return mark_saved_messages(chat, self._own_id())
        raise ChatNotFound()

    def _require_chat(self, chat_id: int) -> None:
        self._chat(chat_id)

    def _details(self, chat: ChatRef) -> ChatDetails:
        extra = self.extras.get(chat.id, ChatExtra())
        pinned = [m.id for m in self.messages if m.chat_id == chat.id and m.pinned]
        role: Role = "none" if chat.type == "user" else extra.my_role
        return ChatDetails(
            id=chat.id,
            name=chat.name,
            type=chat.type,
            username=extra.username,
            description=extra.description,
            member_count=extra.member_count,
            unread=chat.unread,
            muted=chat.muted,
            is_forum=chat.type == "forum",
            my_role=role,
            pinned_message_id=max(pinned) if pinned else None,
            saved_messages=chat.saved_messages,
        )

    async def folders(self) -> list[FolderRef]:
        await self._enter("folders")
        self._require_authorized()
        return list(self.folder_list)

    async def chat_info(self, chat: int | str) -> ChatDetails:
        await self._enter("chat_info", chat=chat)
        self._require_authorized()
        if isinstance(chat, int):
            return self._details(self._chat(chat))
        wanted = chat.lower()
        for chat_id, extra in self.extras.items():
            if extra.username is not None and extra.username.lower() == wanted:
                return self._details(self._chat(chat_id))
        for person in self.users.values():
            if person.username is not None and person.username.lower() == wanted:
                return ChatDetails(
                    id=person.id, name=person.name, type="user", username=person.username
                )
        raise ChatNotFound()

    async def topics_list(self, chat_id: int, limit: int) -> list[TopicInfo]:
        await self._enter("topics_list", chat_id=chat_id, limit=limit)
        self._require_authorized()
        self._require_chat(chat_id)
        found = []
        for topic_id, title in self.topics.get(chat_id, {}).items():
            upto = max(
                self.read_upto.get((chat_id, None), 0), self.read_upto.get((chat_id, topic_id), 0)
            )
            inside = self._in(chat_id, topic_id)
            unread = sum(1 for m in inside if m.id > upto)
            newest = max((m.id for m in inside), default=0)
            found.append((newest, TopicInfo(topic_id=topic_id, title=title, unread=unread)))
        found.sort(key=lambda pair: pair[0], reverse=True)
        return [topic for _, topic in found[:limit]]

    async def read_pinned(self, chat_id: int, limit: int) -> list[MessageView]:
        await self._enter("read_pinned", chat_id=chat_id, limit=limit)
        self._require_authorized()
        self._require_chat(chat_id)
        found = sorted(
            (m for m in self._in(chat_id, None) if m.pinned), key=lambda m: m.id, reverse=True
        )
        return [m.view() for m in found[:limit]]

    async def search_chat(self, chat_id: int, query: str, limit: int) -> list[MessageView]:
        await self._enter("search_chat", chat_id=chat_id, query=query, limit=limit)
        self._require_authorized()
        self._require_chat(chat_id)
        found = [m for m in self._in(chat_id, None) if query.casefold() in m.text.casefold()]
        found.sort(key=lambda m: m.id, reverse=True)
        return [m.view() for m in found[:limit]]

    async def search_global(self, query: str, limit: int) -> list[SearchHit]:
        await self._enter("search_global", query=query, limit=limit)
        self._require_authorized()
        known = {chat.id for chat in self.chats}
        found = [
            m for m in self.messages if m.chat_id in known and query.casefold() in m.text.casefold()
        ]
        found.sort(key=lambda m: m.id, reverse=True)
        return [SearchHit(chat=self._chat(m.chat_id), message=m.view()) for m in found[:limit]]

    async def members(self, chat_id: int, filter: str, limit: int) -> list[PersonInfo]:
        await self._enter("members", chat_id=chat_id, filter=filter, limit=limit)
        self._require_authorized()
        self._require_chat(chat_id)
        found = []
        for user_id in self.members_of.get(chat_id, []):
            person = self.users.get(user_id, PersonInfo(id=user_id, name=f"User {user_id}"))
            role = self.roles.get((chat_id, user_id), "member")
            if filter == "admins" and role == "member":
                continue
            if filter == "bots" and not person.is_bot:
                continue
            rights = None
            if role != "member":
                rights = tuple(sorted(self._rights(chat_id, user_id).items()))
            found.append(replace(person, role=role, admin_rights=rights))
        return found[:limit]

    def _rights(self, chat_id: int, user_id: int) -> dict[str, bool]:
        current = self.rights.get((chat_id, user_id), {})
        return {name: bool(current.get(name, False)) for name in ADMIN_RIGHT_NAMES}

    async def admins(self, chat_id: int, user_id: int) -> dict[str, bool]:
        await self._enter("admins", chat_id=chat_id, user_id=user_id)
        self._require_authorized()
        self._require_chat(chat_id)
        return self._rights(chat_id, user_id)

    async def contacts(self) -> list[PersonInfo]:
        await self._enter("contacts")
        self._require_authorized()
        return [person for person in self.users.values() if person.is_contact]

    async def user_profile(self, users: list[int] | str) -> list[PersonInfo]:
        await self._enter("user_profile", users=users)
        self._require_authorized()
        if isinstance(users, str):
            wanted = users.lower()
            for person in self.users.values():
                if person.username is not None and person.username.lower() == wanted:
                    return [person]
            if any(
                e.username is not None and e.username.lower() == wanted
                for e in self.extras.values()
            ):
                raise NotAUser()
            raise UserNotFound(username=users)
        missing = tuple(user_id for user_id in users if user_id not in self.users)
        if missing:
            raise UserNotFound(ids=missing)
        return [self.users[user_id] for user_id in users]

    async def own_profile(self) -> OwnProfile:
        await self._enter("own_profile")
        self._require_authorized()
        return self._profile()

    def _profile(self) -> OwnProfile:
        if self.profile is None:
            self.profile = OwnProfile(
                id=self.account.id, first_name=self.account.name, username=self.account.username
            )
        return self.profile

    async def download_media(
        self, chat_id: int, message_id: int, file_obj: BinaryIO, max_bytes: int
    ) -> int:
        script = await self._enter(
            "download_media", chat_id=chat_id, message_id=message_id, max_bytes=max_bytes
        )
        self._require_authorized()
        message = self.message(chat_id, message_id)
        if message is None or message.media is None:
            raise TelegramError("MEDIA_EMPTY")
        if message.media_size > max_bytes:
            raise TooLarge(message.media_size)
        data = self.media_bytes.get((chat_id, message_id), b"\0" * message.media_size)
        if script is not None and script.after_bytes is not None:
            file_obj.write(data[: script.after_bytes])
            raise script.error or TelegramError("download failed")
        file_obj.write(data)
        return len(data)

    # U4 writes ---------------------------------------------------------------------------

    async def _write(self, name: str, /, **args: Any) -> Script | None:
        script = await self._enter(name, **args)
        self._require_authorized()
        return script

    @staticmethod
    def _sent(script: Script | None) -> None:
        if script is not None and script.drop_after_send:
            raise OutcomeUnknown()

    def _replace_message(self, old: FakeMessage, new: FakeMessage) -> None:
        self.messages[self.messages.index(old)] = new

    async def edit(self, chat_id: int, message_id: int, text: str) -> MessageView:
        script = await self._write("edit", chat_id=chat_id, message_id=message_id, text=text)
        found = self.message(chat_id, message_id)
        if found is None:
            raise TelegramError("MESSAGE_ID_INVALID")
        changed = replace(found, text=text, edited=True)
        self._replace_message(found, changed)
        self._sent(script)
        return changed.view()

    async def delete(self, chat_id: int, message_ids: list[int], for_everyone: bool) -> None:
        script = await self._write(
            "delete", chat_id=chat_id, message_ids=list(message_ids), for_everyone=for_everyone
        )
        self._require_chat(chat_id)
        self.messages = [
            m for m in self.messages if not (m.chat_id == chat_id and m.id in message_ids)
        ]
        self._sent(script)

    async def forward(self, from_chat: int, message_ids: list[int], to_chat: int) -> list[int]:
        script = await self._write(
            "forward", from_chat=from_chat, message_ids=list(message_ids), to_chat=to_chat
        )
        self._require_chat(to_chat)
        new_ids = []
        for message_id in message_ids:
            original = self.message(from_chat, message_id)
            if original is None:
                raise TelegramError("MESSAGE_ID_INVALID")
            copy = self.add_message(to_chat, original.text, outgoing=True)
            self._replace_message(copy, replace(copy, forwarded_from=original.sender))
            new_ids.append(copy.id)
        self._sent(script)
        return new_ids

    async def react(self, chat_id: int, message_id: int, emoji: str) -> None:
        script = await self._write("react", chat_id=chat_id, message_id=message_id, emoji=emoji)
        self.reactions.append((chat_id, message_id, emoji))
        self._sent(script)

    async def _set_pinned(self, name: str, chat_id: int, message_id: int, pinned: bool) -> None:
        script = await self._write(name, chat_id=chat_id, message_id=message_id)
        found = self.message(chat_id, message_id)
        if found is None:
            raise TelegramError("MESSAGE_ID_INVALID")
        self._replace_message(found, replace(found, pinned=pinned))
        self._sent(script)

    async def pin(self, chat_id: int, message_id: int) -> None:
        await self._set_pinned("pin", chat_id, message_id, True)

    async def unpin(self, chat_id: int, message_id: int) -> None:
        await self._set_pinned("unpin", chat_id, message_id, False)

    async def topic_create(self, chat_id: int, title: str) -> int:
        script = await self._write("topic_create", chat_id=chat_id, title=title)
        topics = self.topics.setdefault(chat_id, {})
        topic_id = max(topics, default=1) + 1
        topics[topic_id] = title
        self._sent(script)
        return topic_id

    def _new_chat(self, title: str, kind: ChatType) -> int:
        self._next_chat_id -= 1
        self.chats.append(ChatRef(id=self._next_chat_id, name=title, type=kind))
        self.extras[self._next_chat_id] = ChatExtra(my_role="creator")
        return self._next_chat_id

    async def create_group(self, title: str, members: list[int], supergroup: bool) -> int:
        script = await self._write(
            "create_group", title=title, members=list(members), supergroup=supergroup
        )
        chat_id = self._new_chat(title, "supergroup" if supergroup else "group")
        for user_id in members:
            self.add_member(chat_id, user_id)
        self._sent(script)
        return chat_id

    async def create_channel(self, title: str) -> int:
        script = await self._write("create_channel", title=title)
        chat_id = self._new_chat(title, "channel")
        self._sent(script)
        return chat_id

    async def join(self, username: str | None, invite_hash: str | None) -> ChatRef:
        script = await self._write("join", username=username, invite_hash=invite_hash)
        chat_id: int | None = None
        if username is not None:
            for candidate, extra in self.extras.items():
                if extra.username is not None and extra.username.lower() == username.lower():
                    chat_id = candidate
        elif invite_hash is not None:
            chat_id = self.invite_targets.get(invite_hash)
        if chat_id is None:
            raise TelegramError("INVITE_HASH_INVALID" if invite_hash else "USERNAME_NOT_OCCUPIED")
        self.extras.setdefault(chat_id, ChatExtra()).my_role = "member"
        self._sent(script)
        return self._chat(chat_id)

    async def leave(self, chat_id: int) -> None:
        script = await self._write("leave", chat_id=chat_id)
        self._require_chat(chat_id)
        self.extras.setdefault(chat_id, ChatExtra()).my_role = "none"
        self._sent(script)

    async def invite_link(
        self, chat_id: int, expire_seconds: int | None, usage_limit: int | None
    ) -> str:
        script = await self._write(
            "invite_link", chat_id=chat_id, expire_seconds=expire_seconds, usage_limit=usage_limit
        )
        self._require_chat(chat_id)
        link = f"https://t.me/+FakeInvite{len(self.invites) + 1:04d}"
        self.invites.append(link)
        self._sent(script)
        return link

    async def set_admin_rights(self, chat_id: int, user_id: int, rights: dict[str, bool]) -> None:
        script = await self._write(
            "set_admin_rights", chat_id=chat_id, user_id=user_id, rights=dict(rights)
        )
        self._require_chat(chat_id)
        self.rights[(chat_id, user_id)] = dict(rights)
        self.roles[(chat_id, user_id)] = "admin" if any(rights.values()) else "member"
        self._sent(script)

    async def edit_chat(
        self,
        chat_id: int,
        title: str | None,
        description: str | None,
        photo: BinaryIO | None,
        photo_name: str | None,
    ) -> None:
        script = await self._write(
            "edit_chat",
            chat_id=chat_id,
            title=title,
            description=description,
            photo_name=photo_name,
        )
        chat = self._chat(chat_id)
        if title is not None:
            self.chats[[c.id for c in self.chats].index(chat_id)] = replace(chat, name=title)
        if description is not None:
            self.extras.setdefault(chat_id, ChatExtra()).description = description
        if photo is not None:
            self.chat_photos.append((chat_id, photo.read()))
        self._sent(script)

    async def edit_own_profile(
        self,
        first_name: str | None,
        last_name: str | None,
        bio: str | None,
        username: str | None,
        photo: BinaryIO | None,
        photo_name: str | None,
    ) -> None:
        script = await self._write(
            "edit_own_profile",
            first_name=first_name,
            last_name=last_name,
            bio=bio,
            username=username,
            photo_name=photo_name,
        )
        current = self._profile()
        changes: dict[str, Any] = {
            name: value
            for name, value in (
                ("first_name", first_name),
                ("last_name", last_name),
                ("bio", bio),
                ("username", username),
            )
            if value is not None
        }
        if photo is not None:
            self.profile_photos.append(photo.read())
            changes["has_photo"] = True
        self.profile = replace(current, **changes)
        self._sent(script)

    async def send_file(
        self, chat_id: int, file_obj: BinaryIO, name: str, caption: str
    ) -> SentMessage:
        script = await self._write("send_file", chat_id=chat_id, name=name, caption=caption)
        self._require_chat(chat_id)
        data = file_obj.read()
        self._next_message_id += 1
        message = SentMessage(id=self._next_message_id, chat_id=chat_id, date=datetime.now(UTC))
        self.sent_files.append(SentFile(chat_id, name, caption, data, message))
        self._sent(script)
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
