"""Data shapes and errors of the Telegram gateway (C3)."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
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
class Me:
    """The logged-in account, as login prints it."""

    id: int
    name: str
    username: str | None


class GatewayError(Exception):
    """Base of every error the gateway raises."""


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


class BadCode(GatewayError):
    """The login code was wrong."""


class CodeExpired(GatewayError):
    """The login code expired."""


class BadPassword(GatewayError):
    """The two-step password was wrong."""


class PasswordNeeded(GatewayError):
    """The account has a two-step password; ask for it next."""


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
