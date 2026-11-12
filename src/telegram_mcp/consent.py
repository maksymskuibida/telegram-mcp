"""The ConsentGate: which actions need the person's confirmation, the approvals that carry it,
and the remembered Denies and timeouts (C2; rules BR3.1 to BR3.9, BR10, BR12, BR13.2,
BR13.6, BR13.8).

An approval is prepared by ``check`` (or ``mark_check``) from the fields a tool's describer
filled, confirmed only by the person's exact pick through ``answer``, and used at most once by
``consume``. An unanswered question expires 120 s after it was asked; an Allow must be used
within 60 s of the pick. A Deny or a timeout is remembered for 60 s so that the same call is
told why instead of asked again, unless the person asked in the chat to try again
(``ask_again``). Everything lives in memory only, so a restart forgets it."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, NamedTuple

from telegram_mcp.gateway.models import SAVED_MESSAGES, ChatRef
from telegram_mcp.prompts import (
    KINDS,
    MARK,
    NOT_ALLOWED_MESSAGE,
    PromptError,
    PromptFields,
    render_question,
    sanitize_chat_name,
)

__all__ = ["sanitize_chat_name"]

APPROVAL_TTL = timedelta(seconds=120)
USE_WINDOW = timedelta(seconds=60)
OUTCOME_MEMORY = timedelta(seconds=60)
ASK_AGAIN_WINDOW = timedelta(minutes=10)
ASK_AGAIN_LIMIT = 2
MAX_OPEN_APPROVALS = 100
MAX_RECENT_OUTCOMES = 100
OPTIONS = ("Allow", "Deny")
ALLOW = "Allow"
_ID_PATTERN = re.compile(r"^\[tg-([a-z2-7]{10})\] ")
_ID_LENGTH = 10
TOO_MANY_MESSAGE = "too many open confirmations; answer or wait for the open ones"
NOT_FOUND_TOOL_MESSAGE = "This version has no tool with that name."
DECLINED_MESSAGE = (
    "The person declined this action. Do not try it again unless the person asks you to in "
    "the chat; then call it again with ask_again set to true."
)
TIMED_OUT_MESSAGE = (
    "The person did not answer in time. Do not try it again unless the person asks you to in "
    "the chat; then call it again with ask_again set to true."
)
ASKED_TOO_OFTEN_MESSAGE = (
    "The person has declined this action several times; ask them in the chat before trying "
    "again later."
)
OUTCOME_MESSAGES = {"declined": DECLINED_MESSAGE, "timed_out": TIMED_OUT_MESSAGE}


class ActionKind(StrEnum):
    READ = "read"
    WRITE = "write"
    MARK = "mark"


@dataclass(frozen=True)
class ActionDecl:
    tool: str
    kind: ActionKind
    write_kind: str | None = None
    marks_read_option: bool = False


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    DECLINED = "declined"
    EXPIRED = "expired"
    USED = "used"


@dataclass
class PendingApproval:
    """A prepared question and its decision. The Telegram call runs against ``prompt`` and the
    send fields copied from it, never against values resolved again (BR3.9)."""

    id: str
    tool: str
    args_digest: str
    write_kind: str
    prompt: PromptFields
    question: str
    option_descriptions: tuple[str, str]
    created_at: datetime
    expires_at: datetime
    chat_id: int | None = None
    chat_name: str = ""
    chat_type: str = ""
    content: str = ""
    reply_to: int | None = None
    topic_id: int | None = None
    options: tuple[str, ...] = OPTIONS
    mark_upto: int | None = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    answered_at: datetime | None = None
    use_by: datetime | None = None


Outcome = Literal["declined", "timed_out"]


@dataclass
class RecentOutcome:
    """The remembered Deny or timeout of one digest (BR12.4 to BR12.6). ``outcome`` is None
    once ``until`` has passed while the ask_again window is still counted."""

    args_digest: str
    outcome: Outcome | None
    approval_id: str
    until: datetime
    asks_again: int = 0
    asks_again_window_start: datetime | None = None


GateEventKind = Literal["ask_again", "ask_again_refused", "ask_again_unneeded"]


@dataclass(frozen=True)
class GateEvent:
    """Something the broker logs: every ask_again, honoured or not (BR12.5, BR15.1)."""

    kind: GateEventKind
    tool: str
    args_digest: str
    approval_id: str | None = None
    code: str | None = None


@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class Ask:
    approval: PendingApproval


@dataclass(frozen=True)
class Refuse:
    code: str
    message: str


Decision = Allow | Ask | Refuse


class AnswerOutcome(StrEnum):
    CONFIRMED = "confirmed"
    DECLINED = "declined"
    UNKNOWN = "unknown"


class AnswerResult(NamedTuple):
    outcome: AnswerOutcome
    approval: PendingApproval | None


class DigestError(ValueError):
    """The arguments cannot be hashed canonically (they hold a float)."""


def canonical_digest(tool: str, args: Mapping[str, Any]) -> str:
    """sha256 of the UTF-8 JSON of {"tool", "args"} with sorted keys and no whitespace (C1).

    ``args`` must already be validated with every default filled and ``ask_again`` removed."""
    _refuse_floats(args)
    payload = json.dumps(
        {"tool": tool, "args": args}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _refuse_floats(value: object) -> None:
    if isinstance(value, float):
        raise DigestError("floats cannot be part of a confirmed action")
    if isinstance(value, Mapping):
        for item in value.values():
            _refuse_floats(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _refuse_floats(item)


def _new_id() -> str:
    return base64.b32encode(secrets.token_bytes(10)).decode("ascii").lower()[:_ID_LENGTH]


@dataclass
class _Store:
    approvals: dict[str, PendingApproval] = field(default_factory=dict)
    outcomes: dict[str, RecentOutcome] = field(default_factory=dict)
    newly_expired: list[tuple[PendingApproval, ApprovalStatus]] = field(default_factory=list)
    events: list[GateEvent] = field(default_factory=list)


class ConsentGate:
    def __init__(self, decls: Mapping[str, ActionDecl], *, clock: Callable[[], datetime]) -> None:
        self._decls = dict(decls)
        self._clock = clock
        self._store = _Store()

    # Kinds -----------------------------------------------------------------------------------

    def kind_of(self, tool: str) -> ActionKind:
        """The declared kind; a tool with no declaration is a write (BR3.1)."""
        decl = self._decls.get(tool)
        return decl.kind if decl is not None else ActionKind.WRITE

    def effective_kind(self, tool: str, args: Mapping[str, Any]) -> ActionKind:
        """Mark for the mark tool, and for the read tool asked to mark (BR10.2)."""
        decl = self._decls.get(tool)
        if decl is None:
            return ActionKind.WRITE
        if decl.marks_read_option and args.get("mark_read") is True:
            return ActionKind.MARK
        return decl.kind

    def row_refusal(self, tool: str) -> Refuse | None:
        """The refusal for a write or mark with no kind table row, or None (BR10.1)."""
        row = self._kind_row(tool)
        return row if isinstance(row, Refuse) else None

    def _kind_row(self, tool: str) -> str | Refuse:
        """The kind table row a write or mark runs through; none refuses (BR10.1)."""
        decl = self._decls.get(tool)
        if decl is None:
            return Refuse("not_found", NOT_FOUND_TOOL_MESSAGE)
        if decl.kind is ActionKind.MARK or decl.marks_read_option:
            return MARK
        if decl.write_kind is None or decl.write_kind not in KINDS or decl.write_kind == MARK:
            return Refuse("not_allowed", NOT_ALLOWED_MESSAGE)
        return decl.write_kind

    # Deciding ----------------------------------------------------------------------------------

    def open_count(self) -> int:
        self._expire_due()
        return sum(1 for a in self._store.approvals.values() if _is_open(a))

    def remembered_count(self) -> int:
        return len(self._store.outcomes)

    def lookup(self, tool: str, digest: str, *, ask_again: bool = False) -> Decision | None:
        """What an existing approval or remembered outcome answers for this call, before any
        question is built: allow a confirmed one, ask a pending one again, refuse a remembered
        Deny or timeout. None means a new question is needed (W1 step 4)."""
        if self.kind_of(tool) is ActionKind.READ and not self._marks(tool):
            return Allow()
        row = self._kind_row(tool)
        if isinstance(row, Refuse):
            return row
        self._expire_due()
        for approval in self._store.approvals.values():
            if approval.tool == tool and approval.args_digest == digest:
                if approval.status is ApprovalStatus.CONFIRMED:
                    self._note_unneeded(ask_again, tool, digest)
                    return Allow()
                if approval.status is ApprovalStatus.PENDING:
                    self._note_unneeded(ask_again, tool, digest)
                    return Ask(approval)
        remembered = self._live_outcome(digest)
        if ask_again:
            return self._ask_again(tool, digest, remembered)
        if remembered is not None and remembered.outcome is not None:
            return Refuse(remembered.outcome, OUTCOME_MESSAGES[remembered.outcome])
        return None

    def _marks(self, tool: str) -> bool:
        decl = self._decls.get(tool)
        return decl is not None and decl.marks_read_option

    def check(
        self,
        tool: str,
        digest: str,
        fields: PromptFields,
        *,
        ask_again: bool = False,
        mark_upto: int | None = None,
    ) -> Decision:
        """Allow, ask or refuse; a new question is rendered from ``fields`` only when no
        approval or remembered outcome answers first (BR3.2, BR11, BR12.4, BR12.5)."""
        found = self.lookup(tool, digest, ask_again=ask_again)
        if found is not None:
            return found
        row = self._kind_row(tool)
        if isinstance(row, Refuse):  # lookup refuses this first; kept for the type
            return row
        if fields.action != row:
            return Refuse("not_allowed", NOT_ALLOWED_MESSAGE)
        if sum(1 for a in self._store.approvals.values() if _is_open(a)) >= MAX_OPEN_APPROVALS:
            return Refuse("telegram_error", TOO_MANY_MESSAGE)
        try:
            return Ask(self._prepare(tool, digest, row, fields, mark_upto))
        except PromptError as error:
            return Refuse(error.code, error.message)

    def mark_check(
        self,
        tool: str,
        digest: str,
        fields: PromptFields,
        newest_id: int,
        *,
        ask_again: bool = False,
    ) -> Decision:
        """A mark with nothing unread runs with no question; otherwise one question names the
        chat and the count, and the approval marks only up to ``newest_id`` (BR13.2, BR13.3)."""
        if not fields.unread:
            return Allow()
        return self.check(tool, digest, fields, ask_again=ask_again, mark_upto=newest_id)

    def _prepare(
        self,
        tool: str,
        digest: str,
        kind: str,
        fields: PromptFields,
        mark_upto: int | None,
    ) -> PendingApproval:
        approval_id = _new_id()
        while approval_id in self._store.approvals:
            approval_id = _new_id()
        now = self._clock()
        question, descriptions = render_question(approval_id, fields, now=now)
        chat = fields.chat
        approval = PendingApproval(
            id=approval_id,
            tool=tool,
            args_digest=digest,
            write_kind=kind,
            prompt=fields,
            question=question,
            option_descriptions=descriptions,
            created_at=now,
            expires_at=now + APPROVAL_TTL,
            chat_id=chat.id if chat is not None else None,
            chat_name=_chat_name(chat),
            chat_type=chat.type if chat is not None else "",
            content=fields.content,
            reply_to=fields.details.message_id if kind == "send" else None,
            topic_id=fields.topic.topic_id if fields.topic is not None else None,
            mark_upto=mark_upto,
        )
        self._store.approvals[approval_id] = approval
        return approval

    def withdraw(self, approval_id: str) -> None:
        """Forget an approval whose question could not be logged; it was never asked (BR15.1)."""
        self._store.approvals.pop(approval_id, None)

    # ask_again and remembered outcomes ----------------------------------------------------------

    def _note_unneeded(self, ask_again: bool, tool: str, digest: str) -> None:
        if ask_again:
            self._store.events.append(GateEvent("ask_again_unneeded", tool, digest))

    def _ask_again(
        self, tool: str, digest: str, remembered: RecentOutcome | None
    ) -> Decision | None:
        """BR12.5: drop a remembered outcome so a new question is asked, at most twice per
        digest in 10 minutes. It never confirms, extends or reuses an approval."""
        if remembered is None or remembered.outcome is None:
            approval_id = remembered.approval_id if remembered is not None else None
            self._store.events.append(
                GateEvent("ask_again_unneeded", tool, digest, approval_id=approval_id)
            )
            return None
        now = self._clock()
        start = remembered.asks_again_window_start
        if start is None or now - start > ASK_AGAIN_WINDOW:
            remembered.asks_again, remembered.asks_again_window_start = 0, now
        if remembered.asks_again >= ASK_AGAIN_LIMIT:
            self._store.events.append(
                GateEvent(
                    "ask_again_refused",
                    tool,
                    digest,
                    approval_id=remembered.approval_id,
                    code=remembered.outcome,
                )
            )
            return Refuse(remembered.outcome, ASKED_TOO_OFTEN_MESSAGE)
        remembered.asks_again += 1
        remembered.outcome = None
        self._store.events.append(
            GateEvent("ask_again", tool, digest, approval_id=remembered.approval_id)
        )
        return None

    def recent_outcome(self, digest: str) -> RecentOutcome | None:
        """The remembered Deny or timeout for this digest while it still applies (BR12.4)."""
        self._expire_due()
        remembered = self._live_outcome(digest)
        if remembered is None or remembered.outcome is None:
            return None
        return remembered

    def _live_outcome(self, digest: str) -> RecentOutcome | None:
        remembered = self._store.outcomes.get(digest)
        if (
            remembered is not None
            and remembered.outcome is not None
            and self._clock() > remembered.until
        ):
            remembered.outcome = None
        return remembered

    def _remember(self, approval: PendingApproval, outcome: Outcome, moment: datetime) -> None:
        outcomes = self._store.outcomes
        previous = outcomes.pop(approval.args_digest, None)
        outcomes[approval.args_digest] = RecentOutcome(
            args_digest=approval.args_digest,
            outcome=outcome,
            approval_id=approval.id,
            until=moment + OUTCOME_MEMORY,
            asks_again=previous.asks_again if previous is not None else 0,
            asks_again_window_start=(
                previous.asks_again_window_start if previous is not None else None
            ),
        )
        while len(outcomes) > MAX_RECENT_OUTCOMES:
            del outcomes[next(iter(outcomes))]

    def drain_events(self) -> list[GateEvent]:
        events, self._store.events = self._store.events, []
        return events

    # Answering and using -------------------------------------------------------------------------

    def answer(
        self,
        question: str,
        options: list[str],
        pick: str,
        *,
        option_descriptions: list[str] | None,
        multi_select: bool | None,
        from_hook: bool,
    ) -> AnswerResult:
        """Record the person's pick. Only an answer relayed by the hook, for a pending,
        unexpired approval, with everything the person saw equal to what was prepared (the
        descriptions stored for its kind included), changes anything; then exactly "Allow"
        confirms, with 60 s to use it, and every other pick declines (BR3.5, BR12.2, BR12.3)."""
        unknown = AnswerResult(AnswerOutcome.UNKNOWN, None)
        match = _ID_PATTERN.match(question) if isinstance(question, str) else None
        if not from_hook or match is None:
            return unknown
        self._expire_due()
        approval = self._store.approvals.get(match[1])
        if approval is None or approval.status is not ApprovalStatus.PENDING:
            return unknown
        shown_as_prepared = (
            question == approval.question
            and list(options) == list(approval.options)
            and option_descriptions is not None
            and list(option_descriptions) == list(approval.option_descriptions)
            and multi_select is False
        )
        if not shown_as_prepared:
            return unknown
        now = self._clock()
        approval.answered_at = now
        if pick == ALLOW:
            approval.status = ApprovalStatus.CONFIRMED
            approval.use_by = now + USE_WINDOW
            return AnswerResult(AnswerOutcome.CONFIRMED, approval)
        approval.status = ApprovalStatus.DECLINED
        self._remember(approval, "declined", now)
        return AnswerResult(AnswerOutcome.DECLINED, approval)

    def consume(self, tool: str, digest: str) -> PendingApproval | None:
        """Use the one confirmed approval with this tool and digest while its 60 s run; it can
        never be used again (BR3.6, BR12.2). None means the action needs confirmation."""
        self._expire_due()
        for approval in self._store.approvals.values():
            if (
                approval.tool == tool
                and approval.args_digest == digest
                and approval.status is ApprovalStatus.CONFIRMED
            ):
                approval.status = ApprovalStatus.USED
                return approval
        return None

    # Expiry --------------------------------------------------------------------------------------

    def sweep(self) -> list[tuple[PendingApproval, ApprovalStatus]]:
        """Expire what is due, forget finished approvals and outcomes whose windows are over,
        and return each approval that expired since the last sweep with the status it had: a
        pending one timed out, a confirmed one was allowed but not used (BR3.7, BR12.6)."""
        self._expire_due()
        expired, self._store.newly_expired = self._store.newly_expired, []
        self._store.approvals = {k: a for k, a in self._store.approvals.items() if _is_open(a)}
        now = self._clock()
        for digest in list(self._store.outcomes):
            remembered = self._live_outcome(digest)
            if remembered is None or remembered.outcome is not None:
                continue
            start = remembered.asks_again_window_start
            if start is None or now - start > ASK_AGAIN_WINDOW:
                del self._store.outcomes[digest]
        return expired

    def drop_open(self) -> list[PendingApproval]:
        """Expire every open approval now (logout): none can be confirmed or used any more.
        They are returned, and the next ``sweep`` reports each with the status it had."""
        dropped = [a for a in self._store.approvals.values() if _is_open(a)]
        for approval in dropped:
            self._store.newly_expired.append((approval, approval.status))
            approval.status = ApprovalStatus.EXPIRED
        return dropped

    def _expire_due(self) -> None:
        now = self._clock()
        for approval in self._store.approvals.values():
            if approval.status is ApprovalStatus.PENDING and now > approval.expires_at:
                self._store.newly_expired.append((approval, approval.status))
                approval.status = ApprovalStatus.EXPIRED
                self._remember(approval, "timed_out", approval.expires_at)
            elif (
                approval.status is ApprovalStatus.CONFIRMED
                and approval.use_by is not None
                and now > approval.use_by
            ):
                self._store.newly_expired.append((approval, approval.status))
                approval.status = ApprovalStatus.EXPIRED


def _chat_name(chat: ChatRef | None) -> str:
    if chat is None:
        return ""
    return SAVED_MESSAGES if chat.saved_messages else chat.name


def _is_open(approval: PendingApproval) -> bool:
    return approval.status in (ApprovalStatus.PENDING, ApprovalStatus.CONFIRMED)
