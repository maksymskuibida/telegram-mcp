"""The TelegramGateway interface. Each feature adds its calls here and to the fake (U2 adds
the unread check and the mark)."""

from __future__ import annotations

from collections.abc import Callable
from typing import BinaryIO, Protocol

from telegram_mcp.gateway.models import (
    ChatDetails,
    ChatRef,
    FolderRef,
    Me,
    MessageView,
    OwnProfile,
    PersonInfo,
    QrToken,
    SearchHit,
    SentCodeInfo,
    SentMessage,
    TopicInfo,
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

    # U4: reads. None of these acknowledges reading (BR22.1, NFR2.3). ---------------------------

    async def folders(self) -> list[FolderRef]:
        """Every chat folder, the main list (0) and the archive (1) first."""

    async def chat_info(self, chat: int | str) -> ChatDetails:
        """One chat by id, or a public username (without @); ``ChatNotFound`` when none."""

    async def topics_list(self, chat_id: int, limit: int) -> list[TopicInfo]:
        """A forum's topics, newest activity first."""

    async def read_messages(
        self,
        chat_id: int,
        limit: int,
        before_id: int | None = None,
        topic_id: int | None = None,
        ids: list[int] | None = None,
    ) -> list[MessageView]:
        """A page of messages newest first (older than ``before_id``), or the messages with
        ``ids`` (missing ones left out). ``TopicNotFound`` for an unknown topic."""

    async def read_pinned(self, chat_id: int, limit: int) -> list[MessageView]:
        """The pinned messages, newest first."""

    async def search_chat(self, chat_id: int, query: str, limit: int) -> list[MessageView]:
        """Messages of one chat holding ``query``, newest first."""

    async def search_global(self, query: str, limit: int) -> list[SearchHit]:
        """Messages across every chat holding ``query``, newest first."""

    async def members(self, chat_id: int, filter: str, limit: int) -> list[PersonInfo]:
        """Members with their role; ``filter`` is all, admins or bots."""

    async def admins(self, chat_id: int, user_id: int) -> dict[str, bool]:
        """The admin rights ``user_id`` has now in the chat; all off for a member."""

    async def contacts(self) -> list[PersonInfo]: ...

    async def user_profile(self, users: list[int] | str) -> list[PersonInfo]:
        """Users by id (one batched call) or one public username. ``UserNotFound`` names the
        ids it cannot see; ``NotAUser`` when a username is a chat."""

    async def own_profile(self) -> OwnProfile: ...

    async def download_media(
        self, chat_id: int, message_id: int, file_obj: BinaryIO, max_bytes: int
    ) -> int:
        """Write a message's media into ``file_obj``; the number of bytes written. ``TooLarge``
        before writing when it is over ``max_bytes``."""

    # U4: writes. ---------------------------------------------------------------------------

    async def edit(self, chat_id: int, message_id: int, text: str) -> MessageView: ...

    async def delete(self, chat_id: int, message_ids: list[int], for_everyone: bool) -> None: ...

    async def forward(self, from_chat: int, message_ids: list[int], to_chat: int) -> list[int]:
        """The ids of the new messages in ``to_chat``."""

    async def react(self, chat_id: int, message_id: int, emoji: str) -> None: ...

    async def pin(self, chat_id: int, message_id: int) -> None: ...

    async def unpin(self, chat_id: int, message_id: int) -> None: ...

    async def topic_create(self, chat_id: int, title: str) -> int:
        """The new topic's id."""

    async def create_group(self, title: str, members: list[int], supergroup: bool) -> int:
        """The new chat's id: a basic group, or a supergroup with ``supergroup``."""

    async def create_channel(self, title: str) -> int: ...

    async def join(self, username: str | None, invite_hash: str | None) -> ChatRef:
        """Join a public chat by username, or a chat by its invite link's hash."""

    async def leave(self, chat_id: int) -> None: ...

    async def invite_link(
        self, chat_id: int, expire_seconds: int | None, usage_limit: int | None
    ) -> str: ...

    async def set_admin_rights(
        self, chat_id: int, user_id: int, rights: dict[str, bool]
    ) -> None: ...

    async def edit_chat(
        self,
        chat_id: int,
        title: str | None,
        description: str | None,
        photo: BinaryIO | None,
        photo_name: str | None,
    ) -> None:
        """Change the fields given; None leaves a field as it is."""

    async def edit_own_profile(
        self,
        first_name: str | None,
        last_name: str | None,
        bio: str | None,
        username: str | None,
        photo: BinaryIO | None,
        photo_name: str | None,
    ) -> None:
        """Change the fields given; None leaves a field as it is."""

    async def send_file(
        self, chat_id: int, file_obj: BinaryIO, name: str, caption: str
    ) -> SentMessage:
        """Upload from the open file object (the one that was checked, NFR3.2)."""


GatewayFactory = Callable[[str, int, str], TelegramGateway]
"""Builds a gateway from (session, api_id, api_hash)."""
