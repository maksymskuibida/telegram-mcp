"""The real gateway's U4 calls against a stubbed Telethon client, no network (test-after; NFR2.3,
NFR6.1, NFR9.1). Each call uses the Telethon method or request named in the tech-stack decision,
read calls never send a read-history request, and a write that fails after the connection is up
is outcome_unknown."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import errors, functions
from telethon.tl import types

from telegram_mcp.gateway.models import (
    SAVED_MESSAGES,
    ChatNotFound,
    FileReferenceExpired,
    FolderRef,
    MediaInfo,
    NotAUser,
    OutcomeUnknown,
    PartRetryable,
    TelegramError,
    TopicInfo,
    TopicNotFound,
    UploadedFile,
    UserNotFound,
)
from telegram_mcp.gateway.telethon_gateway import TelethonGateway

WHEN = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
ADA = types.User(
    id=7, first_name="Ada", last_name="L", username="ada", phone="4900", contact=True, access_hash=1
)
BOB = types.User(id=8, first_name="Bob", bot=True, access_hash=2)
TEAM = types.Channel(
    id=123,
    title="Team",
    photo=types.ChatPhotoEmpty(),
    date=WHEN,
    megagroup=True,
    access_hash=9,
    username="team_chat",
    admin_rights=types.ChatAdminRights(pin_messages=True),
)
FAMILY = types.Chat(
    id=55, title="Family", photo=types.ChatPhotoEmpty(), participants_count=4, date=WHEN, version=1
)
TEAM_ID, FAMILY_ID = -1000000000123, -55
READ_ACKNOWLEDGING = (
    functions.messages.ReadHistoryRequest,
    functions.channels.ReadHistoryRequest,
    functions.messages.ReadDiscussionRequest,
    functions.messages.ReadMentionsRequest,
    functions.messages.ReadReactionsRequest,
)


def message(number: int, text: str = "hi", **extra: Any) -> Any:
    fields: dict[str, Any] = {
        "id": number,
        "date": WHEN,
        "sender": ADA,
        "sender_id": ADA.id,
        "out": False,
        "message": text,
        "reply_to": None,
        "media": None,
        "file": None,
        "edit_date": None,
        "fwd_from": None,
        "peer_id": types.PeerChannel(TEAM.id),
        "from_id": types.PeerUser(ADA.id),
    }
    fields.update(extra)
    return SimpleNamespace(**fields)


class Stub:
    """Stands in for TelegramClient: records high-level calls and raw requests."""

    def __init__(self, *args: Any, **options: Any) -> None:
        self.connected = True
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.requests: list[Any] = []
        self.responses: dict[type, Any] = {}
        self.entities: dict[Any, Any] = {ADA.id: ADA, BOB.id: BOB, TEAM_ID: TEAM, FAMILY_ID: FAMILY}
        self.entities["team_chat"] = TEAM
        self.entities["ada"] = ADA
        self.messages: list[Any] = []
        self.participants: list[Any] = []
        self.permissions: Any = SimpleNamespace(is_creator=False, is_admin=False)
        self.fail: dict[str, BaseException | str] = {}
        self.session = SimpleNamespace(save=lambda: "s")
        self.media_data = b""

    async def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        how = self.fail.pop(name, None)
        if how == "hang":
            await asyncio.Event().wait()
        if isinstance(how, BaseException):
            raise how

    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connected = True

    async def __call__(self, request: Any) -> Any:
        await self._record("call", request)
        self.requests.append(request)
        return self.responses.get(type(request), SimpleNamespace())

    async def get_me(self) -> Any:
        return types.User(id=42, first_name="Me", username="me", phone="1", is_self=True)

    async def get_entity(self, peer: Any) -> Any:
        await self._record("get_entity", peer)
        if peer in self.entities:
            return self.entities[peer]
        raise ValueError("no such entity")

    async def get_input_entity(self, peer: Any) -> Any:
        if peer in self.entities:
            from telethon import utils

            return utils.get_input_peer(self.entities[peer])
        raise ValueError("no such entity")

    async def get_dialogs(self, **options: Any) -> list[Any]:
        return []

    async def get_messages(self, chat: Any, **options: Any) -> Any:
        await self._record("get_messages", chat, **options)
        if "ids" in options:
            wanted = options["ids"]
            return [next((m for m in self.messages if m.id == n), None) for n in wanted]
        return list(self.messages)

    async def get_participants(self, chat: Any, **options: Any) -> list[Any]:
        await self._record("get_participants", chat, **options)
        return list(self.participants)

    async def get_permissions(self, chat: Any, user: Any) -> Any:
        await self._record("get_permissions", chat, user)
        return self.permissions

    def iter_download(self, media: Any, **options: Any) -> Any:
        """One part of ``media_data`` at the offset asked, as Telethon's iterator yields it."""
        self.calls.append(("iter_download", (media,), options))
        how = self.fail.pop("iter_download", None)
        data = self.media_data

        async def chunks() -> Any:
            if how == "hang":
                await asyncio.Event().wait()
            if isinstance(how, BaseException):
                raise how
            start = options["offset"]
            piece = data[start : start + options["request_size"]]
            if piece:
                yield piece

        return chunks()

    async def edit_message(self, *args: Any, **kwargs: Any) -> Any:
        await self._record("edit_message", *args, **kwargs)
        return message(args[1], args[2], edit_date=WHEN, out=True)

    async def delete_messages(self, *args: Any, **kwargs: Any) -> None:
        await self._record("delete_messages", *args, **kwargs)

    async def forward_messages(self, *args: Any, **kwargs: Any) -> Any:
        await self._record("forward_messages", *args, **kwargs)
        return [message(900 + n) for n in range(len(args[1]))]

    async def pin_message(self, *args: Any, **kwargs: Any) -> None:
        await self._record("pin_message", *args, **kwargs)

    async def unpin_message(self, *args: Any, **kwargs: Any) -> None:
        await self._record("unpin_message", *args, **kwargs)

    async def send_file(self, *args: Any, **kwargs: Any) -> Any:
        await self._record("send_file", *args, **kwargs)
        return message(77)


