"""The fake gateway's U4 calls (C3 FakeGateway; BR22.1, NFR9.1): each returns its seeded data or
raises the gateway's error, no read call acknowledges reading, and writes change the state."""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from support.samples import ALICE, FAMILY, FORUM, TEAM
from telegram_mcp.gateway.fake import ChatExtra, FakeGateway
from telegram_mcp.gateway.models import (
    ChatNotFound,
    FileReferenceExpired,
    FloodWait,
    FolderRef,
    MediaInfo,
    NotAUser,
    PartRetryable,
    TelegramError,
    TopicNotFound,
    UploadedFile,
    UserNotFound,
)


def uploaded(fake: FakeGateway, file_id: int, data: bytes, *, name: str = "r.pdf") -> UploadedFile:
    """The UploadedFile a test stores in two parts, as the transfer queue would."""
    return UploadedFile(
        file_id=file_id,
        parts=2,
        name=name,
        big=False,
        md5=hashlib.md5(data).hexdigest(),  # noqa: S324 (Telegram's own checksum)
    )


async def store(fake: FakeGateway, file_id: int, data: bytes) -> UploadedFile:
    half = len(data) // 2
    await fake.upload_part(file_id, 0, 2, data[:half], big=False)
    await fake.upload_part(file_id, 1, 2, data[half:], big=False)
    return uploaded(fake, file_id, data)


@pytest.fixture
def fake(fake_gateway: FakeGateway) -> FakeGateway:
    fake_gateway.topics[FORUM.id] = {9: "Ideas", 10: "Off topic"}
    fake_gateway.extras[TEAM.id] = ChatExtra(username="team_chat", description="We build")
    fake_gateway.add_user(501, "Bob", username="bob", is_contact=True, phone="+100")
    fake_gateway.add_user(502, "Robot", is_bot=True)
    fake_gateway.add_member(TEAM.id, 501, "admin", {"pin_messages": True})
    fake_gateway.add_member(TEAM.id, 502)
    fake_gateway.add_message(ALICE.id, "hello there")
    fake_gateway.add_message(ALICE.id, "photo", media="photo", content=b"abcdef", pinned=True)
    fake_gateway.add_message(ALICE.id, "mine", outgoing=True)
    fake_gateway.add_message(FORUM.id, "in ideas", topic_id=9)
    return fake_gateway


async def run_every_read(fake: FakeGateway) -> None:
    await fake.folders()
    await fake.chat_info(ALICE.id)
    await fake.chat_info("team_chat")
    await fake.topics_list(FORUM.id, 100)
    await fake.read_messages(ALICE.id, 50)
    await fake.read_messages(ALICE.id, 50, ids=[1, 2])
    await fake.read_pinned(ALICE.id, 50)
    await fake.search_chat(ALICE.id, "hello", 20)
    await fake.search_global("hello", 20)
    await fake.members(TEAM.id, "all", 100)
    await fake.admins(TEAM.id, 501)
    await fake.contacts()
    await fake.user_profile([501])
    await fake.own_profile()
    await fake.media_info(ALICE.id, 2)
    await fake.download_part(ALICE.id, 2, 0, 4)
    await fake.upload_part(77, 0, 1, b"part", big=False)


async def test_no_read_call_acknowledges_reading(fake: FakeGateway) -> None:
    before = (await fake.unread_state(ALICE.id, None)).unread
    await run_every_read(fake)
    assert fake.read_acknowledgements() == []
    assert (await fake.unread_state(ALICE.id, None)).unread == before


async def test_reads_return_their_seeded_data(fake: FakeGateway) -> None:
    assert (await fake.folders())[:2] == [FolderRef(0, "Main list"), FolderRef(1, "Archive")]
    team = await fake.chat_info("TEAM_CHAT")
    assert (team.id, team.description, team.my_role) == (TEAM.id, "We build", "member")
    page = await fake.read_messages(ALICE.id, 2)
    assert [m.id for m in page] == [3, 2]
    assert page[0].outgoing
    assert page[0].sender is not None
    assert [m.id for m in await fake.read_messages(ALICE.id, 5, before_id=3)] == [2, 1]
    assert [m.id for m in await fake.read_pinned(ALICE.id, 50)] == [2]
    assert [m.text for m in await fake.search_chat(ALICE.id, "HELLO", 5)] == ["hello there"]
    (hit,) = await fake.search_global("ideas", 5)
    assert hit.chat.id == FORUM.id
    topics = await fake.topics_list(FORUM.id, 100)
    assert {t.topic_id for t in topics} == {9, 10}


