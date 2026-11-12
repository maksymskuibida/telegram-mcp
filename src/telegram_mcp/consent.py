"""The ConsentGate: which actions need the person's confirmation, and the approvals that
carry it (C2; rules BR3.1 to BR3.9).

An approval is prepared by ``check``, confirmed only by the person's exact pick through
``answer``, and used at most once by ``consume``. It expires 120 s after it was prepared.
Approvals live in memory only, so a restart forgets them."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, NamedTuple

from telegram_mcp.gateway.models import SAVED_MESSAGES, ChatRef

APPROVAL_TTL = timedelta(seconds=120)
MAX_OPEN_APPROVALS = 100
OPTIONS = ("Allow", "Deny")
OPTION_DESCRIPTIONS = ("Send it", "Do not send")
ALLOW = "Allow"
CHAT_NAME_LIMIT = 64
_ID_PATTERN = re.compile(r"^\[tg-([a-z2-7]{10})\] ")
_ID_LENGTH = 10
_HIDDEN_CATEGORIES = {"Cc", "Cf", "Zl", "Zp"}
TOO_MANY_MESSAGE = "too many open confirmations; answer or wait for the open ones"


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
    id: str
    tool: str
    args_digest: str
    chat_id: int
    chat_name: str
    chat_type: str
    content: str
    reply_to: int | None
    topic_id: int | None
    question: str
    created_at: datetime
    expires_at: datetime
    options: tuple[str, ...] = OPTIONS
    mark_upto: int | None = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    answered_at: datetime | None = None


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

    ``args`` must already be validated with every default filled."""
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


def sanitize_chat_name(name: str) -> str:
    """Show a chat name so a hostile title cannot pose as another chat or as the tool's own
    text (NFR4.2): control, format and separator characters become spaces, « and » become
    quotes, and the result is cut to 64 characters."""
    shown = "".join(
        " " if unicodedata.category(char) in _HIDDEN_CATEGORIES else char for char in name
    )
    shown = shown.replace("«", '"').replace("»", '"')
    if len(shown) > CHAT_NAME_LIMIT:
        shown = shown[: CHAT_NAME_LIMIT - 1] + "…"
    return shown


def build_send_question(
    approval_id: str, chat: ChatRef, text: str, reply_to: int | None, topic_id: int | None
) -> str:
    """The exact question for a send (BR3.4); the full text follows the first line. The
    account's own chat, as the gateway marked it, is named plainly as your Saved Messages."""
    if chat.saved_messages:
        line = f"[tg-{approval_id}] Send this message to your {SAVED_MESSAGES} (id {chat.id})?"
    else:
        line = (
            f"[tg-{approval_id}] Send this message to «{sanitize_chat_name(chat.name)}» "
            f"({chat.type}, id {chat.id})?"
        )
    if reply_to is not None:
        line += f" Reply to message {reply_to}."
    if topic_id is not None:
        line += f" In topic {topic_id}."
    return f"{line}\n{text}"


def _new_id() -> str:
    return base64.b32encode(secrets.token_bytes(10)).decode("ascii").lower()[:_ID_LENGTH]