@pytest.fixture
def stub() -> Stub:
    return Stub()


@pytest.fixture
def gateway(stub: Stub) -> TelethonGateway:
    return TelethonGateway("", 1, "0" * 32, client_factory=lambda *a, **k: stub)


def no_read_acknowledgement(stub: Stub) -> None:
    assert not [r for r in stub.requests if isinstance(r, READ_ACKNOWLEDGING)]


def requests_of(stub: Stub, kind: type) -> list[Any]:
    return [request for request in stub.requests if isinstance(request, kind)]


# Reads -----------------------------------------------------------------------------------------


async def test_folders_put_the_main_list_and_archive_first(
    gateway: TelethonGateway, stub: Stub
) -> None:
    stub.responses[functions.messages.GetDialogFiltersRequest] = SimpleNamespace(
        filters=[SimpleNamespace(id=2, title=SimpleNamespace(text="Work")), SimpleNamespace()]
    )
    assert await gateway.folders() == [
        FolderRef(0, "Main list"),
        FolderRef(1, "Archive"),
        FolderRef(2, "Work"),
    ]
    no_read_acknowledgement(stub)


async def test_chat_info_of_a_supergroup(gateway: TelethonGateway, stub: Stub) -> None:
    stub.responses[functions.channels.GetFullChannelRequest] = SimpleNamespace(
        full_chat=SimpleNamespace(about="We build", participants_count=10, pinned_msg_id=5)
    )
    stub.responses[functions.messages.GetPeerDialogsRequest] = SimpleNamespace(
        dialogs=[SimpleNamespace(unread_count=3, notify_settings=SimpleNamespace(mute_until=None))]
    )
    details = await gateway.chat_info("team_chat")
    assert (details.id, details.type, details.username, details.description) == (
        TEAM_ID,
        "supergroup",
        "team_chat",
        "We build",
    )
    assert (details.member_count, details.unread, details.my_role) == (10, 3, "admin")
    assert details.pinned_message_id == 5
    no_read_acknowledgement(stub)


async def test_chat_info_of_a_basic_group_and_a_user(gateway: TelethonGateway, stub: Stub) -> None:
    stub.responses[functions.messages.GetFullChatRequest] = SimpleNamespace(
        full_chat=SimpleNamespace(about="", pinned_msg_id=None)
    )
    stub.responses[functions.users.GetFullUserRequest] = SimpleNamespace(
        full_user=SimpleNamespace(about="bio", pinned_msg_id=None)
    )
    family = await gateway.chat_info(FAMILY_ID)
    assert (family.type, family.member_count, family.my_role) == ("group", 4, "member")
    ada = await gateway.chat_info(ADA.id)
    assert (ada.type, ada.description, ada.my_role) == ("user", "bio", "none")


