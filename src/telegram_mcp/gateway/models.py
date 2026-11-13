"""Data shapes and errors of the Telegram gateway (C3)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal

ChatType = Literal["user", "group", "supergroup", "channel", "forum"]
CHAT_TYPES: tuple[ChatType, ...] = ("user", "group", "supergroup", "channel", "forum")
PREVIEW_LIMIT = 200
SAVED_MESSAGES = "Saved Messages"


@dataclass(frozen=True)
class ChatRef:
    """A chat as the gateway returns it. ``name`` and ``last_preview`` are Telegram content.

    ``saved_messages`` is true only for the logged-in account's own chat, decided by the
    gateway from the account's own id and never from a title (NFR4.4)."""

    id: int
    name: str
    type: ChatType
    unread: int = 0
    muted: bool = False
    last_preview: str | None = None
    folder_ids: tuple[int, ...] = ()
    saved_messages: bool = False


def mark_saved_messages(chat: ChatRef, own_id: int | None) -> ChatRef:
    """The chat as the gateway returns it: the account's own chat marked and named
    "Saved Messages", every other chat unmarked whatever it is called. With no own id, no
    chat is marked."""
    if own_id is not None and chat.id == own_id:
        return replace(chat, name=SAVED_MESSAGES, saved_messages=True)
    return replace(chat, saved_messages=False) if chat.saved_messages else chat


CodeDelivery = Literal[
    "app", "sms", "call", "flash_call", "missed_call", "email", "fragment", "other"
]


@dataclass(frozen=True)
class SentCodeInfo:
    """Where Telegram says it sent a login code, and the next way it offers, if any (BR1.10).
    ``email_pattern`` is the masked address Telegram gives for email delivery."""

    delivery: CodeDelivery
    email_pattern: str | None = None
    next_delivery: CodeDelivery | None = None
    timeout_seconds: int | None = None


@dataclass(frozen=True)
class SentMessage:
    id: int
    chat_id: int
    date: datetime


@dataclass(frozen=True)
class UnreadState:
    """The unread check before a mark (C3 unread_state): Telegram's unread count for the chat,
    or for one forum topic, and the id of its newest message."""

    unread: int
    newest_id: int
    topic_title: str | None = None
    """The topic's title when a topic was asked for, so the question can name it."""


@dataclass(frozen=True)
class Me:
    """The logged-in account, as login prints it."""

    id: int
    name: str
    username: str | None


def account_label(me: Me) -> str:
    """How every command names the account: name, @username and id, never the phone number."""
    handle = f"@{me.username}, " if me.username else ""
    return f"{me.name} ({handle}id {me.id})"


@dataclass(frozen=True)
class QrToken:
    """One QR login token. ``url`` (``tg://login?token=…``) is a login secret: it is shown on
    the terminal only and never appears in a repr, a log or an error."""

    url: str = field(repr=False)
    expires_at: datetime


Role = Literal["member", "admin", "creator", "none"]
MediaKind = Literal["photo", "video", "voice", "audio", "document", "sticker", "other"]


@dataclass(frozen=True)
class ChatDetails:
    """What tg_chats_info returns about one chat (C3 chat_info). ``name`` and ``description``
    are Telegram content."""

    id: int
    name: str
    type: ChatType
    username: str | None = None
    description: str | None = None
    member_count: int | None = None
    unread: int = 0
    muted: bool = False
    is_forum: bool = False
    my_role: Role = "none"
    pinned_message_id: int | None = None
    saved_messages: bool = False

    def ref(self) -> ChatRef:
        return ChatRef(
            id=self.id,
            name=self.name,
            type=self.type,
            unread=self.unread,
            muted=self.muted,
            saved_messages=self.saved_messages,
        )


@dataclass(frozen=True)
class FolderRef:
    """One chat folder: 0 is the main list, 1 the archive."""

    id: int
    title: str


@dataclass(frozen=True)
class TopicInfo:
    """One forum topic (C3 topics_list)."""

    topic_id: int
    title: str
    unread: int = 0
    closed: bool = False
    pinned: bool = False


