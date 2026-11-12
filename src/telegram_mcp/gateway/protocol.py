"""The TelegramGateway interface. Each feature adds its calls here and to the fake (U2 adds
the unread check and the mark)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from telegram_mcp.gateway.models import (
    ChatRef,
    Me,
    QrToken,
    SentCodeInfo,
    SentMessage,
    UnreadState,
)


class TelegramGateway(Protocol):
    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def is_authorized(self) -> bool: ...

    async def sign_in_phone(self, phone: str) -> SentCodeInfo:
        """Ask Telegram to send a login code to ``phone``; says where it went (BR1.10)."""

    async def resend_code(self, phone: str) -> SentCodeInfo:
        """Ask Telegram to send the code again by the next way it offered (BR1.10)."""

    async def sign_in_code(self, code: str) -> Me:
        """Sign in with the code; raises ``PasswordNeeded``, ``BadCode`` or ``CodeExpired``."""

    async def sign_in_password(self, password: str) -> Me:
        """Finish sign-in with the two-step password; raises ``BadPassword``."""

    async def sign_in_qr(self) -> QrToken:
        """Ask Telegram for a QR login token; every call returns a new one."""

    async def wait_qr(self, timeout_seconds: float) -> Me:
        """Wait up to ``timeout_seconds`` for a phone to scan the latest token. Raises
        ``QrExpired`` when nobody scanned it in time, ``PasswordNeeded`` when the account has a
        two-step password (then ``sign_in_password`` finishes the login)."""

    async def log_out(self) -> bool:
        """End this session on Telegram. True when Telegram confirmed it; a refusal raises
        the mapped gateway error (``FloodWait``, ``NotAuthorized``, ``TelegramError``...)."""

    def export_session(self) -> str:
        """The session string to store in the Keychain after sign-in."""

    async def me(self) -> Me: ...

    async def list_chats(
        self, folder_id: int | None, limit: int, offset: str | None
    ) -> tuple[list[ChatRef], str | None]:
        """Chats newest first and the cursor of the next page (None on the last page)."""

    async def resolve_chat(self, chat_id: int) -> ChatRef:
        """The chat with this stable id; raises ``ChatNotFound`` when the account cannot see it."""

    async def send_text(
        self, chat_id: int, text: str, reply_to: int | None, topic_id: int | None
    ) -> SentMessage: ...

    async def unread_state(self, chat_id: int, topic_id: int | None) -> UnreadState:
        """The unread count and newest message id of a chat, or of one forum topic; raises
        ``TopicNotFound`` for a topic the forum does not have (BR14.1). Marks nothing."""

    async def mark_read(self, chat_id: int, max_id: int, topic_id: int | None) -> None:
        """Mark the chat, or one forum topic, read up to ``max_id`` and nothing beyond it, with
        Telegram's read-history requests only (BR13.9, BR14.2)."""


GatewayFactory = Callable[[str, int, str], TelegramGateway]
"""Builds a gateway from (session, api_id, api_hash)."""