async def test_chat_info_of_the_saved_messages(gateway: TelethonGateway, stub: Stub) -> None:
    stub.entities[42] = types.User(id=42, first_name="Me", is_self=True, access_hash=3)
    stub.responses[functions.users.GetFullUserRequest] = SimpleNamespace(
        full_user=SimpleNamespace(about=None)
    )
    saved = await gateway.chat_info(42)
    assert (saved.name, saved.saved_messages) == (SAVED_MESSAGES, True)


async def test_an_unknown_chat_is_not_found(gateway: TelethonGateway) -> None:
    with pytest.raises(ChatNotFound):
        await gateway.chat_info("nobody")
    with pytest.raises(ChatNotFound):
        await gateway.chat_info(999)


async def test_topics_use_get_forum_topics(gateway: TelethonGateway, stub: Stub) -> None:
    topic = types.ForumTopic(
        id=9,
        date=WHEN,
        title="Ideas",
        icon_color=0,
        top_message=1,
        read_inbox_max_id=0,
        read_outbox_max_id=0,
        unread_count=2,
        unread_mentions_count=0,
        unread_reactions_count=0,
        from_id=types.PeerUser(7),
        notify_settings=types.PeerNotifySettings(),
        pinned=True,
        peer=types.PeerChannel(123),
        unread_poll_votes_count=0,
    )
    stub.responses[functions.messages.GetForumTopicsRequest] = SimpleNamespace(topics=[topic])
    assert await gateway.topics_list(TEAM_ID, 100) == [
        TopicInfo(topic_id=9, title="Ideas", unread=2, closed=False, pinned=True)
    ]
    assert requests_of(stub, functions.messages.GetForumTopicsRequest)[0].limit == 100


async def test_read_messages_pages_and_fetches_by_id(gateway: TelethonGateway, stub: Stub) -> None:
    stub.messages = [message(3, "newest", out=True), message(2, "older")]
    page = await gateway.read_messages(TEAM_ID, 50, before_id=4, topic_id=9)
    assert [(m.id, m.outgoing) for m in page] == [(3, True), (2, False)]
    assert stub.calls[-1] == ("get_messages", (TEAM_ID,), {"limit": 50, "max_id": 4, "reply_to": 9})
    named = await gateway.read_messages(TEAM_ID, 2, ids=[2, 99])
    assert [m.id for m in named] == [2]
    no_read_acknowledgement(stub)


async def test_a_message_view_carries_media_forward_and_topic(
    gateway: TelethonGateway, stub: Stub
) -> None:
    stub.messages = [
        message(
            5,
            "",
            media=object(),
            photo=object(),
            file=SimpleNamespace(size=12, name="a.jpg"),
            fwd_from=SimpleNamespace(from_name="Carol"),
            reply_to=SimpleNamespace(forum_topic=True, reply_to_msg_id=9, reply_to_top_id=None),
            edit_date=WHEN,
        )
    ]
    (view,) = await gateway.read_messages(TEAM_ID, 1)
    assert view.media is not None
    assert (view.media.kind, view.media.size_bytes, view.media.file_name) == ("photo", 12, "a.jpg")
    assert (view.forwarded_from, view.topic_id, view.edited) == ("Carol", 9, True)
    assert view.sender is not None
    assert view.sender.name == "Ada L"


async def test_an_unknown_topic_is_topic_not_found(gateway: TelethonGateway, stub: Stub) -> None:
    stub.fail["get_messages"] = errors.RPCError(None, "TOPIC_ID_INVALID", 400)
    with pytest.raises(TopicNotFound):
        await gateway.read_messages(TEAM_ID, 5, topic_id=77)


async def test_pinned_and_search_in_one_chat(gateway: TelethonGateway, stub: Stub) -> None:
    stub.messages = [message(1)]
    await gateway.read_pinned(TEAM_ID, 50)
    assert isinstance(stub.calls[-1][2]["filter"], types.InputMessagesFilterPinned)
    await gateway.search_chat(TEAM_ID, "plan", 20)
    assert stub.calls[-1][2] == {"limit": 20, "search": "plan"}
    no_read_acknowledgement(stub)


