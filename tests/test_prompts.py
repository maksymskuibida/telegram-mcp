"""The kind table and the question text, written before the code (team Testing Posture:
confirmation logic is test-first).

Covers BR10.1, BR11.1 to BR11.7 and BR11.9; NFR4.1 to NFR4.3."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from support.samples import ALICE, FAMILY, FORUM, TEAM
from telegram_mcp.gateway.models import ChatRef
from telegram_mcp.prompts import (
    KINDS,
    MESSAGE_LIMIT,
    QUESTION_LIMIT,
    AdminRight,
    FieldChange,
    FileFields,
    HeadlineDetails,
    MessageExcerpt,
    Person,
    PromptError,
    PromptFields,
    TopicRef,
    check_message_ids,
    format_messages,
    format_size,
    normalize_rights,
    render_question,
    visible,
)

ID = "abcdefghij"
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
OWN = ChatRef(id=4242, name="Saved Messages", type="user", saved_messages=True)
ALICE_LABEL = "«Alice» (user, id 101)"
TEAM_LABEL = "«Team» (supergroup, id -1000000000303)"
FORUM_LABEL = "«Builders» (forum, id -1000000000505)"
FAMILY_LABEL = "«Family» (group, id -202)"

M11 = MessageExcerpt(
    id=11, sender="Alice", date=datetime(2026, 11, 11, 9, 5, tzinfo=UTC), text="first"
)
M12 = MessageExcerpt(
    id=12,
    sender="Bob",
    date=datetime(2026, 11, 11, 9, 6, tzinfo=UTC),
    text="",
    media="photo",
)
M11_SHOWN = "#11 Alice, 2026-11-11 09:05 UTC:\nfirst"
M12_SHOWN = "#12 Bob, 2026-11-11 09:06 UTC:\n[photo]"
REPORT = FileFields(path="/Users/p/outbox/report.pdf", name="report.pdf", size_bytes=1_536_000)
REPORT_SHOWN = "File: /Users/p/outbox/report.pdf\nName: report.pdf, 1.5 MB (1536000 bytes)"
BOB = Person(id=102, name="Bob")


def fields(action: str, tool: str, **values: Any) -> PromptFields:
    return PromptFields(action=action, tool=tool, **values)


def ask(prompt: PromptFields) -> tuple[str, tuple[str, str]]:
    return render_question(ID, prompt, now=NOW)


def refusal(prompt: PromptFields) -> PromptError:
    with pytest.raises(PromptError) as raised:
        ask(prompt)
    return raised.value


# BR10.1: the closed kind table -----------------------------------------------------------------

C2_TOOLS = {
    "send": ("tg_messages_send",),
    "file_send": ("tg_files_send",),
    "edit": ("tg_messages_edit",),
    "delete": ("tg_messages_delete",),
    "forward": ("tg_messages_forward",),
    "react": ("tg_messages_react",),
    "pin": ("tg_messages_pin", "tg_messages_unpin"),
    "topic_create": ("tg_topics_create",),
    "chat_create": ("tg_chats_create",),
    "join": ("tg_chats_join",),
    "leave": ("tg_chats_leave",),
    "chat_edit": ("tg_chats_edit",),
    "invite": ("tg_invites_create",),
    "admin": ("tg_admins_set",),
    "profile_edit": ("tg_profile_edit",),
    "mark": ("tg_chats_mark_read", "tg_messages_read"),
}


def test_the_kind_table_has_exactly_the_fifteen_write_kinds_and_mark() -> None:
    assert set(KINDS) == set(C2_TOOLS)
    for kind, tools in C2_TOOLS.items():
        assert KINDS[kind].write_kind == kind
        assert KINDS[kind].tools == tools


def test_only_chat_create_and_profile_edit_and_join_need_no_resolved_chat() -> None:
    without_chat = {kind for kind, spec in KINDS.items() if not spec.needs_chat}
    assert without_chat == {"chat_create", "profile_edit", "join"}


def test_the_kinds_that_list_messages_and_may_carry_a_file() -> None:
    assert {k for k, s in KINDS.items() if s.needs_messages} == {
        "delete",
        "forward",
        "react",
        "pin",
    }
    assert {k for k, s in KINDS.items() if s.may_have_file} == {
        "file_send",
        "chat_edit",
        "profile_edit",
    }


def test_a_kind_outside_the_table_is_not_allowed() -> None:
    error = refusal(fields("teleport", "tg_teleport", chat=ALICE, content="x"))
    assert error.code == "not_allowed"
    assert error.message == "This action has no confirmation rule, so it cannot run."


# BR11.1, BR11.2, BR11.4, BR11.6: every kind's exact question and descriptions -----------------

CASES: list[tuple[PromptFields, str, tuple[str, str]]] = [
    (
        fields("send", "tg_messages_send", chat=ALICE, content="hello"),
        f"Send this message to {ALICE_LABEL}?\nhello",
        ("Send it", "Do not send"),
    ),
    (
        fields(
            "send",
            "tg_messages_send",
            chat=TEAM,
            content="hi",
            details=HeadlineDetails(message_id=5),
        ),
        f"Send this message to {TEAM_LABEL}? Reply to message 5.\nhi",
        ("Send it", "Do not send"),
    ),
    (
        fields("send", "tg_messages_send", chat=TEAM, content="hi", topic=TopicRef(9, "")),
        f"Send this message to {TEAM_LABEL}? In topic 9.\nhi",
        ("Send it", "Do not send"),
    ),
    (
        fields("send", "tg_messages_send", chat=FORUM, content="hi", topic=TopicRef(9, "Ideas")),
        f"Send this message to {FORUM_LABEL} in topic «Ideas» (id 9)?\nhi",
        ("Send it", "Do not send"),
    ),
    (
        fields("send", "tg_messages_send", chat=OWN, content="note"),
        "Send this message to your Saved Messages (id 4242)?\nnote",
        ("Send it", "Do not send"),
    ),
    (
        fields("file_send", "tg_files_send", chat=ALICE, content="the report", file=REPORT),
        f"Send this file to {ALICE_LABEL}?\n{REPORT_SHOWN}\nthe report",
        ("Send the file", "Do not send"),
    ),
    (
        fields("file_send", "tg_files_send", chat=ALICE, file=REPORT),
        f"Send this file to {ALICE_LABEL}?\n{REPORT_SHOWN}",
        ("Send the file", "Do not send"),
    ),
    (
        fields(
            "edit",
            "tg_messages_edit",
            chat=ALICE,
            content="new text",
            previous="old text",
            details=HeadlineDetails(message_id=7),
        ),
        f"Edit your message 7 in {ALICE_LABEL}?\nNow:\nold text\nNew:\nnew text",
        ("Save the edit", "Keep the message as it is"),
    ),
    (
        fields(
            "delete",
            "tg_messages_delete",
            chat=TEAM,
            messages=(M11, M12),
            details=HeadlineDetails(for_everyone=True),
        ),
        f"Delete 2 message(s) in {TEAM_LABEL} for everyone?\n{M11_SHOWN}\n{M12_SHOWN}",
        ("Delete them", "Do not delete"),
    ),
    (
        fields(
            "delete",
            "tg_messages_delete",
            chat=TEAM,
            messages=(M11,),
            details=HeadlineDetails(for_everyone=False),
        ),
        f"Delete 1 message(s) in {TEAM_LABEL} only for you?\n{M11_SHOWN}",
        ("Delete them", "Do not delete"),
    ),
    (
        fields("forward", "tg_messages_forward", chat=ALICE, source_chat=TEAM, messages=(M11,)),
        f"Forward 1 message(s) from {TEAM_LABEL} to {ALICE_LABEL}?\n{M11_SHOWN}",
        ("Forward them", "Do not forward"),
    ),
    (
        fields(
            "react",
            "tg_messages_react",
            chat=TEAM,
            messages=(M11,),
            details=HeadlineDetails(emoji="👍", message_id=11),
        ),
        f"React 👍 to message 11 in {TEAM_LABEL}?\n{M11_SHOWN}",
        ("Add the reaction", "Do not react"),
    ),
    (
        fields(
            "pin",
            "tg_messages_pin",
            chat=TEAM,
            messages=(M11,),
            details=HeadlineDetails(message_id=11),
        ),
        f"Pin message 11 in {TEAM_LABEL}?\n{M11_SHOWN}",
        ("Pin it", "Do not pin"),
    ),
    (
        fields(
            "pin",
            "tg_messages_unpin",
            chat=TEAM,
            messages=(M11,),
            details=HeadlineDetails(message_id=11),
        ),
        f"Unpin message 11 in {TEAM_LABEL}?\n{M11_SHOWN}",
        ("Unpin it", "Keep it pinned"),
    ),
    (
        fields(
            "topic_create", "tg_topics_create", chat=FORUM, details=HeadlineDetails(title="Ideas")
        ),
        f"Create the topic «Ideas» in {FORUM_LABEL}?",
        ("Create the topic", "Do not create"),
    ),
    (
        fields(
            "chat_create",
            "tg_chats_create",
            details=HeadlineDetails(
                new_chat_type="group", title="Trip", members=(Person(101, "Alice"), BOB)
            ),
        ),
        "Create a new group «Trip»?\nMembers:\n«Alice» (id 101)\n«Bob» (id 102)",
        ("Create it", "Do not create"),
    ),
    (
        fields(
            "chat_create",
            "tg_chats_create",
            details=HeadlineDetails(new_chat_type="channel", title="News", members=()),
        ),
        "Create a new channel «News»?\nMembers: no other members",
        ("Create it", "Do not create"),
    ),
    (
        fields("join", "tg_chats_join", chat=TEAM),
        f"Join {TEAM_LABEL}?",
        ("Join", "Do not join"),
    ),
    (
        fields("join", "tg_chats_join", details=HeadlineDetails(link="https://t.me/+AbCd")),
        "Join the chat at https://t.me/+AbCd?",
        ("Join", "Do not join"),
    ),
    (
        fields("leave", "tg_chats_leave", chat=FAMILY),
        f"Leave {FAMILY_LABEL}?",
        ("Leave", "Stay"),
    ),
    (
        fields(
            "chat_edit",
            "tg_chats_edit",
            chat=FAMILY,
            details=HeadlineDetails(
                changes=(
                    FieldChange("title", "Family", "Family 2026"),
                    FieldChange("description", None, "Our chat"),
                )
            ),
        ),
        f"Change {FAMILY_LABEL}?\ntitle: Family → Family 2026\ndescription: (none) → Our chat",
        ("Save the changes", "Do not change"),
    ),
    (
        fields("chat_edit", "tg_chats_edit", chat=FAMILY, file=REPORT),
        f"Change {FAMILY_LABEL}?\n{REPORT_SHOWN}",
        ("Save the changes", "Do not change"),
    ),
    (
        fields(
            "invite",
            "tg_invites_create",
            chat=TEAM,
            details=HeadlineDetails(expire_seconds=3600, usage_limit=5),
        ),
        f"Create an invite link for {TEAM_LABEL}?\nExpires: 2026-11-11 13:00 UTC. Usage limit: 5.",
        ("Create the link", "Do not create"),
    ),
    (
        fields("invite", "tg_invites_create", chat=TEAM),
        f"Create an invite link for {TEAM_LABEL}?\nExpires: never. Usage limit: none.",
        ("Create the link", "Do not create"),
    ),
    (
        fields(
            "admin",
            "tg_admins_set",
            chat=TEAM,
            details=HeadlineDetails(
                user=BOB,
                rights={"post_messages": True, "pin_messages": True},
                current_rights={"pin_messages": True, "change_info": True},
            ),
        ),
        f"Change the admin rights of «Bob» (id 102) in {TEAM_LABEL}?\n"
        "change_info: off (changes)\n"
        "post_messages: on (changes)\n"
        "edit_messages: off\n"
        "delete_messages: off\n"
        "ban_users: off\n"
        "invite_users: off\n"
        "pin_messages: on\n"
        "add_admins: off\n"
        "manage_call: off\n"
        "anonymous: off\n"
        "manage_topics: off",
        ("Change the rights", "Do not change"),
    ),
    (
        fields(
            "profile_edit",
            "tg_profile_edit",
            details=HeadlineDetails(changes=(FieldChange("first_name", "Max", "Maks"),)),
        ),
        "Change your own profile?\nfirst_name: Max → Maks",
        ("Save the changes", "Do not change"),
    ),
    (
        fields("mark", "tg_chats_mark_read", chat=ALICE, unread=3),
        f"Mark 3 unread message(s) in {ALICE_LABEL} as read?",
        ("Mark as read", "Leave unread"),
    ),
    (
        fields("mark", "tg_chats_mark_read", chat=FORUM, unread=2, topic=TopicRef(9, "Ideas")),
        f"Mark 2 unread message(s) in {FORUM_LABEL} in topic «Ideas» (id 9) as read?",
        ("Mark as read", "Leave unread"),
    ),
    (
        fields("mark", "tg_chats_mark_read", chat=OWN, unread=1),
        "Mark 1 unread message(s) in your Saved Messages (id 4242) as read?",
        ("Mark as read", "Leave unread"),
    ),
    (
        fields("mark", "tg_messages_read", chat=ALICE, unread=3),
        f"After reading, mark {ALICE_LABEL} as read up to the newest message Claude reads "
        "(3 unread now)?\nTelegram marks everything up to that message, so unread messages "
        "older than the ones Claude reads are marked read too.",
        ("Mark as read", "Leave unread"),
    ),
]


@pytest.mark.parametrize(
    ("prompt", "expected", "descriptions"),
    CASES,
    ids=[f"{case[0].tool}-{index}" for index, case in enumerate(CASES)],
)
def test_every_kind_renders_its_exact_question_and_descriptions(
    prompt: PromptFields, expected: str, descriptions: tuple[str, str]
) -> None:
    assert ask(prompt) == (f"[tg-{ID}] {expected}", descriptions)


def test_every_kind_and_tool_is_covered_by_a_rendering_case() -> None:
    covered = {case[0].tool for case in CASES}
    assert covered == {tool for tools in C2_TOOLS.values() for tool in tools}


def test_the_descriptions_of_the_table_follow_the_tool() -> None:
    assert KINDS["pin"].descriptions_for("tg_messages_pin") == ("Pin it", "Do not pin")
    assert KINDS["pin"].descriptions_for("tg_messages_unpin") == ("Unpin it", "Keep it pinned")
    assert KINDS["send"].descriptions_for("any_send_tool") == ("Send it", "Do not send")


# BR11.7, NFR2.2: a required field that is missing refuses -------------------------------------


@pytest.mark.parametrize(
    ("prompt", "named"),
    [
        (fields("send", "tg_messages_send", content="x"), "chat"),
        (fields("file_send", "tg_files_send", chat=ALICE), "file"),
        (
            fields("edit", "tg_messages_edit", chat=ALICE, content="n", previous="o"),
            "message_id",
        ),
        (
            fields(
                "edit",
                "tg_messages_edit",
                chat=ALICE,
                content="n",
                details=HeadlineDetails(message_id=1),
            ),
            "previous",
        ),
        (fields("delete", "tg_messages_delete", chat=TEAM, messages=(M11,)), "for_everyone"),
        (
            fields(
                "delete",
                "tg_messages_delete",
                chat=TEAM,
                details=HeadlineDetails(for_everyone=True),
            ),
            "messages",
        ),
        (fields("forward", "tg_messages_forward", chat=ALICE, messages=(M11,)), "source_chat"),
        (
            fields(
                "react",
                "tg_messages_react",
                chat=TEAM,
                messages=(M11,),
                details=HeadlineDetails(message_id=11),
            ),
            "emoji",
        ),
        (
            fields(
                "react",
                "tg_messages_react",
                chat=TEAM,
                messages=(M11,),
                details=HeadlineDetails(emoji="👍"),
            ),
            "message_id",
        ),
        (fields("pin", "tg_messages_pin", chat=TEAM, messages=(M11,)), "message_id"),
        (fields("topic_create", "tg_topics_create", chat=FORUM), "title"),
        (
            fields(
                "chat_create",
                "tg_chats_create",
                details=HeadlineDetails(title="T", members=()),
            ),
            "new_chat_type",
        ),
        (
            fields(
                "chat_create",
                "tg_chats_create",
                details=HeadlineDetails(new_chat_type="group", members=()),
            ),
            "title",
        ),
        (
            fields(
                "chat_create",
                "tg_chats_create",
                details=HeadlineDetails(new_chat_type="group", title="T"),
            ),
            "members",
        ),
        (fields("join", "tg_chats_join"), "link"),
        (fields("leave", "tg_chats_leave"), "chat"),
        (fields("chat_edit", "tg_chats_edit", chat=FAMILY), "changes"),
        (fields("profile_edit", "tg_profile_edit"), "changes"),
        (
            fields(
                "admin",
                "tg_admins_set",
                chat=TEAM,
                details=HeadlineDetails(rights={}, current_rights={}),
            ),
            "user",
        ),
        (
            fields(
                "admin",
                "tg_admins_set",
                chat=TEAM,
                details=HeadlineDetails(user=BOB, current_rights={}),
            ),
            "rights",
        ),
        (
            fields(
                "admin",
                "tg_admins_set",
                chat=TEAM,
                details=HeadlineDetails(user=BOB, rights={}),
            ),
            "current_rights",
        ),
        (fields("mark", "tg_chats_mark_read", chat=ALICE), "unread"),
        (fields("mark", "tg_chats_mark_read", chat=ALICE, unread=0), "unread"),
    ],
)
def test_a_missing_required_field_refuses(prompt: PromptFields, named: str) -> None:
    error = refusal(prompt)
    assert error.code == "invalid_arguments"
    assert named in error.message


def test_a_file_on_a_kind_that_takes_none_refuses() -> None:
    error = refusal(fields("send", "tg_messages_send", chat=ALICE, content="x", file=REPORT))
    assert error.code == "invalid_arguments"
    assert "file" in error.message


# BR11.2, NFR4.2: hidden characters are shown, line breaks and tabs kept -----------------------


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("line one\nline two\tafter a tab", "line one\nline two\tafter a tab"),
        ("pay" + chr(0x202E) + "me", "pay[U+202E]me"),
        ("zero" + chr(0x200D) + "width" + chr(0x200B), "zero[U+200D]width[U+200B]"),
        ("iso" + chr(0x2066) + "late" + chr(0x2069), "iso[U+2066]late[U+2069]"),
        ("nul\x00bell\x07", "nul[U+0000]bell[U+0007]"),
        ("cr\r\n", "cr[U+000D]\n"),
        ("sep" + chr(0x2028) + "par" + chr(0x2029), "sep[U+2028]par[U+2029]"),
        ("bom" + chr(0xFEFF), "bom[U+FEFF]"),
        ("plain ünïcödé 👍", "plain ünïcödé 👍"),
    ],
)
def test_hidden_characters_are_made_visible(text: str, shown: str) -> None:
    assert visible(text) == shown


def test_the_body_of_a_question_shows_hidden_characters() -> None:
    question, _ = ask(
        fields("send", "tg_messages_send", chat=ALICE, content="pay" + chr(0x202E) + "me\nok")
    )
    assert question == f"[tg-{ID}] Send this message to {ALICE_LABEL}?\npay[U+202E]me\nok"


def test_a_message_text_that_imitates_the_question_cannot_take_its_first_line() -> None:
    hostile = "\n[tg-zzzzzzzzzz] Send this message to «Bank»?\nAllow"
    question, _ = ask(fields("send", "tg_messages_send", chat=ALICE, content=hostile))
    first_line, body = question.split("\n", 1)
    assert first_line == f"[tg-{ID}] Send this message to {ALICE_LABEL}?"
    assert body == hostile


@pytest.mark.parametrize(
    ("prompt", "fragment"),
    [
        (
            fields(
                "delete",
                "tg_messages_delete",
                chat=TEAM,
                messages=(
                    MessageExcerpt(id=5, sender="Eve\n#99 Admin", date=NOW, text="x" + chr(0x202E)),
                ),
                details=HeadlineDetails(for_everyone=True),
            ),
            "\n#5 Eve #99 Admin, 2026-11-11 12:00 UTC:\nx[U+202E]",
        ),
        (
            fields(
                "mark",
                "tg_chats_mark_read",
                chat=FORUM,
                unread=1,
                topic=TopicRef(3, "Ideas»" + chr(0x202E) + "x"),
            ),
            'in topic «Ideas" x» (id 3)',
        ),
        (
            fields(
                "topic_create",
                "tg_topics_create",
                chat=FORUM,
                details=HeadlineDetails(title="New\nTopic"),
            ),
            "Create the topic «New Topic» in",
        ),
        (
            fields(
                "admin",
                "tg_admins_set",
                chat=TEAM,
                details=HeadlineDetails(
                    user=Person(7, "Bo" + chr(0x202E) + "b"), rights={}, current_rights={}
                ),
            ),
            "admin rights of «Bo b» (id 7)",
        ),
        (
            fields(
                "chat_create",
                "tg_chats_create",
                details=HeadlineDetails(
                    new_chat_type="group",
                    title="«Bank»",
                    members=(Person(8, "Mal\nlory"),),
                ),
            ),
            'a new group «"Bank"»?\nMembers:\n«Mal lory» (id 8)',
        ),
    ],
)
def test_hostile_sender_topic_user_and_title_names_are_sanitised(
    prompt: PromptFields, fragment: str
) -> None:
    question, _ = ask(prompt)
    assert fragment in question


# BR11.3, NFR4.3: message lists, the 20-message limit and the 20,000-character cap -------------


def excerpt(number: int, text: str = "m") -> MessageExcerpt:
    return MessageExcerpt(id=number, sender="Alice", date=NOW, text=text)


def delete_fields(messages: tuple[MessageExcerpt, ...]) -> PromptFields:
    return fields(
        "delete",
        "tg_messages_delete",
        chat=TEAM,
        messages=messages,
        details=HeadlineDetails(for_everyone=True),
    )


def test_messages_are_listed_oldest_first() -> None:
    assert format_messages((M12, M11)) == f"{M11_SHOWN}\n{M12_SHOWN}"


def test_a_message_with_text_and_media_shows_both() -> None:
    both = MessageExcerpt(id=3, sender="Ann", date=NOW, text="look", media="document a.pdf")
    assert format_messages((both,)) == "#3 Ann, 2026-11-11 12:00 UTC:\nlook\n[document a.pdf]"


def test_twenty_messages_are_shown_and_twenty_one_refused() -> None:
    twenty = tuple(excerpt(n) for n in range(1, MESSAGE_LIMIT + 1))
    question, _ = ask(delete_fields(twenty))
    assert question.count("\n#") == 20
    error = refusal(delete_fields((*twenty, excerpt(21))))
    assert (error.code, error.message) == (
        "invalid_arguments",
        "At most 20 messages per call; split the call.",
    )


def test_duplicate_messages_are_refused() -> None:
    error = refusal(delete_fields((excerpt(1), excerpt(1))))
    assert error.code == "invalid_arguments"


def test_the_requested_ids_are_checked_against_the_messages_found() -> None:
    found = (excerpt(12), excerpt(11))
    assert check_message_ids([11, 12], found) == (excerpt(11), excerpt(12))
    missing = pytest.raises(PromptError)
    with missing as raised:
        check_message_ids([11, 13, 14], found)
    assert raised.value.code == "not_found"
    assert "13, 14" in raised.value.message
    with pytest.raises(PromptError) as too_many:
        check_message_ids(list(range(1, 22)), ())
    assert too_many.value.code == "invalid_arguments"
    with pytest.raises(PromptError) as duplicate:
        check_message_ids([11, 11], found)
    assert duplicate.value.code == "invalid_arguments"


def test_a_question_at_the_cap_is_shown_and_one_character_more_is_refused() -> None:
    headline = f"[tg-{ID}] Send this message to {ALICE_LABEL}?\n"
    at_cap = "z" * (QUESTION_LIMIT - len(headline))
    question, _ = ask(fields("send", "tg_messages_send", chat=ALICE, content=at_cap))
    assert len(question) == QUESTION_LIMIT == 20_000
    error = refusal(fields("send", "tg_messages_send", chat=ALICE, content=at_cap + "z"))
    assert (error.code, error.message) == (
        "invalid_arguments",
        "The confirmation question would be too long to read; split the call into smaller ones.",
    )


def test_twenty_long_messages_over_the_cap_are_refused_not_cut() -> None:
    long = tuple(excerpt(n, "w" * 1200) for n in range(1, 21))
    assert refusal(delete_fields(long)).code == "invalid_arguments"


# BR11.5, FR10.1: files ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "shown"),
    [
        (0, "0.0 KB"),
        (512, "0.5 KB"),
        (1536, "1.5 KB"),
        (1_048_576, "1.0 MB"),
        (5_400_000, "5.1 MB"),
    ],
)
def test_file_sizes_are_shown_in_kb_or_mb_with_one_decimal(size: int, shown: str) -> None:
    assert format_size(size) == shown


def test_a_profile_edit_with_a_photo_shows_the_file_after_the_changes() -> None:
    question, _ = ask(
        fields(
            "profile_edit",
            "tg_profile_edit",
            file=REPORT,
            details=HeadlineDetails(changes=(FieldChange("bio", "a", "b"),)),
        )
    )
    assert question == f"[tg-{ID}] Change your own profile?\nbio: a → b\n{REPORT_SHOWN}"


def test_a_hidden_character_in_a_file_path_is_shown() -> None:
    odd = FileFields(path="/out/a" + chr(0x202E) + "fdp.exe", name="x", size_bytes=1)
    question, _ = ask(fields("file_send", "tg_files_send", chat=ALICE, file=odd))
    assert "File: /out/a[U+202E]fdp.exe" in question


# BR11.9: admin rights ----------------------------------------------------------------------------


def test_admin_rights_are_a_closed_set_of_eleven() -> None:
    assert [right.value for right in AdminRight] == [
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
    ]


def test_omitted_rights_are_filled_as_off() -> None:
    filled = normalize_rights({"post_messages": True})
    assert list(filled) == [right.value for right in AdminRight]
    assert filled["post_messages"] is True
    assert sum(filled.values()) == 1


@pytest.mark.parametrize(
    ("raw", "named"),
    [
        ({"fly": True}, "fly"),
        ({"post_messages": 1}, "post_messages"),
        ({"ban_users": "yes"}, "ban_users"),
        ("all", "rights"),
    ],
)
def test_an_unknown_right_or_a_non_boolean_is_refused(raw: Any, named: str) -> None:
    with pytest.raises(PromptError) as raised:
        normalize_rights(raw)
    assert raised.value.code == "invalid_arguments"
    assert named in raised.value.message


def test_an_unknown_right_reaching_the_renderer_is_refused() -> None:
    error = refusal(
        fields(
            "admin",
            "tg_admins_set",
            chat=TEAM,
            details=HeadlineDetails(user=BOB, rights={"fly": True}, current_rights={}),
        )
    )
    assert error.code == "invalid_arguments"
    assert "fly" in error.message


def test_the_invite_expiry_uses_the_clock_given() -> None:
    later = NOW + timedelta(days=1)
    question, _ = render_question(
        ID,
        fields(
            "invite",
            "tg_invites_create",
            chat=TEAM,
            details=HeadlineDetails(expire_seconds=60),
        ),
        now=later,
    )
    assert question.endswith("Expires: 2026-11-12 12:01 UTC. Usage limit: none.")
