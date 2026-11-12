"""The ConsentGate's U2 rules, written before the code (team Testing Posture): kinds, the
120 s and 60 s windows, repeat calls, ask_again and marks.

Covers BR10.1 to BR10.3, BR12.1 to BR12.6, BR13.2, BR13.6, BR13.8; NFR2.1, NFR3.1 to NFR3.6.
Time is a controllable clock; no test sleeps."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from support.samples import ALICE, TEAM
from telegram_mcp.consent import (
    ASK_AGAIN_LIMIT,
    ASKED_TOO_OFTEN_MESSAGE,
    DECLINED_MESSAGE,
    MAX_RECENT_OUTCOMES,
    TIMED_OUT_MESSAGE,
    ActionDecl,
    ActionKind,
    Allow,
    ApprovalStatus,
    Ask,
    ConsentGate,
    Refuse,
    canonical_digest,
)
from telegram_mcp.prompts import (
    KINDS,
    HeadlineDetails,
    MessageExcerpt,
    PromptFields,
)

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
MARK_TOOL = "tg_chats_mark_read"
READ_TOOL = "tg_messages_read"
SEND = "tg_messages_send"
DELETE = "tg_messages_delete"


class FakeClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    def at(self, seconds: float) -> None:
        self.now = NOW + timedelta(seconds=seconds)


def stand_in_decls() -> dict[str, ActionDecl]:
    decls = {
        tool: ActionDecl(tool=tool, kind=ActionKind.WRITE, write_kind=kind)
        for kind, spec in KINDS.items()
        if kind != "mark"
        for tool in spec.tools
    }
    decls[MARK_TOOL] = ActionDecl(tool=MARK_TOOL, kind=ActionKind.MARK)
    decls[READ_TOOL] = ActionDecl(tool=READ_TOOL, kind=ActionKind.READ, marks_read_option=True)
    decls["tg_no_kind"] = ActionDecl(tool="tg_no_kind", kind=ActionKind.WRITE)
    decls["tg_odd_kind"] = ActionDecl(tool="tg_odd_kind", kind=ActionKind.WRITE, write_kind="x")
    return decls


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def gate(clock: FakeClock) -> ConsentGate:
    return ConsentGate(stand_in_decls(), clock=clock)


EXCERPT = MessageExcerpt(id=11, sender="Alice", date=NOW, text="first")

FIELDS: dict[str, PromptFields] = {
    "send": PromptFields(action="send", tool=SEND, chat=ALICE, content="hello"),
    "file_send": PromptFields(action="file_send", tool="tg_files_send", chat=ALICE),
    "edit": PromptFields(
        action="edit",
        tool="tg_messages_edit",
        chat=ALICE,
        content="new",
        previous="old",
        details=HeadlineDetails(message_id=3),
    ),
    "delete": PromptFields(
        action="delete",
        tool=DELETE,
        chat=TEAM,
        messages=(EXCERPT,),
        details=HeadlineDetails(for_everyone=True),
    ),
    "leave": PromptFields(action="leave", tool="tg_chats_leave", chat=TEAM),
    "invite": PromptFields(action="invite", tool="tg_invites_create", chat=TEAM),
}


def digest_of(tool: str, **args: Any) -> str:
    return canonical_digest(tool, {"chat": ALICE.id, **args})


def ask(
    gate: ConsentGate,
    kind: str = "send",
    text: str = "hello",
    *,
    ask_again: bool = False,
) -> Ask:
    fields = FIELDS[kind]
    decision = gate.check(
        fields.tool, digest_of(fields.tool, text=text), fields, ask_again=ask_again
    )
    assert isinstance(decision, Ask), decision
    return decision


def check(gate: ConsentGate, kind: str = "send", text: str = "hello", **options: Any) -> Any:
    fields = FIELDS[kind]
    return gate.check(fields.tool, digest_of(fields.tool, text=text), fields, **options)


def pick(gate: ConsentGate, asked: Ask, choice: str = "Allow", **overrides: Any) -> str:
    approval = asked.approval
    descriptions = overrides.pop("descriptions", list(approval.option_descriptions))
    result = gate.answer(
        overrides.pop("question", approval.question),
        ["Allow", "Deny"],
        choice,
        option_descriptions=descriptions,
        multi_select=False,
        from_hook=True,
    )
    return str(result.outcome)


# BR10.1: the kind row ------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["tg_no_kind", "tg_odd_kind"])
def test_a_write_without_a_kind_row_is_not_allowed(gate: ConsentGate, tool: str) -> None:
    refused = gate.check(tool, canonical_digest(tool, {}), FIELDS["send"])
    assert refused == Refuse(
        "not_allowed", "This action has no confirmation rule, so it cannot run."
    )
    assert gate.open_count() == 0


def test_a_tool_the_catalog_does_not_know_is_not_found(gate: ConsentGate) -> None:
    refused = gate.check("tg_mystery", "d", FIELDS["send"])
    assert isinstance(refused, Refuse)
    assert refused.code == "not_found"


def test_fields_of_another_kind_than_the_tool_declares_are_refused(gate: ConsentGate) -> None:
    refused = gate.check(DELETE, "d", FIELDS["send"])
    assert isinstance(refused, Refuse)
    assert refused.code == "not_allowed"


def test_a_question_that_cannot_be_built_prepares_nothing(gate: ConsentGate) -> None:
    broken = PromptFields(action="edit", tool="tg_messages_edit", chat=ALICE, content="x")
    refused = gate.check("tg_messages_edit", "d", broken)
    assert isinstance(refused, Refuse)
    assert refused.code == "invalid_arguments"
    assert gate.open_count() == 0


# BR10.2: the effective kind ----------------------------------------------------------------------


def test_the_effective_kind_of_the_read_tool_follows_mark_read(gate: ConsentGate) -> None:
    assert gate.effective_kind(READ_TOOL, {"chat": 1, "mark_read": True}) is ActionKind.MARK
    assert gate.effective_kind(READ_TOOL, {"chat": 1, "mark_read": False}) is ActionKind.READ
    assert gate.effective_kind(READ_TOOL, {"chat": 1}) is ActionKind.READ
    assert gate.effective_kind(MARK_TOOL, {"chat": 1}) is ActionKind.MARK
    assert gate.effective_kind(SEND, {"chat": 1}) is ActionKind.WRITE
    assert gate.effective_kind("tg_unknown", {}) is ActionKind.WRITE


# BR11.6: the stored pair ---------------------------------------------------------------------


def test_each_approval_stores_its_kinds_descriptions(gate: ConsentGate) -> None:
    deleting = ask(gate, "delete").approval
    assert deleting.option_descriptions == ("Delete them", "Do not delete")
    assert deleting.write_kind == "delete"
    assert deleting.prompt == FIELDS["delete"]


def test_an_answer_is_matched_against_the_stored_pair_not_the_send_pair(
    gate: ConsentGate,
) -> None:
    asked = ask(gate, "delete")
    assert pick(gate, asked, descriptions=["Send it", "Do not send"]) == "unknown"
    assert asked.approval.status is ApprovalStatus.PENDING
    assert pick(gate, asked) == "confirmed"


# BR10.3, NFR3.1: single use for every kind -------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(FIELDS))
def test_one_allow_is_used_once_for_every_kind(gate: ConsentGate, kind: str) -> None:
    asked = ask(gate, kind)
    assert pick(gate, asked) == "confirmed"
    digest = asked.approval.args_digest
    assert gate.consume(FIELDS[kind].tool, digest) is not None
    assert gate.consume(FIELDS[kind].tool, digest) is None
    again = check(gate, kind)
    assert isinstance(again, Ask)
    assert again.approval.id != asked.approval.id


def test_options_are_always_allow_and_deny(gate: ConsentGate) -> None:
    for kind in FIELDS:
        assert ask(gate, kind).approval.options == ("Allow", "Deny")


# BR12.2, NFR3.2: an Allow is usable 60 s from the pick --------------------------------------------


def test_an_allow_picked_at_110_s_is_usable_at_169_s(gate: ConsentGate, clock: FakeClock) -> None:
    asked = ask(gate)
    clock.at(110)
    assert pick(gate, asked) == "confirmed"
    assert asked.approval.use_by == NOW + timedelta(seconds=170)
    clock.at(169)
    assert isinstance(check(gate), Allow)
    assert gate.consume(SEND, asked.approval.args_digest) is not None


def test_an_allow_picked_at_110_s_is_not_usable_at_171_s(
    gate: ConsentGate, clock: FakeClock
) -> None:
    asked = ask(gate)
    clock.at(110)
    pick(gate, asked)
    clock.at(171)
    assert gate.consume(SEND, asked.approval.args_digest) is None
    assert asked.approval.status is ApprovalStatus.EXPIRED
    assert gate.recent_outcome(asked.approval.args_digest) is None


def test_an_unused_allow_is_reported_by_the_sweep_and_the_next_call_asks_again(
    gate: ConsentGate, clock: FakeClock
) -> None:
    asked = ask(gate)
    pick(gate, asked)
    clock.advance(61)
    assert [(a.id, previous) for a, previous in gate.sweep()] == [
        (asked.approval.id, ApprovalStatus.CONFIRMED)
    ]
    again = check(gate)
    assert isinstance(again, Ask)
    assert again.approval.id != asked.approval.id


# BR12.1, BR12.3, NFR3.3: 120 s to answer; unknown picks ---------------------------------------------


def test_a_pick_at_125_s_with_no_sweep_is_unknown(gate: ConsentGate, clock: FakeClock) -> None:
    asked = ask(gate)
    clock.at(125)
    assert pick(gate, asked) == "unknown"
    assert asked.approval.status is ApprovalStatus.EXPIRED
    assert gate.consume(SEND, asked.approval.args_digest) is None


def test_an_unknown_id_or_an_answered_approval_changes_nothing(gate: ConsentGate) -> None:
    asked = ask(gate)
    forged = asked.approval.question.replace(asked.approval.id, "aaaaaaaaaa")
    assert pick(gate, asked, question=forged) == "unknown"
    assert pick(gate, asked, "Deny") == "declined"
    outcome = gate.recent_outcome(asked.approval.args_digest)
    assert outcome is not None
    assert pick(gate, asked, "Allow") == "unknown"
    assert asked.approval.status is ApprovalStatus.DECLINED
    assert gate.recent_outcome(asked.approval.args_digest) == outcome


# BR12.4, NFR3.6: repeat calls after a Deny or a timeout ---------------------------------------------


def test_after_a_deny_the_same_call_is_refused_declined_for_60_s(
    gate: ConsentGate, clock: FakeClock
) -> None:
    asked = ask(gate)
    pick(gate, asked, "Deny")
    clock.advance(60)
    assert check(gate) == Refuse("declined", DECLINED_MESSAGE)
    assert "ask_again set to true" in DECLINED_MESSAGE
    assert gate.open_count() == 0
    clock.advance(1)
    assert isinstance(check(gate), Ask)


def test_a_different_call_after_a_deny_is_asked(gate: ConsentGate) -> None:
    pick(gate, ask(gate), "Deny")
    assert isinstance(check(gate, text="something else"), Ask)


def test_after_a_timeout_the_same_call_is_refused_timed_out_for_60_s(
    gate: ConsentGate, clock: FakeClock
) -> None:
    ask(gate)
    clock.at(121)
    assert check(gate) == Refuse("timed_out", TIMED_OUT_MESSAGE)
    clock.at(180)
    assert check(gate) == Refuse("timed_out", TIMED_OUT_MESSAGE)
    clock.at(181)
    assert isinstance(check(gate), Ask)


def test_the_remembered_outcome_is_visible_to_tool_call(gate: ConsentGate) -> None:
    asked = ask(gate)
    pick(gate, asked, "Deny")
    outcome = gate.recent_outcome(asked.approval.args_digest)
    assert outcome is not None
    assert outcome.outcome == "declined"
    assert outcome.approval_id == asked.approval.id
    assert outcome.until == NOW + timedelta(seconds=60)


def test_a_lookup_answers_without_fields_until_a_new_question_is_needed(
    gate: ConsentGate,
) -> None:
    digest = digest_of(SEND, text="hello")
    assert gate.lookup(SEND, digest) is None
    asked = ask(gate)
    assert gate.lookup(SEND, digest) == asked
    pick(gate, asked)
    assert isinstance(gate.lookup(SEND, digest), Allow)


# BR12.5, NFR3.4: ask_again ------------------------------------------------------------------------


def test_ask_again_drops_the_outcome_and_asks_a_new_question(gate: ConsentGate) -> None:
    first = ask(gate)
    pick(gate, first, "Deny")
    again = ask(gate, ask_again=True)
    assert again.approval.id != first.approval.id
    assert again.approval.status is ApprovalStatus.PENDING
    events = gate.drain_events()
    assert [(e.kind, e.approval_id) for e in events] == [("ask_again", first.approval.id)]


def test_ask_again_never_changes_status_or_use_by(gate: ConsentGate, clock: FakeClock) -> None:
    asked = ask(gate)
    pick(gate, asked)
    use_by = asked.approval.use_by
    assert isinstance(check(gate, ask_again=True), Allow)
    assert asked.approval.status is ApprovalStatus.CONFIRMED
    assert asked.approval.use_by == use_by
    pending = ask(gate, text="other")
    assert check(gate, text="other", ask_again=True) == pending
    assert pending.approval.status is ApprovalStatus.PENDING
    assert pending.approval.use_by is None
    assert [e.kind for e in gate.drain_events()] == ["ask_again_unneeded", "ask_again_unneeded"]


def test_ask_again_without_a_remembered_outcome_changes_nothing(gate: ConsentGate) -> None:
    assert isinstance(check(gate, ask_again=True), Ask)
    assert [e.kind for e in gate.drain_events()] == ["ask_again_unneeded"]


def test_a_third_ask_again_within_10_minutes_is_refused(gate: ConsentGate) -> None:
    pick(gate, ask(gate), "Deny")
    for _ in range(ASK_AGAIN_LIMIT):
        pick(gate, ask(gate, ask_again=True), "Deny")
    assert ASK_AGAIN_LIMIT == 2
    assert check(gate, ask_again=True) == Refuse("declined", ASKED_TOO_OFTEN_MESSAGE)
    assert gate.open_count() == 0
    assert [e.kind for e in gate.drain_events()] == [
        "ask_again",
        "ask_again",
        "ask_again_refused",
    ]


def test_the_ask_again_count_outlives_until(gate: ConsentGate, clock: FakeClock) -> None:
    pick(gate, ask(gate), "Deny")
    pick(gate, ask(gate, ask_again=True), "Deny")  # first reopening
    clock.advance(61)
    pick(gate, ask(gate), "Deny")  # the outcome had cleared: a plain call asks
    pick(gate, ask(gate, ask_again=True), "Deny")  # second reopening
    clock.advance(30)
    assert check(gate, ask_again=True) == Refuse("declined", ASKED_TOO_OFTEN_MESSAGE)
    clock.advance(31)
    assert isinstance(check(gate, ask_again=True), Ask)  # outcome cleared: asks as Q1 A allows


def test_the_ask_again_window_restarts_after_10_minutes(
    gate: ConsentGate, clock: FakeClock
) -> None:
    pick(gate, ask(gate), "Deny")
    pick(gate, ask(gate, ask_again=True), "Deny")
    pick(gate, ask(gate, ask_again=True), "Deny")
    clock.advance(601)
    pick(gate, ask(gate), "Deny")
    assert isinstance(check(gate, ask_again=True), Ask)


def test_ask_again_also_reopens_after_a_timeout(gate: ConsentGate, clock: FakeClock) -> None:
    ask(gate)
    clock.advance(121)
    assert isinstance(check(gate), Refuse)
    assert isinstance(check(gate, ask_again=True), Ask)


# BR12.6: at most 100 remembered outcomes ------------------------------------------------------------


def test_remembered_outcomes_are_capped_at_100_oldest_dropped_first(gate: ConsentGate) -> None:
    for number in range(MAX_RECENT_OUTCOMES + 1):
        pick(gate, ask(gate, text=f"n{number}"), "Deny")
    assert MAX_RECENT_OUTCOMES == 100
    assert gate.recent_outcome(digest_of(SEND, text="n0")) is None
    assert gate.recent_outcome(digest_of(SEND, text="n1")) is not None
    assert isinstance(check(gate, text="n0"), Ask)


def test_the_sweep_forgets_outcomes_once_their_window_is_over(
    gate: ConsentGate, clock: FakeClock
) -> None:
    pick(gate, ask(gate), "Deny")
    pick(gate, ask(gate, ask_again=True), "Deny")
    pick(gate, ask(gate, text="never asked again"), "Deny")
    clock.advance(61)
    gate.sweep()
    assert gate.recent_outcome(digest_of(SEND, text="hello")) is None
    assert gate.remembered_count() == 1  # kept, cleared, while its ask_again window runs
    clock.advance(600)
    gate.sweep()
    assert gate.remembered_count() == 0


def test_the_sweep_reports_an_unanswered_question_and_remembers_it(
    gate: ConsentGate, clock: FakeClock
) -> None:
    asked = ask(gate)
    clock.advance(121)
    assert [(a.id, p) for a, p in gate.sweep()] == [(asked.approval.id, ApprovalStatus.PENDING)]
    outcome = gate.recent_outcome(asked.approval.args_digest)
    assert outcome is not None
    assert outcome.outcome == "timed_out"
    assert outcome.until == NOW + timedelta(seconds=180)


# BR13.2, BR13.6, BR13.8: marks -------------------------------------------------------------------


def mark_fields(unread: int, tool: str = MARK_TOOL) -> PromptFields:
    return PromptFields(action="mark", tool=tool, chat=ALICE, unread=unread)


MARK_DIGEST = canonical_digest(MARK_TOOL, {"chat": ALICE.id, "topic_id": None})


def test_a_mark_with_nothing_unread_is_allowed_with_no_approval(gate: ConsentGate) -> None:
    assert isinstance(gate.mark_check(MARK_TOOL, MARK_DIGEST, mark_fields(0), 0), Allow)
    assert gate.open_count() == 0


def test_a_mark_with_unread_messages_asks_and_stores_mark_upto(gate: ConsentGate) -> None:
    asked = gate.mark_check(MARK_TOOL, MARK_DIGEST, mark_fields(3), 57)
    assert isinstance(asked, Ask)
    assert asked.approval.mark_upto == 57
    assert asked.approval.write_kind == "mark"
    assert asked.approval.option_descriptions == ("Mark as read", "Leave unread")
    assert "Mark 3 unread message(s) in «Alice»" in asked.approval.question


def test_the_read_tools_mark_uses_its_own_question(gate: ConsentGate) -> None:
    digest = canonical_digest(READ_TOOL, {"chat": ALICE.id, "mark_read": True})
    asked = gate.mark_check(READ_TOOL, digest, mark_fields(2, READ_TOOL), 9)
    assert isinstance(asked, Ask)
    assert "After reading, mark «Alice»" in asked.approval.question


def test_a_mark_permission_covers_one_call(gate: ConsentGate) -> None:
    asked = gate.mark_check(MARK_TOOL, MARK_DIGEST, mark_fields(3), 57)
    assert isinstance(asked, Ask)
    pick(gate, asked)
    used = gate.consume(MARK_TOOL, MARK_DIGEST)
    assert used is not None
    assert used.mark_upto == 57
    assert gate.consume(MARK_TOOL, MARK_DIGEST) is None


def test_a_declined_mark_is_remembered_like_any_write(gate: ConsentGate) -> None:
    asked = gate.mark_check(MARK_TOOL, MARK_DIGEST, mark_fields(3), 57)
    assert isinstance(asked, Ask)
    pick(gate, asked, "Deny")
    refused = gate.mark_check(MARK_TOOL, MARK_DIGEST, mark_fields(3), 57)
    assert refused == Refuse("declined", DECLINED_MESSAGE)


# Withdrawing an approval whose prompt could not be logged (BR15.1) ------------------------------------


def test_a_withdrawn_approval_is_gone(gate: ConsentGate) -> None:
    asked = ask(gate)
    gate.withdraw(asked.approval.id)
    assert gate.open_count() == 0
    assert pick(gate, asked) == "unknown"
    assert gate.recent_outcome(asked.approval.args_digest) is None