async def test_search_across_chats_names_each_chat(gateway: TelethonGateway, stub: Stub) -> None:
    stub.responses[functions.messages.SearchGlobalRequest] = SimpleNamespace(
        messages=[message(4, "the plan"), message(5, peer_id=types.PeerChannel(999))],
        chats=[TEAM],
        users=[ADA],
    )
    (hit,) = await gateway.search_global("plan", 20)
    assert (hit.chat.id, hit.chat.name, hit.message.text) == (TEAM_ID, "Team", "the plan")
    assert requests_of(stub, functions.messages.SearchGlobalRequest)[0].q == "plan"


async def test_members_with_roles_and_rights(gateway: TelethonGateway, stub: Stub) -> None:
    admin = types.User(id=8, first_name="Bob")
    admin.participant = types.ChannelParticipantAdmin(
        user_id=8, promoted_by=1, date=WHEN, admin_rights=types.ChatAdminRights(ban_users=True)
    )
    owner = types.User(id=9, first_name="Owner")
    owner.participant = types.ChannelParticipantCreator(
        user_id=9, admin_rights=types.ChatAdminRights()
    )
    plain = types.User(id=10, first_name="Plain")
    stub.participants = [admin, owner, plain]
    found = await gateway.members(TEAM_ID, "admins", 100)
    assert [(p.id, p.role) for p in found] == [(8, "admin"), (9, "creator"), (10, "member")]
    assert dict(found[0].admin_rights or ())["ban_users"] is True
    assert all(dict(found[1].admin_rights or ()).values())
    assert isinstance(stub.calls[-1][2]["filter"], types.ChannelParticipantsAdmins)
    await gateway.members(TEAM_ID, "bots", 5)
    assert isinstance(stub.calls[-1][2]["filter"], types.ChannelParticipantsBots)


async def test_current_admin_rights(gateway: TelethonGateway, stub: Stub) -> None:
    assert not any((await gateway.admins(TEAM_ID, 8)).values())
    stub.permissions = SimpleNamespace(
        is_creator=False,
        is_admin=True,
        participant=SimpleNamespace(admin_rights=types.ChatAdminRights(pin_messages=True)),
    )
    rights = await gateway.admins(TEAM_ID, 8)
    assert rights["pin_messages"] is True
    assert rights["ban_users"] is False
    stub.permissions = SimpleNamespace(is_creator=True)
    assert all((await gateway.admins(TEAM_ID, 8)).values())


async def test_contacts_and_users(gateway: TelethonGateway, stub: Stub) -> None:
    stub.responses[functions.contacts.GetContactsRequest] = SimpleNamespace(users=[ADA])
    (contact,) = await gateway.contacts()
    assert (contact.name, contact.phone, contact.is_contact) == ("Ada L", "4900", True)
    stub.responses[functions.users.GetUsersRequest] = [ADA, BOB]
    people = await gateway.user_profile([7, 8])
    assert [(p.id, p.is_bot) for p in people] == [(7, False), (8, True)]
    assert len(requests_of(stub, functions.users.GetUsersRequest)) == 1  # one batched call
    stub.responses[functions.users.GetFullUserRequest] = SimpleNamespace(
        full_user=SimpleNamespace(about="Ada's bio")
    )
    (ada,) = await gateway.user_profile("ada")
    assert ada.bio == "Ada's bio"


async def test_user_lookups_that_fail(gateway: TelethonGateway, stub: Stub) -> None:
    with pytest.raises(UserNotFound) as missing:
        await gateway.user_profile([7, 404])
    assert missing.value.ids == (404,)
    with pytest.raises(NotAUser):
        await gateway.user_profile("team_chat")
    with pytest.raises(UserNotFound):
        await gateway.user_profile("nobody")


async def test_own_profile(gateway: TelethonGateway, stub: Stub) -> None:
    stub.responses[functions.users.GetFullUserRequest] = SimpleNamespace(
        full_user=SimpleNamespace(about="my bio")
    )
    profile = await gateway.own_profile()
    assert (profile.id, profile.first_name, profile.bio, profile.has_photo) == (
        42,
        "Me",
        "my bio",
        False,
    )


