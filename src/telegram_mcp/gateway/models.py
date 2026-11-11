"""Data shapes and errors of the Telegram gateway (C3)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

ChatType = Literal["user", "group", "supergroup", "channel", "forum"]
CHAT_TYPES: tuple[ChatType, ...] = ("user", "group", "supergroup", "channel", "forum")
PREVIEW_LIMIT = 200


@dataclass(frozen=True)
class ChatRef:
    """A chat as the gateway returns it. ``name`` and ``last_preview`` are Telegram content."""

    id: int
    name: str
    type: ChatType
    unread: int = 0
    muted: bool = False
    last_preview: str | None = None
    folder_ids: tuple[int, ...] = ()


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