@dataclass(frozen=True)
class MediaRef:
    """The file a message carries (C3 MediaRef); ``file_name`` is Telegram content."""

    kind: str
    size_bytes: int
    file_name: str | None = None


@dataclass(frozen=True)
class Sender:
    id: int
    name: str


@dataclass(frozen=True)
class MessageView:
    """One message as a read returns it. ``text``, the sender's name, the media file name and
    ``forwarded_from`` are Telegram content."""

    id: int
    chat_id: int
    date: datetime
    sender: Sender | None
    outgoing: bool
    text: str
    reply_to: int | None = None
    topic_id: int | None = None
    media: MediaRef | None = None
    edited: bool = False
    forwarded_from: str | None = None


@dataclass(frozen=True)
class SearchHit:
    """One message found by a search across chats, with its chat."""

    chat: ChatRef
    message: MessageView


@dataclass(frozen=True)
class PersonInfo:
    """A user as members, contacts and user profiles return them."""

    id: int
    name: str
    username: str | None = None
    phone: str | None = None
    bio: str | None = None
    is_bot: bool = False
    is_contact: bool = False
    role: Literal["member", "admin", "creator"] | None = None
    admin_rights: tuple[tuple[str, bool], ...] | None = None


@dataclass(frozen=True)
class OwnProfile:
    """The person's own profile: nothing about sessions, privacy or login methods (FR6.3)."""

    id: int
    first_name: str
    last_name: str | None = None
    username: str | None = None
    bio: str | None = None
    phone: str | None = None
    has_photo: bool = False


@dataclass(frozen=True)
class ChatCandidate:
    """One chat a lookup matched, so the AI can name it by id. ``name`` is Telegram content."""

    chat_id: int
    name: str
    type: ChatType
    username: str | None
    match: Literal["id", "username", "exact_title", "partial_title"]


class GatewayError(Exception):
    """Base of every error the gateway raises."""


class TooLarge(GatewayError):
    """A file is larger than the tool moves (BR26, NFR11.5)."""

    def __init__(self, size_bytes: int) -> None:
        super().__init__(f"the file is {size_bytes} bytes")
        self.size_bytes = size_bytes


class UserNotFound(GatewayError):
    """The account cannot see these users (or this username names nobody)."""

    def __init__(self, ids: tuple[int, ...] = (), username: str | None = None) -> None:
        super().__init__("user not found")
        self.ids = ids
        self.username = username


class NotAUser(GatewayError):
    """A username names a group or a channel, not a person."""


class NotAuthorized(GatewayError):
    """The session is missing, revoked or not signed in."""


class GatewayTimeout(GatewayError):
    """A read took longer than the 30 s limit."""


class FloodWait(GatewayError):
    def __init__(self, seconds: int) -> None:
        super().__init__(f"Telegram asked to wait {seconds} s")
        self.seconds = seconds


class OutcomeUnknown(GatewayError):
    """A write may or may not have reached Telegram; it is never resent."""


class TelegramError(GatewayError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ChatNotFound(GatewayError):
    """The account cannot see a chat with that id."""


class TopicNotFound(GatewayError):
    """The forum has no topic with that id."""


class BadCode(GatewayError):
    """The login code was wrong."""


class CodeExpired(GatewayError):
    """The login code expired."""


class BadPassword(GatewayError):
    """The two-step password was wrong."""


class PasswordNeeded(GatewayError):
    """The account has a two-step password; ask for it next."""


class QrExpired(GatewayError):
    """No phone scanned the QR token before the wait ended; ask for a new token."""


_CURSOR = re.compile(r"^v1\.(-?\d{1,20})\.(-?\d{1,20})\.(-?\d{1,20})$")


def encode_cursor(first: int, second: int, third: int) -> str:
    """The opaque ``offset`` cursor that ``list_chats`` returns for the next page."""
    return f"v1.{first}.{second}.{third}"


def decode_cursor(cursor: str) -> tuple[int, int, int]:
    """Parse a cursor made by :func:`encode_cursor`; raises ``ValueError`` on anything else."""
    match = _CURSOR.match(cursor)
    if match is None:
        raise ValueError("not a cursor returned by tg_chats_list")
    return int(match[1]), int(match[2]), int(match[3])