def with_media(stub: Stub, data: bytes, *, size: int | None = None, name: str = "a.pdf") -> Any:
    """A message with a document; returns the Document that iter_download must be given."""
    media = SimpleNamespace(kind="document")
    document = SimpleNamespace(kind="the document")
    file = SimpleNamespace(size=len(data) if size is None else size, name=name)
    found = message(3, media=media, file=file, document=document)
    stub.messages = [found]
    stub.media_data = data
    return document


def downloads(stub: Stub) -> list[dict[str, Any]]:
    return [call[2] for call in stub.calls if call[0] == "iter_download"]


async def test_media_info_reads_kind_size_and_name(gateway: TelethonGateway, stub: Stub) -> None:
    with_media(stub, b"12345", name="r.pdf")
    assert await gateway.media_info(TEAM_ID, 3) == MediaInfo("document", 5, "r.pdf")
    with_media(stub, b"", size=0)
    assert (await gateway.media_info(TEAM_ID, 3)) == MediaInfo("document", None, "a.pdf")
    stub.messages = [message(3)]
    assert await gateway.media_info(TEAM_ID, 3) is None  # a message with no media
    stub.messages = []
    with pytest.raises(TelegramError, match="MESSAGE_ID_INVALID"):
        await gateway.media_info(TEAM_ID, 3)
    no_read_acknowledgement(stub)


async def test_download_parts_use_iter_download_at_their_offsets(
    gateway: TelethonGateway, stub: Stub
) -> None:
    media = with_media(stub, b"0123456789")
    assert await gateway.download_part(TEAM_ID, 3, 0, 4) == b"0123"
    assert await gateway.download_part(TEAM_ID, 3, 8, 4) == b"89"
    assert await gateway.download_part(TEAM_ID, 3, 12, 4) == b""
    asked = downloads(stub)
    assert [(c["offset"], c["request_size"], c["limit"]) for c in asked] == [
        (0, 4, 1),
        (8, 4, 1),
        (12, 4, 1),
    ]
    assert all(call[1] == (media,) for call in stub.calls if call[0] == "iter_download")
    assert [c[0] for c in stub.calls].count("get_messages") == 1  # the message is read once
    no_read_acknowledgement(stub)


async def test_a_link_preview_photo_downloads_the_photo_not_the_web_page(
    gateway: TelethonGateway, stub: Stub
) -> None:
    # A channel post whose picture is a link preview: message.media is MessageMediaWebPage,
    # which iter_download cannot cast (it raised TypeError on the real account); Telethon's
    # message.photo finds the preview's Photo.
    web_page = SimpleNamespace(kind="web page")
    photo = SimpleNamespace(kind="the preview photo")
    file = SimpleNamespace(size=6, name=None)
    stub.messages = [message(3, media=web_page, file=file, photo=photo)]
    stub.media_data = b"abcdef"
    assert await gateway.media_info(TEAM_ID, 3) == MediaInfo("photo", 6, None)
    assert await gateway.download_part(TEAM_ID, 3, 0, 4) == b"abcd"
    assert [call[1] for call in stub.calls if call[0] == "iter_download"] == [(photo,)]


async def test_media_with_nothing_to_download_is_no_media(
    gateway: TelethonGateway, stub: Stub
) -> None:
    # A location, a poll or a web page without a picture: there is a media field and even a
    # file view, but neither a Photo nor a Document to download.
    file = SimpleNamespace(size=0, name=None)
    stub.messages = [message(3, media=SimpleNamespace(kind="geo"), file=file)]
    assert await gateway.media_info(TEAM_ID, 3) is None


async def test_a_link_preview_document_downloads_the_document_with_its_name_and_size(
    gateway: TelethonGateway, stub: Stub
) -> None:
    web_page = SimpleNamespace(kind="web page")
    document = SimpleNamespace(kind="the preview document")
    file = SimpleNamespace(size=9, name="report.pdf")
    stub.messages = [message(3, media=web_page, file=file, document=document)]
    stub.media_data = b"012345678"
    assert await gateway.media_info(TEAM_ID, 3) == MediaInfo("document", 9, "report.pdf")
    assert await gateway.download_part(TEAM_ID, 3, 4, 4) == b"4567"
    assert [call[1] for call in stub.calls if call[0] == "iter_download"] == [(document,)]