class ConsentGate:
    def __init__(self, decls: Mapping[str, ActionDecl], *, clock: Callable[[], datetime]) -> None:
        self._decls = dict(decls)
        self._clock = clock
        self._approvals: dict[str, PendingApproval] = {}
        self._newly_expired: list[tuple[PendingApproval, ApprovalStatus]] = []

    def kind_of(self, tool: str) -> ActionKind:
        """The declared kind; a tool with no declaration is a write (BR3.1)."""
        decl = self._decls.get(tool)
        return decl.kind if decl is not None else ActionKind.WRITE

    def open_count(self) -> int:
        self._expire_due()
        return sum(1 for a in self._approvals.values() if _is_open(a))

    def check(
        self, tool: str, digest: str, chat: ChatRef | None, args: Mapping[str, Any]
    ) -> Decision:
        """Allow a read or a confirmed write; otherwise ask, reusing a pending approval with
        the same digest or preparing a new one (BR3.2, BR3.4)."""
        if self.kind_of(tool) is ActionKind.READ:
            return Allow()
        decl = self._decls.get(tool)
        if decl is None or decl.write_kind != "send" or chat is None:
            return Refuse("not_found", f"{tool} is not a tool this version knows.")
        self._expire_due()
        for approval in self._approvals.values():
            if approval.tool == tool and approval.args_digest == digest:
                if approval.status is ApprovalStatus.CONFIRMED:
                    return Allow()
                if approval.status is ApprovalStatus.PENDING:
                    return Ask(approval)
        # Expired approvals were swept just above, so only live ones count against the cap.
        if sum(1 for a in self._approvals.values() if _is_open(a)) >= MAX_OPEN_APPROVALS:
            return Refuse("telegram_error", TOO_MANY_MESSAGE)
        return Ask(self._prepare(tool, digest, chat, args))

    def _prepare(
        self, tool: str, digest: str, chat: ChatRef, args: Mapping[str, Any]
    ) -> PendingApproval:
        approval_id = _new_id()
        while approval_id in self._approvals:
            approval_id = _new_id()
        now = self._clock()
        text, reply_to, topic_id = str(args["text"]), args.get("reply_to"), args.get("topic_id")
        approval = PendingApproval(
            id=approval_id,
            tool=tool,
            args_digest=digest,
            chat_id=chat.id,
            chat_name=SAVED_MESSAGES if chat.saved_messages else chat.name,
            chat_type=chat.type,
            content=text,
            reply_to=reply_to,
            topic_id=topic_id,
            question=build_send_question(approval_id, chat, text, reply_to, topic_id),
            created_at=now,
            expires_at=now + APPROVAL_TTL,
        )
        self._approvals[approval_id] = approval
        return approval

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
        unexpired approval, with everything the person saw equal to what was prepared, changes
        anything; then exactly "Allow" confirms and every other pick declines (BR3.5)."""
        unknown = AnswerResult(AnswerOutcome.UNKNOWN, None)
        match = _ID_PATTERN.match(question) if isinstance(question, str) else None
        if not from_hook or match is None:
            return unknown
        self._expire_due()
        approval = self._approvals.get(match[1])
        if approval is None or approval.status is not ApprovalStatus.PENDING:
            return unknown
        shown_as_prepared = (
            question == approval.question
            and list(options) == list(approval.options)
            and option_descriptions is not None
            and list(option_descriptions) == list(OPTION_DESCRIPTIONS)
            and multi_select is False
        )
        if not shown_as_prepared:
            return unknown
        approval.answered_at = self._clock()
        if pick == ALLOW:
            approval.status = ApprovalStatus.CONFIRMED
            return AnswerResult(AnswerOutcome.CONFIRMED, approval)
        approval.status = ApprovalStatus.DECLINED
        return AnswerResult(AnswerOutcome.DECLINED, approval)

    def consume(self, tool: str, digest: str) -> PendingApproval | None:
        """Use the one confirmed, unexpired, unused approval with this tool and digest; it
        can never be used again (BR3.6). None means the action needs confirmation."""
        self._expire_due()
        for approval in self._approvals.values():
            if (
                approval.tool == tool
                and approval.args_digest == digest
                and approval.status is ApprovalStatus.CONFIRMED
            ):
                approval.status = ApprovalStatus.USED
                return approval
        return None

    def sweep(self) -> list[tuple[PendingApproval, ApprovalStatus]]:
        """Expire what is due, forget finished approvals, and return each approval that expired
        since the last sweep with the status it had (for the log, BR3.7)."""
        self._expire_due()
        expired, self._newly_expired = self._newly_expired, []
        self._approvals = {k: a for k, a in self._approvals.items() if _is_open(a)}
        return expired

    def drop_open(self) -> list[PendingApproval]:
        """Expire every open approval now (logout): none can be confirmed or used any more.
        They are returned, and the next ``sweep`` reports each with the status it had."""
        dropped = [a for a in self._approvals.values() if _is_open(a)]
        for approval in dropped:
            self._newly_expired.append((approval, approval.status))
            approval.status = ApprovalStatus.EXPIRED
        return dropped

    def _expire_due(self) -> None:
        now = self._clock()
        for approval in self._approvals.values():
            if _is_open(approval) and now > approval.expires_at:
                self._newly_expired.append((approval, approval.status))
                approval.status = ApprovalStatus.EXPIRED


def _is_open(approval: PendingApproval) -> bool:
    return approval.status in (ApprovalStatus.PENDING, ApprovalStatus.CONFIRMED)
