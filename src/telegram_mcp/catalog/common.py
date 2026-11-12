"""What every tool family shares: the catalogue's context, closed-schema validation (BR20.4,
BR21.1), the reads describers make, and the shapes results take (BR22.2)."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from telegram_mcp.activity_log import iso_utc
from telegram_mcp.files import FileStore
from telegram_mcp.gateway.models import ChatRef, MessageView, PersonInfo
from telegram_mcp.gateway.protocol import TelegramGateway
from telegram_mcp.paths import FixedPaths
from telegram_mcp.prompts import MessageExcerpt, PromptError, check_message_ids
from telegram_mcp.tools import TELEGRAM_CONTENT_NOTICE, InvalidArguments

CHAT_ID_TEXT = re.compile(r"^-?\d{1,20}$")
CHAT_BY_ID = "Name the chat by its id; tg_chats_resolve finds the id from a title or username."
GROUP_TYPES = ("group", "supergroup", "channel", "forum")
ONLY_GROUPS = "only groups and channels can do this; this chat is a person or your Saved Messages"
MAX_LIST = 20


@dataclass
class CatalogContext:
    """What the catalogue's actions need besides the gateway: the fixed paths (folders and
    the data folder) and the file records kept between a question and its send."""

    paths: FixedPaths
    files: FileStore
    clock: Callable[[], datetime]


class Args:
    """Reads a tool's raw arguments against its closed schema (BR20.4). Every error is
    :class:`InvalidArguments` naming the argument."""

    def __init__(self, tool: str, raw: object, known: Sequence[str]) -> None:
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise InvalidArguments("arguments must be an object")
        for name in raw:
            if name not in known:
                raise InvalidArguments(f"{name} is not an argument of {tool}")
        self.raw: Mapping[str, Any] = raw

    def given(self, name: str) -> bool:
        return self.raw.get(name) is not None

    def chat(self, name: str = "chat") -> int:
        """A chat by its id: an integer or a string of an optional minus and digits (BR21.1)."""
        value = self.raw.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and CHAT_ID_TEXT.match(value):
            return int(value)
        raise InvalidArguments(f"{name}: {CHAT_BY_ID}")

    def integer(
        self,
        name: str,
        *,
        required: bool = False,
        default: int | None = None,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int | None:
        value = self.raw.get(name)
        if value is None:
            if required:
                raise InvalidArguments(f"{name} is required")
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidArguments(f"{name} must be a whole number")
        if minimum is not None and value < minimum:
            raise InvalidArguments(f"{name} must be at least {minimum}")
        if maximum is not None and value > maximum:
            raise InvalidArguments(f"{name} must be at most {maximum}")
        return value

    def required_int(self, name: str, *, minimum: int = 1) -> int:
        value = self.integer(name, required=True, minimum=minimum)
        assert value is not None  # noqa: S101 (required)
        return value

    def text(
        self,
        name: str,
        *,
        required: bool = False,
        default: str | None = None,
        min_len: int = 0,
        max_len: int,
    ) -> str | None:
        """Text exactly as given, never trimmed (BR23.5)."""
        value = self.raw.get(name)
        if value is None:
            if required:
                raise InvalidArguments(f"{name} is required")
            return default
        if not isinstance(value, str):
            raise InvalidArguments(f"{name} must be text")
        if not min_len <= len(value) <= max_len:
            raise InvalidArguments(f"{name} must be {min_len} to {max_len} characters")
        return value

    def required_text(self, name: str, *, min_len: int = 1, max_len: int) -> str:
        value = self.text(name, required=True, min_len=min_len, max_len=max_len)
        assert value is not None  # noqa: S101 (required)
        return value

    def boolean(self, name: str, default: bool | None) -> bool | None:
        value = self.raw.get(name)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise InvalidArguments(f"{name} must be true or false")
        return value

    def choice(self, name: str, allowed: Sequence[str], default: str | None = None) -> str:
        value = self.raw.get(name, default)
        if value not in allowed:
            raise InvalidArguments(f"{name} must be one of {', '.join(allowed)}")
        return str(value)

    def ids(self, name: str, *, minimum: int = 1, maximum: int = MAX_LIST) -> list[int]:
        """A list of positive ids in the order given, each once (BR20.4, BR23.4)."""
        value = self.raw.get(name, [] if minimum == 0 else None)
        if not isinstance(value, list):
            raise InvalidArguments(f"{name} must be a list of ids")
        if not minimum <= len(value) <= maximum:
            raise InvalidArguments(f"{name} must hold {minimum} to {maximum} ids")
        for item in value:
            if isinstance(item, bool) or not isinstance(item, int) or item < 1:
                raise InvalidArguments(f"{name} must hold positive whole numbers")
        if len(set(value)) != len(value):
            raise InvalidArguments(f"{name}: each id may be named only once")
        return list(value)


# Reads describers make --------------------------------------------------------------------------


def excerpt(message: MessageView) -> MessageExcerpt:
    media = None
    if message.media is not None:
        media = message.media.kind
        if message.media.file_name:
            media += f" {message.media.file_name}"
    sender = message.sender.name if message.sender is not None else ""
    return MessageExcerpt(
        id=message.id, sender=sender, date=message.date, text=message.text, media=media
    )


async def named_messages(
    gateway: TelegramGateway, chat_id: int, ids: Sequence[int]
) -> tuple[MessageExcerpt, ...]:
    """The messages a write names, in one call (NFR11.4); a missing one is not_found."""
    found = await gateway.read_messages(chat_id, len(ids), ids=list(ids))
    return check_message_ids(list(ids), [excerpt(message) for message in found])


def require_group(chat: ChatRef, allowed: Sequence[str] = GROUP_TYPES) -> None:
    """BR23.6: chat changes apply to groups and channels only."""
    if chat.saved_messages or chat.type not in allowed:
        raise PromptError("invalid_arguments", ONLY_GROUPS)


# Result shapes -------------------------------------------------------------------------------


def with_notice(data: dict[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    """A read result with its Telegram-content fields listed and U1's notice (BR22.2)."""
    return {**data, "telegram_content_fields": list(fields), "notice": TELEGRAM_CONTENT_NOTICE}


def chat_dict(chat: ChatRef) -> dict[str, Any]:
    return {"id": chat.id, "name": chat.name, "type": chat.type}


def message_dict(message: MessageView) -> dict[str, Any]:
    media = None
    if message.media is not None:
        media = {
            "kind": message.media.kind,
            "size_bytes": message.media.size_bytes,
            "file_name": message.media.file_name,
        }
    sender = None
    if message.sender is not None:
        sender = {"id": message.sender.id, "name": message.sender.name}
    return {
        "id": message.id,
        "chat_id": message.chat_id,
        "date": iso_utc(message.date),
        "sender": sender,
        "outgoing": message.outgoing,
        "text": message.text,
        "reply_to": message.reply_to,
        "topic_id": message.topic_id,
        "media": media,
        "edited": message.edited,
        "forwarded_from": message.forwarded_from,
    }


MESSAGE_CONTENT = ("text", "sender.name", "media.file_name", "forwarded_from")


def message_fields(prefix: str) -> list[str]:
    return [f"{prefix}.{field}" for field in MESSAGE_CONTENT]


def person_dict(person: PersonInfo) -> dict[str, Any]:
    return {
        "id": person.id,
        "name": person.name,
        "username": person.username,
        "phone": person.phone,
        "bio": person.bio,
        "is_bot": person.is_bot,
        "is_contact": person.is_contact,
        "role": person.role,
        "admin_rights": dict(person.admin_rights) if person.admin_rights is not None else None,
    }