async def test_download_part_with_nothing_left_to_fetch_is_refused_without_a_request(
    gateway: TelethonGateway, stub: Stub
) -> None:
    with_media(stub, b"0123456789")
    assert await gateway.media_info(TEAM_ID, 3) is not None  # the message is kept for its parts
    kept = stub.messages[0]
    kept.photo = None
    kept.document = None
    with pytest.raises(TelegramError, match="MEDIA_EMPTY"):
        await gateway.download_part(TEAM_ID, 3, 0, 4)
    assert downloads(stub) == []


async def test_reads_and_downloads_agree_on_what_a_media_is(
    gateway: TelethonGateway, stub: Stub
) -> None:
    # A location: a read shows media of kind other, and there is nothing to download.
    location = message(3, media=SimpleNamespace(kind="geo"))
    # A link preview with a photo: a read shows a photo, and the download reports that photo.
    preview = message(
        4,
        media=SimpleNamespace(kind="web page"),
        photo=SimpleNamespace(kind="the preview photo"),
        file=SimpleNamespace(size=6, name=None),
    )
    stub.messages = [preview, location]
    views = {view.id: view for view in await gateway.read_messages(TEAM_ID, 2)}
    assert views[3].media is not None
    assert views[3].media.kind == "other"
    assert await gateway.media_info(TEAM_ID, 3) is None
    assert views[4].media is not None
    assert (views[4].media.kind, views[4].media.size_bytes) == ("photo", 6)
    assert await gateway.media_info(TEAM_ID, 4) == MediaInfo("photo", 6, None)
    no_read_acknowledgement(stub)


async def test_an_expired_file_reference_is_reported_and_the_message_read_again(
    gateway: TelethonGateway, stub: Stub
) -> None:
    with_media(stub, b"0123456789")
    stub.fail["iter_download"] = errors.FileReferenceExpiredError(None)
    with pytest.raises(FileReferenceExpired):
        await gateway.download_part(TEAM_ID, 3, 4, 4)
    assert await gateway.download_part(TEAM_ID, 3, 4, 4) == b"4567"
    assert [c[0] for c in stub.calls].count("get_messages") == 2


async def test_a_part_that_times_out_or_drops_may_be_tried_again(stub: Stub) -> None:
    gateway = TelethonGateway(
        "", 1, "0" * 32, client_factory=lambda *a, **k: stub, call_timeout=0.05
    )
    with_media(stub, b"0123456789")
    stub.fail["iter_download"] = "hang"
    with pytest.raises(PartRetryable):
        await gateway.download_part(TEAM_ID, 3, 0, 4)
    stub.fail["iter_download"] = ConnectionError()
    with pytest.raises(PartRetryable):
        await gateway.download_part(TEAM_ID, 3, 0, 4)
    stub.fail["call"] = "hang"
    with pytest.raises(PartRetryable):
        await gateway.upload_part(1, 0, 1, b"x", big=False)
    stub.messages = [message(3)]
    gateway_without = TelethonGateway("", 1, "0" * 32, client_factory=lambda *a, **k: stub)
    with pytest.raises(TelegramError, match="MEDIA_EMPTY"):
        await gateway_without.download_part(TEAM_ID, 3, 0, 4)


async def test_small_files_use_save_file_part_and_big_ones_save_big_file_part(
    gateway: TelethonGateway, stub: Stub
) -> None:
    await gateway.upload_part(77, 0, 2, b"abc", big=False)
    await gateway.upload_part(78, 1, 30, b"def", big=True)
    (small,) = requests_of(stub, functions.upload.SaveFilePartRequest)
    assert (small.file_id, small.file_part, small.bytes) == (77, 0, b"abc")
    (big,) = requests_of(stub, functions.upload.SaveBigFilePartRequest)
    assert (big.file_id, big.file_part, big.file_total_parts, big.bytes) == (78, 1, 30, b"def")
    stub.responses[functions.upload.SaveFilePartRequest] = False
    with pytest.raises(TelegramError, match="not stored"):
        await gateway.upload_part(77, 1, 2, b"x", big=False)


# Writes ----------------------------------------------------------------------------------------