async def test_unknown_chats_topics_and_users_raise(fake: FakeGateway) -> None:
    with pytest.raises(ChatNotFound):
        await fake.chat_info(999)
    with pytest.raises(ChatNotFound):
        await fake.chat_info("nobody_here")
    with pytest.raises(ChatNotFound):
        await fake.read_messages(999, 5)
    with pytest.raises(TopicNotFound):
        await fake.read_messages(FORUM.id, 5, topic_id=77)
    with pytest.raises(UserNotFound) as missing:
        await fake.user_profile([501, 777])
    assert missing.value.ids == (777,)
    with pytest.raises(NotAUser):
        await fake.user_profile("team_chat")


async def test_members_filter_and_admin_rights(fake: FakeGateway) -> None:
    admins = await fake.members(TEAM.id, "admins", 100)
    assert [(p.id, p.role) for p in admins] == [(501, "admin")]
    assert dict(admins[0].admin_rights or ())["pin_messages"] is True
    assert [p.id for p in await fake.members(TEAM.id, "bots", 100)] == [502]
    assert (await fake.admins(TEAM.id, 502))["pin_messages"] is False
    assert [p.id for p in await fake.contacts()] == [501]


async def test_media_info_gives_kind_size_and_name(fake: FakeGateway) -> None:
    named = fake.add_message(ALICE.id, "doc", media="document", file_name="a.pdf", content=b"x")
    assert await fake.media_info(ALICE.id, named.id) == MediaInfo("document", 1, "a.pdf")
    assert await fake.media_info(ALICE.id, 1) is None  # a message with no media
    with pytest.raises(TelegramError, match="MESSAGE_ID_INVALID"):
        await fake.media_info(ALICE.id, 999)
    with pytest.raises(ChatNotFound):
        await fake.media_info(999, 2)


async def test_a_size_of_zero_or_a_hidden_size_reads_as_unknown(fake: FakeGateway) -> None:
    empty = fake.add_message(ALICE.id, "voice", media="voice")
    hidden = fake.add_message(ALICE.id, "video", media="video", media_size=3000)
    fake.unknown_size.add((ALICE.id, hidden.id))
    assert (await fake.media_info(ALICE.id, empty.id)) == MediaInfo("voice", None)
    assert (await fake.media_info(ALICE.id, hidden.id)) == MediaInfo("video", None)


async def test_download_parts_read_the_media_at_their_offsets(fake: FakeGateway) -> None:
    assert await fake.download_part(ALICE.id, 2, 0, 4) == b"abcd"
    assert await fake.download_part(ALICE.id, 2, 4, 4) == b"ef"
    assert await fake.download_part(ALICE.id, 2, 8, 4) == b""
    sized = fake.add_message(ALICE.id, "big", media="video", media_size=10)
    assert await fake.download_part(ALICE.id, sized.id, 8, 4) == b"\0\0"  # zeros up to the size
    assert [c["offset"] for c in fake.part_calls("download_part")] == [0, 4, 8, 8]
    with pytest.raises(TelegramError):
        await fake.download_part(ALICE.id, 1, 0, 4)


async def test_part_scripts_apply_to_the_part_they_name(fake: FakeGateway) -> None:
    fake.script("download_part", error=FileReferenceExpired(), part=1)
    fake.script("download_part", error=FloodWait(10), part=2)
    fake.script("upload_part", error=PartRetryable(), part=0)
    assert await fake.download_part(ALICE.id, 2, 0, 2) == b"ab"  # part 0 is not scripted
    with pytest.raises(FileReferenceExpired):
        await fake.download_part(ALICE.id, 2, 2, 2)
    assert await fake.download_part(ALICE.id, 2, 2, 2) == b"cd"  # failed once only
    with pytest.raises(FloodWait):
        await fake.download_part(ALICE.id, 2, 4, 2)
    with pytest.raises(PartRetryable):
        await fake.upload_part(5, 0, 1, b"x", big=False)
    await fake.upload_part(5, 0, 1, b"x", big=False)
    assert fake.uploaded_parts[5] == {0: b"x"}


async def test_a_part_can_be_delayed_or_hang(fake: FakeGateway) -> None:
    waited: list[float] = []

    async def note(seconds: float) -> None:
        waited.append(seconds)

    fake.sleep = note
    fake.script("download_part", delay=12.5, part=0)
    fake.script("upload_part", hang=True)
    assert await fake.download_part(ALICE.id, 2, 0, 6) == b"abcdef"
    assert waited == [12.5]
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await fake.upload_part(9, 0, 1, b"x", big=False)
    assert 9 not in fake.uploaded_parts


