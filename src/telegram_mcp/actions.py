"""The action registry: what the broker runs for each tool (C2, C5).

An action pairs a tool's declaration (its kind) with three functions: ``validate`` checks the
arguments against the tool's closed schema and fills every default; ``describe`` (writes only)
reads what a question needs from Telegram and returns PromptFields; ``run`` makes the Telegram
call. The broker owns the order of the checks, the question and the confirmation; an action
never sees an approval it may skip, and never writes question text.

The broker is given a registry, by default U1's two tools. Later tools register here."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from telegram_mcp.consent import ActionDecl, PendingApproval
from telegram_mcp.gateway.protocol import TelegramGateway
from telegram_mcp.prompts import HeadlineDetails, PromptFields, TopicRef
from telegram_mcp.tools import (
    CHATS_LIST,
    DECLS,
    MESSAGES_SEND,
    list_result,
    send_result,
    validate_args,
)

Validator = Callable[[object], dict[str, Any]]
Describer = Callable[[TelegramGateway, dict[str, Any]], Awaitable[PromptFields]]
Runner = Callable[[TelegramGateway, PendingApproval | None, dict[str, Any]], Awaitable[Any]]
NewestReturned = Callable[[Any], int | None]


@dataclass(frozen=True)
class ConsentAction:
    """One tool as the broker runs it.

    - ``describe`` is required for a write; a mark is described by the broker itself from the
      unread check.
    - ``run`` receives the consumed approval for a write (its stored values are what was
      approved, U1 rule 3.9) and None for a read. The mark tool has no ``run``: the broker
      marks up to the approval's ``mark_upto``.
    - ``newest_returned`` is for a read tool with a mark_read option: the newest message id a
      read result holds, so the mark never goes past what was returned (BR13.3)."""

    decl: ActionDecl
    validate: Validator
    describe: Describer | None = None
    run: Runner | None = None
    newest_returned: NewestReturned | None = None

    @property
    def tool(self) -> str:
        return self.decl.tool


class ActionRegistry:
    def __init__(self, actions: Iterable[ConsentAction] = ()) -> None:
        self._actions: dict[str, ConsentAction] = {}
        for action in actions:
            self.register(action)

    def register(self, action: ConsentAction) -> None:
        if action.tool in self._actions:
            raise ValueError(f"{action.tool} is already registered")
        self._actions[action.tool] = action

    def get(self, tool: str) -> ConsentAction | None:
        return self._actions.get(tool)

    def __contains__(self, tool: object) -> bool:
        return tool in self._actions

    def decls(self) -> dict[str, ActionDecl]:
        return {tool: action.decl for tool, action in self._actions.items()}


# U1's two tools ---------------------------------------------------------------------------------


def send_prompt(tool: str, chat: Any, args: dict[str, Any]) -> PromptFields:
    """What the send question shows: the resolved chat, the full text, and a reply or a topic
    by id (U1 rule 3.4)."""
    topic_id = args.get("topic_id")
    return PromptFields(
        action="send",
        tool=tool,
        chat=chat,
        content=str(args["text"]),
        topic=TopicRef(topic_id, "") if topic_id is not None else None,
        details=HeadlineDetails(message_id=args.get("reply_to")),
    )


async def _describe_send(gateway: TelegramGateway, args: dict[str, Any]) -> PromptFields:
    return send_prompt(MESSAGES_SEND, await gateway.resolve_chat(int(args["chat"])), args)


async def _run_send(
    gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
) -> Any:
    if approval is None or approval.chat_id is None:
        raise ValueError("a send runs only with its approval")
    sent = await gateway.send_text(
        approval.chat_id, approval.content, approval.reply_to, approval.topic_id
    )
    return send_result(sent)


async def _run_list(
    gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
) -> Any:
    chats, next_offset = await gateway.list_chats(args["folder_id"], args["limit"], args["offset"])
    return list_result(chats, next_offset)


def _validator(tool: str) -> Validator:
    return lambda raw: validate_args(tool, raw if raw is not None else {})


def default_registry() -> ActionRegistry:
    """U1's tools: the chat list (a read) and the text send (a write)."""
    return ActionRegistry(
        [
            ConsentAction(decl=DECLS[CHATS_LIST], validate=_validator(CHATS_LIST), run=_run_list),
            ConsentAction(
                decl=DECLS[MESSAGES_SEND],
                validate=_validator(MESSAGES_SEND),
                describe=_describe_send,
                run=_run_send,
            ),
        ]
    )
