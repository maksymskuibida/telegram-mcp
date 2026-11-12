"""The fake gateway's U4 calls (C3 FakeGateway; BR22.1, NFR9.1): each returns its seeded data or
raises the gateway's error, no read call acknowledges reading, and writes change the state."""

from __future__ import annotations

import io

import pytest

from support.samples import ALICE, FAMILY, FORUM, TEAM
from telegram_mcp.gateway.fake import ChatExtra, FakeGateway
from telegram_mcp.gateway.models import (
    ChatNotFound,
    FolderRef,
    NotAUser,
    TelegramError,
    TooLarge,
    TopicNotFound,
    UserNotFound,
)


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
    await fake.download_media(ALICE.id, 2, io.BytesIO(), 100)


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
    assert page[0].outgoing and page[0].sender is not None
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


async def test_download_writes_bytes_and_refuses_large_media(fake: FakeGateway) -> None:
    target = io.BytesIO()
    assert await fake.download_media(ALICE.id, 2, target, 100) == 6
    assert target.getvalue() == b"abcdef"
    with pytest.raises(TooLarge):
        await fake.download_media(ALICE.id, 2, io.BytesIO(), 3)
    with pytest.raises(TelegramError):
        await fake.download_media(ALICE.id, 1, io.BytesIO(), 100)


async def test_message_writes_change_the_state(fake: FakeGateway) -> None:
    edited = await fake.edit(ALICE.id, 3, "changed")
    assert edited.edited and edited.text == "changed"
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
    await fake.edit_chat(TEAM.id, "Team 2", "About", io.BytesIO(b"img"), "p.jpg")
    details = await fake.chat_info(TEAM.id)
    assert (details.name, details.description) == ("Team 2", "About")
    assert fake.chat_photos == [(TEAM.id, b"img")]
    with pytest.raises(TelegramError):
        await fake.join(None, "unknown")


async def test_profile_and_file_writes(fake: FakeGateway) -> None:
    await fake.edit_own_profile("Maks", None, "bio", None, io.BytesIO(b"me"), "me.jpg")
    profile = await fake.own_profile()
    assert (profile.first_name, profile.bio, profile.has_photo) == ("Maks", "bio", True)
    sent = await fake.send_file(ALICE.id, io.BytesIO(b"report"), "r.pdf", "caption")
    assert fake.sent_files[0].data == b"report"
    assert sent.chat_id == ALICE.id


async def test_a_write_can_be_dropped_after_sending(fake: FakeGateway) -> None:
    from telegram_mcp.gateway.models import OutcomeUnknown

    fake.script("react", drop_after_send=True)
    with pytest.raises(OutcomeUnknown):
        await fake.react(ALICE.id, 1, "👍")
    assert fake.reactions == [(ALICE.id, 1, "👍")]
