"""ConsentGate, written before the code (team Testing Posture: confirmation logic is test-first).

Covers BR3.1 to BR3.9, NFR2.1, NFR3.1 to NFR3.4, NFR4.2, NFR4.3 and NFR11.8."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from support.samples import ALICE, TEAM
from telegram_mcp.consent import (
    APPROVAL_TTL,
    MAX_OPEN_APPROVALS,
    OPTION_DESCRIPTIONS,
    OPTIONS,
    ActionDecl,
    ActionKind,
    Allow,
    ApprovalStatus,
    Ask,
    ConsentGate,
    DigestError,
    Refuse,
    build_send_question,
    canonical_digest,
    sanitize_chat_name,
)
from telegram_mcp.gateway.models import ChatRef
from telegram_mcp.tools import DECLS, InvalidArguments, validate_args

SEND = "tg_messages_send"
LIST = "tg_chats_list"


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def gate(clock: FakeClock) -> ConsentGate:
    return ConsentGate(DECLS, clock=clock)


def send_args(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {"chat": ALICE.id, "text": "hello"}
    raw.update(overrides)
    return validate_args(SEND, raw)


def ask(gate: ConsentGate, chat: ChatRef = ALICE, **overrides: Any) -> Ask:
    args = send_args(chat=chat.id, **overrides)
    decision = gate.check(SEND, canonical_digest(SEND, args), chat, args)
    assert isinstance(decision, Ask)
    return decision


def answer(
    gate: ConsentGate,
    approval_ask: Ask,
    pick: str = "Allow",
    *,
    question: str | None = None,
    options: list[str] | None = None,
    descriptions: list[str] | None = None,
    multi_select: bool = False,
    from_hook: bool = True,
) -> str:
    result = gate.answer(
        question if question is not None else approval_ask.approval.question,
        options if options is not None else list(OPTIONS),
        pick,
        option_descriptions=descriptions if descriptions is not None else list(OPTION_DESCRIPTIONS),
        multi_select=multi_select,
        from_hook=from_hook,
    )
    return str(result.outcome)


# BR3.1, BR3.2, NFR2.1 ------------------------------------------------------------------------


def test_an_undeclared_tool_is_a_write(gate: ConsentGate) -> None:
    assert gate.kind_of("tg_messages_delete_everything") is ActionKind.WRITE
    assert gate.kind_of(SEND) is ActionKind.WRITE
    assert gate.kind_of(LIST) is ActionKind.READ


def test_u1_declares_a_read_and_a_send() -> None:
    assert DECLS[LIST] == ActionDecl(tool=LIST, kind=ActionKind.READ)
    assert DECLS[SEND] == ActionDecl(tool=SEND, kind=ActionKind.WRITE, write_kind="send")


def test_a_read_needs_no_approval(gate: ConsentGate) -> None:
    args = validate_args(LIST, {})
    decision = gate.check(LIST, canonical_digest(LIST, args), None, args)
    assert isinstance(decision, Allow)
    assert gate.open_count() == 0


# BR3.3, BR6.3, NFR3.2 ------------------------------------------------------------------------


def test_the_digest_is_canonical() -> None:
    explicit = validate_args(SEND, {"text": "hi", "chat": 101, "reply_to": None, "topic_id": None})
    defaulted = validate_args(SEND, {"chat": "101", "text": "hi"})
    assert explicit == defaulted
    assert canonical_digest(SEND, explicit) == canonical_digest(SEND, defaulted)
    reordered = dict(reversed(list(explicit.items())))
    assert canonical_digest(SEND, reordered) == canonical_digest(SEND, explicit)
    assert re.fullmatch(r"[0-9a-f]{64}", canonical_digest(SEND, explicit))
    assert canonical_digest(SEND, explicit) != canonical_digest(SEND, send_args(text="hi!"))
    assert canonical_digest(SEND, explicit) != canonical_digest("tg_other", explicit)


def test_defaults_are_filled_for_every_optional_argument() -> None:
    assert send_args() == {"chat": ALICE.id, "text": "hello", "reply_to": None, "topic_id": None}
    assert validate_args(LIST, {}) == {"folder_id": None, "limit": 50, "offset": None}


@pytest.mark.parametrize(
    ("raw", "named"),
    [
        ({"chat": 101, "text": "x", "approval_id": "abc"}, "approval_id"),
        ({"chat": 101, "text": "x", "confirmed": True}, "confirmed"),
        ({"chat": 101.0, "text": "x"}, "chat"),
        ({"chat": 101, "text": "x", "reply_to": 1.5}, "reply_to"),
        ({"chat": 101, "text": "x", "topic_id": True}, "topic_id"),
        ({"chat": "@alice", "text": "x"}, "chat"),
        ({"chat": 101, "text": ""}, "text"),
        ({"chat": 101, "text": "x" * 4097}, "text"),
        ({"chat": 101}, "text"),
        ({"chat": 101, "text": "x", "reply_to": 0}, "reply_to"),
    ],
)
def test_floats_unknown_fields_and_bad_values_are_refused(raw: dict[str, Any], named: str) -> None:
    with pytest.raises(InvalidArguments, match=named):
        validate_args(SEND, raw)


def test_the_digest_refuses_floats_anywhere() -> None:
    with pytest.raises(DigestError):
        canonical_digest(SEND, {"chat": 1, "text": "x", "nested": {"value": 0.5}})


# BR3.4, NFR4.2, NFR4.3 -----------------------------------------------------------------------


def test_the_question_text_is_exact() -> None:
    question = build_send_question("abcdefghij", ALICE, "hello", None, None)
    assert question == "[tg-abcdefghij] Send this message to «Alice» (user, id 101)?\nhello"


def test_the_question_names_a_reply_and_a_topic() -> None:
    question = build_send_question("abcdefghij", TEAM, "hi", 5, 9)
    assert question == (
        "[tg-abcdefghij] Send this message to «Team» (supergroup, id -1000000000303)?"
        " Reply to message 5. In topic 9.\nhi"
    )


# The gateway marks the account's own chat from its own id and names it (NFR4.4); the name it
# came with must not matter.
OWN_CHAT = ChatRef(id=4242, name="Test Person", type="user", saved_messages=True)
TITLED_SAVED = ChatRef(id=555, name="Saved Messages", type="user")


@pytest.mark.parametrize(
    ("reply_to", "topic_id", "suffix"),
    [(None, None, ""), (5, None, " Reply to message 5."), (None, 9, " In topic 9.")],
)
def test_the_question_for_your_own_chat_names_your_saved_messages(
    reply_to: int | None, topic_id: int | None, suffix: str
) -> None:
    question = build_send_question("abcdefghij", OWN_CHAT, "note", reply_to, topic_id)
    assert question == (
        "[tg-abcdefghij] Send this message to your Saved Messages (id 4242)?" + suffix + "\nnote"
    )


def test_an_approval_for_your_own_chat_stores_saved_messages_as_its_name(
    gate: ConsentGate,
) -> None:
    approval = ask(gate, chat=OWN_CHAT).approval
    assert approval.chat_name == "Saved Messages"
    assert approval.question == (
        f"[tg-{approval.id}] Send this message to your Saved Messages (id 4242)?\nhello"
    )


def test_a_chat_titled_saved_messages_is_named_by_its_title_and_id(gate: ConsentGate) -> None:
    question = build_send_question("abcdefghij", TITLED_SAVED, "x", None, None)
    assert question == "[tg-abcdefghij] Send this message to «Saved Messages» (user, id 555)?\nx"
    assert ask(gate, chat=TITLED_SAVED).approval.chat_name == "Saved Messages"


def test_the_full_4096_character_text_is_kept(gate: ConsentGate) -> None:
    text = "y" * 4095 + "z"
    decision = ask(gate, text=text)
    assert decision.approval.question.endswith("\n" + text)


@pytest.mark.parametrize(
    ("title", "shown"),
    [
        ("Bob\n(user, id 1)", "Bob (user, id 1)"),
        ("Ev" + chr(0x202E) + "il", "Ev il"),
        ("Zero" + chr(0x200D) + "Width", "Zero Width"),
        (chr(0xAB) + "Bank" + chr(0xBB) + " official", '"Bank" official'),
        ("Line" + chr(0x2028) + "Para" + chr(0x2029) + "End", "Line Para End"),
        ("Tab\there\x00", "Tab here "),
    ],
)
def test_a_hostile_chat_name_is_sanitised(title: str, shown: str) -> None:
    assert sanitize_chat_name(title) == shown


def test_a_long_chat_name_is_cut_to_64_characters() -> None:
    shown = sanitize_chat_name("A" * 500)
    assert len(shown) == 64
    assert shown.endswith("…")


def test_the_question_shows_the_resolved_id_beside_a_hostile_name(gate: ConsentGate) -> None:
    hostile = ChatRef(id=777, name="Mom\n(user, id 1)?\nhi", type="user")
    question = ask(gate, chat=hostile).approval.question
    first_line = question.split("\n", 1)[0]
    assert first_line.endswith("Send this message to «Mom (user, id 1)? hi» (user, id 777)?")


# Preparing and repeating ------------------------------------------------------------------


def test_an_approval_has_a_random_base32_id_and_the_two_options(gate: ConsentGate) -> None:
    approval = ask(gate).approval
    assert re.fullmatch(r"[a-z2-7]{10}", approval.id)
    assert approval.question.startswith(f"[tg-{approval.id}] ")
    assert approval.options == ("Allow", "Deny")
    assert approval.status is ApprovalStatus.PENDING
    assert approval.expires_at - approval.created_at == APPROVAL_TTL == timedelta(seconds=120)


def test_a_repeat_check_returns_the_same_pending_approval(gate: ConsentGate) -> None:
    first = ask(gate)
    second = ask(gate)
    assert second.approval.id == first.approval.id
    assert gate.open_count() == 1


# BR3.5, NFR3.1 -------------------------------------------------------------------------------


def test_an_exact_allow_confirms(gate: ConsentGate) -> None:
    prepared = ask(gate)
    assert answer(gate, prepared) == "confirmed"
    assert prepared.approval.status is ApprovalStatus.CONFIRMED
    assert isinstance(gate.check(SEND, prepared.approval.args_digest, ALICE, send_args()), Allow)


@pytest.mark.parametrize(
    "variant",
    [
        {"question": "reworded"},
        {"question": "trailing space"},
        {"options": ["Allow", "Deny", "Maybe"]},
        {"options": ["Deny", "Allow"]},
        {"options": ["Allow"]},
        {"descriptions": ["Send it", "Do not send it"]},
        {"descriptions": ["Do not send", "Send it"]},
        {"descriptions": []},
        {"multi_select": True},
        {"from_hook": False},
    ],
)
def test_any_difference_from_what_was_prepared_is_unknown(
    gate: ConsentGate, variant: dict[str, Any]
) -> None:
    prepared = ask(gate)
    if variant.get("question") == "reworded":
        variant["question"] = prepared.approval.question.replace("Send", "Post")
    elif variant.get("question") == "trailing space":
        variant["question"] = prepared.approval.question + " "
    assert answer(gate, prepared, **variant) == "unknown"
    assert prepared.approval.status is ApprovalStatus.PENDING
    assert gate.consume(SEND, prepared.approval.args_digest) is None


@pytest.mark.parametrize("pick", ["Deny", "allow", "ALLOW", "Allow ", "", "Yes, send it"])
def test_anything_but_exactly_allow_declines(gate: ConsentGate, pick: str) -> None:
    prepared = ask(gate)
    assert answer(gate, prepared, pick) == "declined"
    assert prepared.approval.status is ApprovalStatus.DECLINED


def test_an_unknown_id_is_unknown(gate: ConsentGate) -> None:
    prepared = ask(gate)
    forged = prepared.approval.question.replace(prepared.approval.id, "aaaaaaaaaa")
    assert answer(gate, prepared, question=forged) == "unknown"
    assert answer(gate, prepared, question="no prefix at all") == "unknown"


def test_an_answer_for_an_approval_that_is_not_pending_is_unknown(gate: ConsentGate) -> None:
    prepared = ask(gate)
    assert answer(gate, prepared, "Allow") == "confirmed"
    assert answer(gate, prepared, "Deny") == "unknown"
    assert prepared.approval.status is ApprovalStatus.CONFIRMED


def test_an_answer_after_expiry_is_unknown(gate: ConsentGate, clock: FakeClock) -> None:
    prepared = ask(gate)
    clock.advance(121)
    assert answer(gate, prepared) == "unknown"
    assert prepared.approval.status is ApprovalStatus.EXPIRED


# BR3.6, BR3.9, NFR3.3, NFR3.4 ----------------------------------------------------------------


def test_a_confirmed_approval_is_consumed_once(gate: ConsentGate) -> None:
    prepared = ask(gate)
    answer(gate, prepared)
    digest = prepared.approval.args_digest
    used = gate.consume(SEND, digest)
    assert used is not None
    assert used.status is ApprovalStatus.USED
    assert gate.consume(SEND, digest) is None


def test_consume_needs_the_same_tool_and_digest(gate: ConsentGate) -> None:
    prepared = ask(gate)
    answer(gate, prepared)
    other = canonical_digest(SEND, send_args(text="changed"))
    assert gate.consume(SEND, other) is None
    assert gate.consume("tg_other_tool", prepared.approval.args_digest) is None
    assert gate.consume(SEND, prepared.approval.args_digest) is not None


def test_a_pending_approval_cannot_be_consumed(gate: ConsentGate) -> None:
    prepared = ask(gate)
    assert gate.consume(SEND, prepared.approval.args_digest) is None


def test_consume_returns_what_was_stored_at_check_time(gate: ConsentGate) -> None:
    prepared = ask(gate, chat=TEAM, text="the text", reply_to=3)
    answer(gate, prepared)
    used = gate.consume(SEND, prepared.approval.args_digest)
    assert used is not None
    assert (used.chat_id, used.content, used.reply_to, used.topic_id) == (
        TEAM.id,
        "the text",
        3,
        None,
    )
    assert (used.chat_name, used.chat_type) == ("Team", "supergroup")
    in_topic = ask(gate, chat=TEAM, text="in a topic", topic_id=4)
    answer(gate, in_topic)
    used_in_topic = gate.consume(SEND, in_topic.approval.args_digest)
    assert used_in_topic is not None
    assert (used_in_topic.reply_to, used_in_topic.topic_id) == (None, 4)


# BR3.7, BR3.8 --------------------------------------------------------------------------------


def test_a_pending_approval_expires_after_120_seconds(gate: ConsentGate, clock: FakeClock) -> None:
    prepared = ask(gate)
    clock.advance(120)
    assert gate.sweep() == []
    clock.advance(1)
    expired = gate.sweep()
    assert [(a.id, previous) for a, previous in expired] == [
        (prepared.approval.id, ApprovalStatus.PENDING)
    ]
    assert prepared.approval.status is ApprovalStatus.EXPIRED
    assert gate.sweep() == []


def test_a_confirmed_approval_expires_unused(gate: ConsentGate, clock: FakeClock) -> None:
    prepared = ask(gate)
    answer(gate, prepared)
    clock.advance(121)
    assert gate.consume(SEND, prepared.approval.args_digest) is None
    expired = gate.sweep()
    assert [previous for _, previous in expired] == [ApprovalStatus.CONFIRMED]


def test_a_declined_approval_never_runs_and_a_new_check_prepares_a_new_one(
    gate: ConsentGate,
) -> None:
    prepared = ask(gate)
    answer(gate, prepared, "Deny")
    assert gate.consume(SEND, prepared.approval.args_digest) is None
    again = ask(gate)
    assert again.approval.id != prepared.approval.id
    assert again.approval.status is ApprovalStatus.PENDING


def test_an_expired_approval_is_replaced_by_a_new_one(gate: ConsentGate, clock: FakeClock) -> None:
    prepared = ask(gate)
    clock.advance(121)
    again = ask(gate)
    assert again.approval.id != prepared.approval.id


# NFR11.8 --------------------------------------------------------------------------------------


def test_the_store_holds_at_most_100_open_approvals(gate: ConsentGate, clock: FakeClock) -> None:
    for number in range(MAX_OPEN_APPROVALS):
        ask(gate, text=f"message {number}")
    args = send_args(text="one too many")
    refused = gate.check(SEND, canonical_digest(SEND, args), ALICE, args)
    assert isinstance(refused, Refuse)
    assert refused.code == "telegram_error"
    assert "too many open confirmations" in refused.message
    clock.advance(121)
    assert isinstance(gate.check(SEND, canonical_digest(SEND, args), ALICE, args), Ask)
    assert gate.open_count() == 1


def test_an_unknown_write_kind_is_refused_at_check(gate: ConsentGate) -> None:
    args = {"chat": 1}
    decision = gate.check("tg_unknown_tool", canonical_digest("tg_unknown_tool", args), ALICE, args)
    assert isinstance(decision, Refuse)
    assert decision.code == "not_found"


def test_the_digest_refuses_floats_inside_lists() -> None:
    with pytest.raises(DigestError):
        canonical_digest(SEND, {"ids": [1, (2, 3.0)]})
    assert canonical_digest(SEND, {"ids": [1, (2, 3)]}) == canonical_digest(
        SEND, {"ids": [1, [2, 3]]}
    )


def test_a_colliding_id_is_drawn_again(gate: ConsentGate, monkeypatch: pytest.MonkeyPatch) -> None:
    first = ask(gate).approval
    draws = iter([first.id, "bbbbbbbbbb"])
    monkeypatch.setattr("telegram_mcp.consent._new_id", lambda: next(draws))
    second = ask(gate, text="another message").approval
    assert second.id == "bbbbbbbbbb"