async def test_message_writes_use_the_high_level_methods(
    gateway: TelethonGateway, stub: Stub
) -> None:
    edited = await gateway.edit(TEAM_ID, 3, "new")
    assert edited.edited
    await gateway.delete(TEAM_ID, [1, 2], True)
    assert await gateway.forward(TEAM_ID, [1, 2], FAMILY_ID) == [900, 901]
    await gateway.pin(TEAM_ID, 1)
    await gateway.unpin(TEAM_ID, 1)
    names = [(name, args, kwargs) for name, args, kwargs in stub.calls]
    assert names == [
        ("edit_message", (TEAM_ID, 3, "new"), {"parse_mode": None}),
        ("delete_messages", (TEAM_ID, [1, 2]), {"revoke": True}),
        ("forward_messages", (FAMILY_ID, [1, 2]), {"from_peer": TEAM_ID}),
        ("pin_message", (TEAM_ID, 1), {"notify": False}),
        ("unpin_message", (TEAM_ID, 1), {}),
    ]


async def test_a_write_that_fails_after_sending_is_outcome_unknown(
    gateway: TelethonGateway, stub: Stub
) -> None:
    stub.fail["delete_messages"] = ConnectionError()
    with pytest.raises(OutcomeUnknown):
        await gateway.delete(TEAM_ID, [1], False)


async def test_react_and_topic_create(gateway: TelethonGateway, stub: Stub) -> None:
    await gateway.react(TEAM_ID, 3, "👍")
    (reaction,) = requests_of(stub, functions.messages.SendReactionRequest)
    assert (reaction.msg_id, reaction.reaction[0].emoticon) == (3, "👍")
    created = SimpleNamespace(
        message=SimpleNamespace(id=11, action=types.MessageActionTopicCreate("Plans", 0))
    )
    stub.responses[functions.messages.CreateForumTopicRequest] = SimpleNamespace(
        updates=[SimpleNamespace(), created]
    )
    assert await gateway.topic_create(TEAM_ID, "Plans") == 11
    stub.responses[functions.messages.CreateForumTopicRequest] = SimpleNamespace(updates=[])
    with pytest.raises(OutcomeUnknown):
        await gateway.topic_create(TEAM_ID, "Plans")


async def test_creating_groups_and_channels(gateway: TelethonGateway, stub: Stub) -> None:
    stub.responses[functions.messages.CreateChatRequest] = SimpleNamespace(
        updates=SimpleNamespace(chats=[FAMILY])
    )
    assert await gateway.create_group("Fam", [7], supergroup=False) == FAMILY_ID
    stub.responses[functions.channels.CreateChannelRequest] = SimpleNamespace(chats=[TEAM])
    assert await gateway.create_group("Team", [7, 8], supergroup=True) == TEAM_ID
    (invite,) = requests_of(stub, functions.channels.InviteToChannelRequest)
    assert len(invite.users) == 2
    assert await gateway.create_channel("News") == TEAM_ID
    channels = requests_of(stub, functions.channels.CreateChannelRequest)
    assert [(c.megagroup, c.broadcast) for c in channels] == [(True, None), (None, True)]
    with pytest.raises(UserNotFound):
        await gateway.create_group("x", [404], supergroup=False)


async def test_joining_by_username_and_by_invite(gateway: TelethonGateway, stub: Stub) -> None:
    stub.responses[functions.channels.JoinChannelRequest] = SimpleNamespace(chats=[TEAM])
    stub.responses[functions.messages.ImportChatInviteRequest] = SimpleNamespace(chats=[FAMILY])
    assert (await gateway.join("team_chat", None)).id == TEAM_ID
    assert (await gateway.join(None, "AbC")).id == FAMILY_ID
    assert requests_of(stub, functions.messages.ImportChatInviteRequest)[0].hash == "AbC"


async def test_leaving_a_group_and_a_channel(gateway: TelethonGateway, stub: Stub) -> None:
    await gateway.leave(FAMILY_ID)
    await gateway.leave(TEAM_ID)
    assert requests_of(stub, functions.messages.DeleteChatUserRequest)[0].chat_id == 55
    assert len(requests_of(stub, functions.channels.LeaveChannelRequest)) == 1


async def test_invite_links_and_admin_rights(gateway: TelethonGateway, stub: Stub) -> None:
    stub.responses[functions.messages.ExportChatInviteRequest] = SimpleNamespace(
        link="https://t.me/+x"
    )
    assert await gateway.invite_link(TEAM_ID, 3600, 5) == "https://t.me/+x"
    (export,) = requests_of(stub, functions.messages.ExportChatInviteRequest)
    assert export.usage_limit == 5
    assert export.expire_date is not None
    assert export.expire_date - datetime.now(UTC) <= timedelta(seconds=3600)
    await gateway.set_admin_rights(TEAM_ID, 7, {"pin_messages": True})
    (edit,) = requests_of(stub, functions.channels.EditAdminRequest)
    assert (edit.admin_rights.pin_messages, edit.admin_rights.ban_users) == (True, False)


