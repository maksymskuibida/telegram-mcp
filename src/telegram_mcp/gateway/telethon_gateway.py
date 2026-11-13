"""The real gateway, on Telethon. This is the only module that imports Telethon (ADR-004).

The client never retries or sleeps on its own (NFR6.2), every call has a 30 s limit (NFR6.1),
and a dropped connection is re-established on the next call (NFR6.3)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, BinaryIO, Literal, TypeVar

from telethon import TelegramClient, errors, functions, utils
from telethon.sessions import StringSession
from telethon.tl import types

from telegram_mcp.gateway.models import (
    PREVIEW_LIMIT,
    SAVED_MESSAGES,
    BadCode,
    BadPassword,
    ChatDetails,
    ChatNotFound,
    ChatRef,
    ChatType,
    CodeDelivery,
    CodeExpired,
    FloodWait,
    FolderRef,
    GatewayError,
    GatewayTimeout,
    Me,
    MediaRef,
    MessageView,
    NotAUser,
    NotAuthorized,
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

    async def _input_peer(self, chat_id: int) -> Any:
        try:
            return await self._call(lambda: self._client.get_input_entity(chat_id))
        except ValueError as error:
            raise ChatNotFound() from error

    async def unread_state(self, chat_id: int, topic_id: int | None) -> UnreadState:
        """The dialog's unread count and top message, or the topic's (BR14.1). Neither request
        acknowledges reading."""
        peer = await self._input_peer(chat_id)
        if topic_id is None:
            request: Any = functions.messages.GetPeerDialogsRequest(
                peers=[types.InputDialogPeer(peer=peer)]
            )
            result = await self._call(lambda: self._client(request))
            dialogs = list(getattr(result, "dialogs", []) or [])
            if not dialogs:
                raise ChatNotFound()
            found: Any = dialogs[0]
        else:
            request = functions.messages.GetForumTopicsByIDRequest(peer=peer, topics=[topic_id])
            result = await self._call(lambda: self._client(request))
            topics = [
                topic
                for topic in getattr(result, "topics", []) or []
                if isinstance(topic, types.ForumTopic) and topic.id == topic_id
            ]
            if not topics:
                raise TopicNotFound()
            found = topics[0]
        title = str(found.title) if topic_id is not None else None
        return UnreadState(
            unread=int(found.unread_count or 0),
            newest_id=int(found.top_message or 0),
            topic_title=title,
        )

    async def mark_read(self, chat_id: int, max_id: int, topic_id: int | None) -> None:
        """Read history up to ``max_id`` for the chat, or the topic's read request. Never the
        separate requests that clear all mentions or reactions (BR13.9). A timeout is
        ``OutcomeUnknown`` and is never retried here (BR14.2)."""
        peer = await self._input_peer(chat_id)
        request: Any
        if topic_id is not None:
            request = functions.messages.ReadDiscussionRequest(
                peer=peer, msg_id=topic_id, read_max_id=max_id
            )
        elif isinstance(peer, types.InputPeerChannel):
            request = functions.channels.ReadHistoryRequest(
                channel=utils.get_input_channel(peer), max_id=max_id
            )
        else:
            request = functions.messages.ReadHistoryRequest(peer=peer, max_id=max_id)
        await self._call(lambda: self._client(request), write=True)

    # U4 reads. None of these sends a read acknowledgement (NFR2.3). ------------------------------

    async def _request(self, request: Any, *, write: bool = False) -> Any:
        return await self._call(lambda: self._client(request), write=write)

    async def _entity(self, chat: int | str) -> Any:
        """The chat or user, by id (filling the cache from recent chats once) or username."""
        try:
            return await self._call(lambda: self._client.get_entity(chat))
        except (ValueError, TelegramError) as error:
            if isinstance(chat, str):
                raise ChatNotFound() from error
        await self._call(lambda: self._client.get_dialogs(limit=RESOLVE_DIALOG_SCAN))
        try:
            return await self._call(lambda: self._client.get_entity(chat))
        except (ValueError, TelegramError) as error:
            raise ChatNotFound() from error

    async def folders(self) -> list[FolderRef]:
        result = await self._request(functions.messages.GetDialogFiltersRequest())
        filters = getattr(result, "filters", result) or []
        found = [FolderRef(0, MAIN_LIST), FolderRef(1, ARCHIVE)]
        for item in filters:
            if hasattr(item, "id") and hasattr(item, "title"):
                found.append(FolderRef(int(item.id), _text(item.title)))
        return found

    async def chat_info(self, chat: int | str) -> ChatDetails:
        entity = await self._entity(chat)
        if isinstance(entity, types.User):
            full: Any = await self._request(functions.users.GetFullUserRequest(entity))
            about = getattr(full.full_user, "about", None)
            count: int | None = None
            pinned: int | None = getattr(full.full_user, "pinned_msg_id", None)
        elif isinstance(entity, types.Chat | types.ChatForbidden):
            full = await self._request(functions.messages.GetFullChatRequest(entity.id))
            about = getattr(full.full_chat, "about", None)
            count = getattr(entity, "participants_count", None)
            pinned = getattr(full.full_chat, "pinned_msg_id", None)
        else:
            full = await self._request(functions.channels.GetFullChannelRequest(entity))
            about = getattr(full.full_chat, "about", None)
            count = getattr(full.full_chat, "participants_count", None)
            pinned = getattr(full.full_chat, "pinned_msg_id", None)
        unread, muted = await self._dialog_state(entity)
        chat_id = int(utils.get_peer_id(entity))
        own_id = await self._account_id()
        kind = _chat_type(entity)
        return ChatDetails(
            id=chat_id,
            name=SAVED_MESSAGES if chat_id == own_id else utils.get_display_name(entity),
            type=kind,
            username=getattr(entity, "username", None),
            description=about or None,
            member_count=count,
            unread=unread,
            muted=muted,
            is_forum=kind == "forum",
            my_role=_role(entity),
            pinned_message_id=pinned or None,
            saved_messages=chat_id == own_id,
        )

    async def _dialog_state(self, entity: Any) -> tuple[int, bool]:
        """The unread count and muted flag from the person's dialog, if they have one."""
        try:
            peer = utils.get_input_peer(entity)
            request = functions.messages.GetPeerDialogsRequest(
                peers=[types.InputDialogPeer(peer=peer)]
            )
            result = await self._request(request)
        except (GatewayError, TypeError):
            return 0, False
        dialogs = list(getattr(result, "dialogs", []) or [])
        if not dialogs:
            return 0, False
        mute_until = getattr(dialogs[0].notify_settings, "mute_until", None)
        muted = isinstance(mute_until, datetime) and mute_until > datetime.now(UTC)
        return int(dialogs[0].unread_count or 0), muted

    async def topics_list(self, chat_id: int, limit: int) -> list[TopicInfo]:
        peer = await self._input_peer(chat_id)
        request = functions.messages.GetForumTopicsRequest(
            peer=peer, offset_date=None, offset_id=0, offset_topic=0, limit=limit
        )
        result = await self._request(request)
        return [
            TopicInfo(
                topic_id=int(topic.id),
                title=str(topic.title),
                unread=int(topic.unread_count or 0),
                closed=bool(topic.closed),
                pinned=bool(topic.pinned),
            )
            for topic in getattr(result, "topics", []) or []
            if isinstance(topic, types.ForumTopic)
        ]

    async def _messages(self, chat_id: int, **options: Any) -> list[Any]:
        try:
            found = await self._call(lambda: self._client.get_messages(chat_id, **options))
        except ValueError as error:
            raise ChatNotFound() from error
        except TelegramError as error:
            if options.get("reply_to") is not None and "TOPIC" in error.reason.upper():
                raise TopicNotFound() from error
            raise
        if found is None:
            return []
        return [message for message in (found if isinstance(found, list) else [found]) if message]

    async def read_messages(
        self,
        chat_id: int,
        limit: int,
        before_id: int | None = None,
        topic_id: int | None = None,
        ids: list[int] | None = None,
    ) -> list[MessageView]:
        if ids is not None:
            found = await self._messages(chat_id, ids=list(ids))
        else:
            options: dict[str, Any] = {"limit": limit, "max_id": before_id or 0}
            if topic_id is not None:
                options["reply_to"] = topic_id
            found = await self._messages(chat_id, **options)
        return [_view(message, chat_id) for message in found]

    async def read_pinned(self, chat_id: int, limit: int) -> list[MessageView]:
        found = await self._messages(chat_id, limit=limit, filter=types.InputMessagesFilterPinned())
        return [_view(message, chat_id) for message in found]

    async def search_chat(self, chat_id: int, query: str, limit: int) -> list[MessageView]:
        found = await self._messages(chat_id, limit=limit, search=query)
        return [_view(message, chat_id) for message in found]

    async def search_global(self, query: str, limit: int) -> list[SearchHit]:
        request = functions.messages.SearchGlobalRequest(
            q=query,
            filter=types.InputMessagesFilterEmpty(),
            min_date=None,
            max_date=None,
            offset_rate=0,
            offset_peer=types.InputPeerEmpty(),
            offset_id=0,
            limit=limit,
        )
        result = await self._request(request)
        peers = {
            int(utils.get_peer_id(entity)): entity
            for entity in [*getattr(result, "chats", []), *getattr(result, "users", [])]
        }
        hits = []
        for message in getattr(result, "messages", []) or []:
            chat_id = int(utils.get_peer_id(message.peer_id))
            entity = peers.get(chat_id)
            if entity is None:
                continue
            chat = ChatRef(id=chat_id, name=utils.get_display_name(entity), type=_chat_type(entity))
            sender = peers.get(int(utils.get_peer_id(message.from_id))) if message.from_id else None
            hits.append(SearchHit(chat=chat, message=_view(message, chat_id, sender)))
        return hits

    async def members(self, chat_id: int, filter: str, limit: int) -> list[PersonInfo]:
        options: dict[str, Any] = {"limit": limit}
        if filter == "admins":
            options["filter"] = types.ChannelParticipantsAdmins()
        elif filter == "bots":
            options["filter"] = types.ChannelParticipantsBots()
        users = await self._call(lambda: self._client.get_participants(chat_id, **options))
        found = []
        for user in users:
            participant = getattr(user, "participant", None)
            role = _participant_role(participant)
            rights = None
            if role != "member":
                rights = tuple(sorted(_rights(getattr(participant, "admin_rights", None)).items()))
                if role == "creator":
                    rights = tuple((name, True) for name in sorted(ADMIN_RIGHTS))
            found.append(replace(_person(user), role=role, admin_rights=rights))
        return found

    async def admins(self, chat_id: int, user_id: int) -> dict[str, bool]:
        permissions = await self._call(lambda: self._client.get_permissions(chat_id, user_id))
        if getattr(permissions, "is_creator", False):
            return dict.fromkeys(ADMIN_RIGHTS, True)
        if not getattr(permissions, "is_admin", False):
            return dict.fromkeys(ADMIN_RIGHTS, False)
        participant = getattr(permissions, "participant", None)
        return _rights(getattr(participant, "admin_rights", None))

    async def contacts(self) -> list[PersonInfo]:
        result = await self._request(functions.contacts.GetContactsRequest(hash=0))
        return [replace(_person(user), is_contact=True) for user in getattr(result, "users", [])]

    async def user_profile(self, users: list[int] | str) -> list[PersonInfo]:
        if isinstance(users, str):
            try:
                entity = await self._entity(users)
            except ChatNotFound as error:
                raise UserNotFound(username=users) from error
            if not isinstance(entity, types.User):
                raise NotAUser()
            return [await self._with_bio(entity)]
        inputs = []
        missing = []
        for user_id in users:
            try:
                inputs.append(await self._input_entity(user_id))
            except (ValueError, TelegramError):
                missing.append(user_id)
        if missing:
            raise UserNotFound(ids=tuple(missing))
        found = await self._request(functions.users.GetUsersRequest(id=inputs))
        people = {int(user.id): user for user in found if isinstance(user, types.User)}
        absent = tuple(user_id for user_id in users if user_id not in people)
        if absent:
            raise UserNotFound(ids=absent)
        if len(users) == 1:
            return [await self._with_bio(people[users[0]])]
        return [_person(people[user_id]) for user_id in users]

    async def _with_bio(self, user: Any) -> PersonInfo:
        full = await self._request(functions.users.GetFullUserRequest(user))
        return replace(_person(user), bio=getattr(full.full_user, "about", None) or None)

    async def own_profile(self) -> OwnProfile:
        user = await self._call(self._client.get_me)
        if user is None:
            raise NotAuthorized()
        full = await self._request(functions.users.GetFullUserRequest(types.InputUserSelf()))
        return OwnProfile(
            id=int(user.id),
            first_name=str(user.first_name or ""),
            last_name=user.last_name or None,
            username=user.username,
            bio=getattr(full.full_user, "about", None) or None,
            phone=user.phone,
            has_photo=user.photo is not None,
        )

    async def download_media(
        self, chat_id: int, message_id: int, file_obj: BinaryIO, max_bytes: int
    ) -> int:
        found = await self._messages(chat_id, ids=[message_id])
        if not found or found[0].media is None:
            raise TelegramError("MEDIA_EMPTY")
        size = int(getattr(found[0].file, "size", 0) or 0)
        if size > max_bytes:
            raise TooLarge(size)
        await self._call(lambda: self._client.download_media(found[0], file=file_obj))
        return int(file_obj.tell())

    # U4 writes: any failure after the connection is up is OutcomeUnknown (NFR6). -------------

    async def edit(self, chat_id: int, message_id: int, text: str) -> MessageView:
        edited = await self._call(
            lambda: self._client.edit_message(chat_id, message_id, text, parse_mode=None),
            write=True,
        )
        return _view(edited, chat_id)

    async def delete(self, chat_id: int, message_ids: list[int], for_everyone: bool) -> None:
        await self._call(
            lambda: self._client.delete_messages(chat_id, list(message_ids), revoke=for_everyone),
            write=True,
        )

    async def forward(self, from_chat: int, message_ids: list[int], to_chat: int) -> list[int]:
        sent = await self._call(
            lambda: self._client.forward_messages(to_chat, list(message_ids), from_peer=from_chat),
            write=True,
        )
        items = sent if isinstance(sent, list) else [sent]
        return [int(message.id) for message in items if message is not None]

    async def react(self, chat_id: int, message_id: int, emoji: str) -> None:
        peer = await self._input_peer(chat_id)
        request = functions.messages.SendReactionRequest(
            peer=peer, msg_id=message_id, reaction=[types.ReactionEmoji(emoticon=emoji)]
        )
        await self._request(request, write=True)

    async def pin(self, chat_id: int, message_id: int) -> None:
        await self._call(
            lambda: self._client.pin_message(chat_id, message_id, notify=False), write=True
        )

    async def unpin(self, chat_id: int, message_id: int) -> None:
        await self._call(lambda: self._client.unpin_message(chat_id, message_id), write=True)

    async def topic_create(self, chat_id: int, title: str) -> int:
        peer = await self._input_peer(chat_id)
        result = await self._request(
            functions.messages.CreateForumTopicRequest(peer=peer, title=title), write=True
        )
        for update in getattr(result, "updates", []) or []:
            message = getattr(update, "message", None)
            if message is not None and isinstance(
                getattr(message, "action", None), types.MessageActionTopicCreate
            ):
                return int(message.id)
        raise OutcomeUnknown()

    async def _input_entity(self, peer: int) -> Any:
        return await self._call(lambda: self._client.get_input_entity(peer))

    async def _input_users(self, user_ids: list[int]) -> list[Any]:
        found = []
        for user_id in user_ids:
            try:
                found.append(await self._input_entity(user_id))
            except ValueError as error:
                raise UserNotFound(ids=(user_id,)) from error
        return found

    async def create_group(self, title: str, members: list[int], supergroup: bool) -> int:
        users = await self._input_users(members)
        if not supergroup:
            result = await self._request(
                functions.messages.CreateChatRequest(users=users, title=title), write=True
            )
            return _new_chat_id(result)
        created = await self._request(
            functions.channels.CreateChannelRequest(title=title, about="", megagroup=True),
            write=True,
        )
        chat_id = _new_chat_id(created)
        if users:
            channel = await self._input_peer(chat_id)
            try:
                await self._request(
                    functions.channels.InviteToChannelRequest(channel=channel, users=users),
                    write=True,
                )
            except GatewayError as error:
                raise TelegramError(
                    f"the supergroup was created (id {chat_id}) but adding its members "
                    f"failed: {error}"
                ) from error
        return chat_id

    async def create_channel(self, title: str) -> int:
        created = await self._request(
            functions.channels.CreateChannelRequest(title=title, about="", broadcast=True),
            write=True,
        )
        return _new_chat_id(created)

    async def join(self, username: str | None, invite_hash: str | None) -> ChatRef:
        if invite_hash is not None:
            request: Any = functions.messages.ImportChatInviteRequest(hash=invite_hash)
        else:
            entity = await self._entity(username or "")
            request = functions.channels.JoinChannelRequest(channel=entity)
        result = await self._request(request, write=True)
        chats = list(getattr(result, "chats", []) or [])
        if not chats:
            raise OutcomeUnknown()
        entity = chats[0]
        return ChatRef(
            id=int(utils.get_peer_id(entity)),
            name=utils.get_display_name(entity),
            type=_chat_type(entity),
        )

    async def leave(self, chat_id: int) -> None:
        entity = await self._entity(chat_id)
        if isinstance(entity, types.Chat):
            request: Any = functions.messages.DeleteChatUserRequest(
                chat_id=entity.id, user_id=types.InputUserSelf()
            )
        else:
            request = functions.channels.LeaveChannelRequest(channel=entity)
        await self._request(request, write=True)

    async def invite_link(
        self, chat_id: int, expire_seconds: int | None, usage_limit: int | None
    ) -> str:
        peer = await self._input_peer(chat_id)
        expires = None
        if expire_seconds is not None:
            expires = datetime.now(UTC) + timedelta(seconds=expire_seconds)
        result = await self._request(
            functions.messages.ExportChatInviteRequest(
                peer=peer, expire_date=expires, usage_limit=usage_limit
            ),
            write=True,
        )
        return str(result.link)

    async def set_admin_rights(self, chat_id: int, user_id: int, rights: dict[str, bool]) -> None:
        channel = await self._input_peer(chat_id)
        (user,) = await self._input_users([user_id])
        allowed = {name: bool(rights.get(name, False)) for name in ADMIN_RIGHTS}
        await self._request(
            functions.channels.EditAdminRequest(
                channel=channel,
                user_id=user,
                admin_rights=types.ChatAdminRights(**allowed),
                rank="",
            ),
            write=True,
        )

    async def _uploaded(self, photo: BinaryIO, name: str | None) -> Any:
        return await self._call(
            lambda: self._client.upload_file(photo, file_name=name or "photo.jpg"), write=True
        )

    async def edit_chat(
        self,
        chat_id: int,
        title: str | None,
        description: str | None,
        photo: BinaryIO | None,
        photo_name: str | None,
    ) -> None:
        entity = await self._entity(chat_id)
        basic = isinstance(entity, types.Chat)
        if title is not None:
            request: Any = (
                functions.messages.EditChatTitleRequest(chat_id=entity.id, title=title)
                if basic
                else functions.channels.EditTitleRequest(channel=entity, title=title)
            )
            await self._request(request, write=True)
        if description is not None:
            peer = utils.get_input_peer(entity)
            request = functions.messages.EditChatAboutRequest(peer=peer, about=description)
            await self._request(request, write=True)
        if photo is not None:
            chat_photo = types.InputChatUploadedPhoto(file=await self._uploaded(photo, photo_name))
            request = (
                functions.messages.EditChatPhotoRequest(chat_id=entity.id, photo=chat_photo)
                if basic
                else functions.channels.EditPhotoRequest(channel=entity, photo=chat_photo)
            )
            await self._request(request, write=True)

    async def edit_own_profile(
        self,
        first_name: str | None,
        last_name: str | None,
        bio: str | None,
        username: str | None,
        photo: BinaryIO | None,
        photo_name: str | None,
    ) -> None:
        if (first_name, last_name, bio) != (None, None, None):
            request: Any = functions.account.UpdateProfileRequest(
                first_name=first_name, last_name=last_name, about=bio
            )
            await self._request(request, write=True)
        if username is not None:
            await self._request(functions.account.UpdateUsernameRequest(username), write=True)
        if photo is not None:
            uploaded = await self._uploaded(photo, photo_name)
            request = functions.photos.UploadProfilePhotoRequest(file=uploaded)
            await self._request(request, write=True)

    async def send_file(
        self, chat_id: int, file_obj: BinaryIO, name: str, caption: str
    ) -> SentMessage:
        """Upload from the open file the sandbox checked, under its approved name."""
        uploaded = await self._call(
            lambda: self._client.upload_file(file_obj, file_name=name), write=True
        )
        message = await self._call(
            lambda: self._client.send_file(chat_id, uploaded, caption=caption, parse_mode=None),
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


MAIN_LIST = "Main list"
ARCHIVE = "Archive"
ADMIN_RIGHTS = (
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


def _text(value: Any) -> str:
    """A folder title is text, or text with entities in newer layers."""
    return str(getattr(value, "text", value) or "")


def _role(entity: Any) -> Role:
    if isinstance(entity, types.User):
        return "none"
    if getattr(entity, "left", False) or getattr(entity, "kicked", False):
        return "none"
    if getattr(entity, "creator", False):
        return "creator"
    if getattr(entity, "admin_rights", None) is not None:
        return "admin"
    return "member"


def _participant_role(participant: Any) -> Literal["member", "admin", "creator"]:
    if isinstance(participant, types.ChannelParticipantCreator | types.ChatParticipantCreator):
        return "creator"
    if isinstance(participant, types.ChannelParticipantAdmin | types.ChatParticipantAdmin):
        return "admin"
    return "member"


def _rights(rights: Any) -> dict[str, bool]:
    return {name: bool(getattr(rights, name, False)) for name in ADMIN_RIGHTS}


def _person(user: Any) -> PersonInfo:
    return PersonInfo(
        id=int(user.id),
        name=utils.get_display_name(user),
        username=getattr(user, "username", None),
        phone=getattr(user, "phone", None),
        is_bot=bool(getattr(user, "bot", False)),
        is_contact=bool(getattr(user, "contact", False)),
    )


def _media(message: Any) -> MediaRef | None:
    if getattr(message, "media", None) is None:
        return None
    kind = "document"
    if getattr(message, "photo", None) is not None:
        kind = "photo"
    elif getattr(message, "voice", None) is not None:
        kind = "voice"
    elif getattr(message, "video", None) is not None:
        kind = "video"
    elif getattr(message, "audio", None) is not None:
        kind = "audio"
    elif getattr(message, "sticker", None) is not None:
        kind = "sticker"
    elif getattr(message, "document", None) is None:
        kind = "other"
    file = getattr(message, "file", None)
    return MediaRef(
        kind=kind,
        size_bytes=int(getattr(file, "size", 0) or 0),
        file_name=getattr(file, "name", None),
    )


def _view(message: Any, chat_id: int, sender: Any = None) -> MessageView:
    who = sender if sender is not None else getattr(message, "sender", None)
    sender_id = getattr(message, "sender_id", None)
    shown = None
    if who is not None:
        shown = Sender(int(getattr(who, "id", sender_id or 0)), utils.get_display_name(who))
    elif sender_id is not None:
        shown = Sender(int(sender_id), "")
    reply = getattr(message, "reply_to", None)
    topic_id = None
    reply_to = getattr(reply, "reply_to_msg_id", None)
    if getattr(reply, "forum_topic", False):
        topic_id = getattr(reply, "reply_to_top_id", None) or reply_to
    forward = getattr(message, "fwd_from", None)
    return MessageView(
        id=int(message.id),
        chat_id=chat_id,
        date=message.date,
        sender=shown,
        outgoing=bool(getattr(message, "out", False)),
        text=str(getattr(message, "message", "") or ""),
        reply_to=reply_to,
        topic_id=topic_id,
        media=_media(message),
        edited=getattr(message, "edit_date", None) is not None,
        forwarded_from=(getattr(forward, "from_name", None) or "unknown sender")
        if forward is not None
        else None,
    )


def _new_chat_id(result: Any) -> int:
    """The id of the chat a create request made (it comes back in the result's chats)."""
    updates = getattr(result, "updates", result)
    chats = list(getattr(updates, "chats", []) or [])
    if not chats:
        raise OutcomeUnknown()
    return int(utils.get_peer_id(chats[0]))


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