async def test_final_requests_use_the_stored_parts_and_are_recorded(fake: FakeGateway) -> None:
    report = await store(fake, 11, b"report")
    sent = await fake.send_uploaded_file(ALICE.id, report, "caption")
    assert (fake.sent_files[0].data, fake.sent_files[0].caption) == (b"report", "caption")
    assert sent.chat_id == ALICE.id
    await fake.set_chat_photo(TEAM.id, await store(fake, 12, b"img!"))
    await fake.set_profile_photo(await store(fake, 13, b"me!!"))
    assert fake.chat_photos == [(TEAM.id, b"img!")]
    assert fake.profile_photos == [b"me!!"]
    assert (await fake.own_profile()).has_photo
    assert [name for name, _ in fake.final_requests()] == [
        "send_uploaded_file",
        "set_chat_photo",
        "set_profile_photo",
    ]


async def test_a_final_request_needs_every_part_and_the_checksum(fake: FakeGateway) -> None:
    await fake.upload_part(21, 0, 2, b"abc", big=False)
    with pytest.raises(TelegramError, match="FILE_PARTS_INVALID"):
        await fake.send_uploaded_file(ALICE.id, uploaded(fake, 21, b"abcdef"), "")
    await fake.upload_part(21, 1, 2, b"def", big=False)
    wrong = UploadedFile(file_id=21, parts=2, name="r.pdf", big=False, md5="0" * 32)
    with pytest.raises(TelegramError, match="MD5"):
        await fake.send_uploaded_file(ALICE.id, wrong, "")
    big = UploadedFile(file_id=21, parts=2, name="r.pdf", big=True)  # big files carry no md5
    await fake.send_uploaded_file(ALICE.id, big, "")
    assert fake.sent_files[-1].data == b"abcdef"
    assert len(fake.final_requests()) == 3  # refused ones are recorded too


async def test_message_writes_change_the_state(fake: FakeGateway) -> None:
    edited = await fake.edit(ALICE.id, 3, "changed")
    assert edited.edited
    assert edited.text == "changed"
    new_ids = await fake.forward(ALICE.id, [1], FAMILY.id)
    assert [m.forwarded_from for m in await fake.read_messages(FAMILY.id, 5)] == ["Alice"]
    assert len(new_ids) == 1
    await fake.pin(ALICE.id, 1)
    await fake.unpin(ALICE.id, 2)
    assert [m.id for m in await fake.read_pinned(ALICE.id, 5)] == [1]
    await fake.react(ALICE.id, 1, "👍")
    assert fake.reactions == [(ALICE.id, 1, "👍")]
    await fake.delete(ALICE.id, [1], for_everyone=False)
    assert 1 not in [m.id for m in await fake.read_messages(ALICE.id, 50)]


async def test_chat_writes_change_the_state(fake: FakeGateway) -> None:
    group = await fake.create_group("Trip", [501], supergroup=False)
    assert (await fake.chat_info(group)).type == "group"
    channel = await fake.create_channel("News 2")
    assert (await fake.chat_info(channel)).my_role == "creator"
    assert await fake.topic_create(FORUM.id, "New") == 11
    fake.invite_targets["AbC"] = FAMILY.id
    joined = await fake.join(None, "AbC")
    assert joined.id == FAMILY.id
    await fake.leave(FAMILY.id)
    assert (await fake.chat_info(FAMILY.id)).my_role == "none"
    assert (await fake.invite_link(TEAM.id, 60, 2)).startswith("https://t.me/+")
    await fake.set_admin_rights(TEAM.id, 502, {"pin_messages": True})
    assert (await fake.admins(TEAM.id, 502))["pin_messages"] is True
    await fake.edit_chat(TEAM.id, "Team 2", "About")
    details = await fake.chat_info(TEAM.id)
    assert (details.name, details.description) == ("Team 2", "About")
    assert fake.chat_photos == []
    with pytest.raises(TelegramError):
        await fake.join(None, "unknown")


async def test_profile_writes(fake: FakeGateway) -> None:
    await fake.edit_own_profile("Maks", None, "bio", None)
    profile = await fake.own_profile()
    assert (profile.first_name, profile.bio, profile.has_photo) == ("Maks", "bio", False)


async def test_a_write_can_be_dropped_after_sending(fake: FakeGateway) -> None:
    from telegram_mcp.gateway.models import OutcomeUnknown

    fake.script("react", drop_after_send=True)
    with pytest.raises(OutcomeUnknown):
        await fake.react(ALICE.id, 1, "👍")
    assert fake.reactions == [(ALICE.id, 1, "👍")]