async def test_editing_a_chat(gateway: TelethonGateway, stub: Stub) -> None:
    await gateway.edit_chat(FAMILY_ID, "Fam 2", "About")
    assert requests_of(stub, functions.messages.EditChatTitleRequest)[0].title == "Fam 2"
    assert requests_of(stub, functions.messages.EditChatAboutRequest)[0].about == "About"
    await gateway.edit_chat(TEAM_ID, "Team 2", None)
    assert requests_of(stub, functions.channels.EditTitleRequest)[0].title == "Team 2"
    assert not requests_of(stub, functions.messages.EditChatPhotoRequest)


async def test_editing_the_own_profile(gateway: TelethonGateway, stub: Stub) -> None:
    await gateway.edit_own_profile("Ada", None, "bio", "ada_new")
    (profile,) = requests_of(stub, functions.account.UpdateProfileRequest)
    assert (profile.first_name, profile.last_name, profile.about) == ("Ada", None, "bio")
    assert requests_of(stub, functions.account.UpdateUsernameRequest)[0].username == "ada_new"
    stub.requests.clear()
    await gateway.edit_own_profile(None, None, None, "")
    assert [type(r) for r in stub.requests] == [functions.account.UpdateUsernameRequest]


SMALL = UploadedFile(file_id=5, parts=2, name="r.pdf", big=False, md5="ab" * 16)
BIG = UploadedFile(file_id=6, parts=40, name="film.mp4", big=True)


async def test_the_final_send_uses_the_stored_parts_under_their_name(
    gateway: TelethonGateway, stub: Stub
) -> None:
    sent = await gateway.send_uploaded_file(TEAM_ID, SMALL, "caption")
    assert sent.id == 77
    (send,) = [c for c in stub.calls if c[0] == "send_file"]
    assert send[1][0] == TEAM_ID
    assert send[1][1] == types.InputFile(id=5, parts=2, name="r.pdf", md5_checksum="ab" * 16)
    assert send[2] == {"caption": "caption", "parse_mode": None}
    await gateway.send_uploaded_file(TEAM_ID, BIG, "")
    assert stub.calls[-1][1][1] == types.InputFileBig(id=6, parts=40, name="film.mp4")


async def test_photo_final_requests_use_the_stored_parts(
    gateway: TelethonGateway, stub: Stub
) -> None:
    await gateway.set_chat_photo(FAMILY_ID, SMALL)
    (basic,) = requests_of(stub, functions.messages.EditChatPhotoRequest)
    assert basic.chat_id == FAMILY.id
    assert basic.photo.file.id == SMALL.file_id
    await gateway.set_chat_photo(TEAM_ID, SMALL)
    assert len(requests_of(stub, functions.channels.EditPhotoRequest)) == 1
    await gateway.set_profile_photo(BIG)
    (profile,) = requests_of(stub, functions.photos.UploadProfilePhotoRequest)
    assert profile.file == types.InputFileBig(id=6, parts=40, name="film.mp4")


async def test_a_final_request_that_hangs_is_outcome_unknown(stub: Stub) -> None:
    gateway = TelethonGateway(
        "", 1, "0" * 32, client_factory=lambda *a, **k: stub, call_timeout=0.05
    )
    stub.fail["send_file"] = "hang"
    with pytest.raises(OutcomeUnknown):
        await gateway.send_uploaded_file(TEAM_ID, SMALL, "")
    stub.fail["call"] = ConnectionError()
    with pytest.raises(OutcomeUnknown):
        await gateway.set_profile_photo(SMALL)


async def test_a_hanging_write_times_out_as_outcome_unknown(stub: Stub) -> None:
    gateway = TelethonGateway(
        "", 1, "0" * 32, client_factory=lambda *a, **k: stub, call_timeout=0.05
    )
    stub.fail["pin_message"] = "hang"
    with pytest.raises(OutcomeUnknown):
        await gateway.pin(TEAM_ID, 1)
