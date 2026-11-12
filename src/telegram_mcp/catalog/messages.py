"""Message tools: the message writes (edit, delete, forward, react, pin, unpin; BR23) and the
message reads (read, pinned, search; BR22).

A describer reads what its question needs (the chat, the named messages) and fills U2's
PromptFields; a runner makes one Telegram call with the values stored in the approval, never a
new lookup (BR21.4, BR23.2)."""

from __future__ import annotations

from typing import Any

from telegram_mcp.actions import ConsentAction
from telegram_mcp.catalog.common import (
    Args,
    CatalogContext,
    chat_dict,
    message_dict,
    message_fields,
    named_messages,
    with_notice,
)
from telegram_mcp.consent import PendingApproval
from telegram_mcp.gateway.models import ChatRef, SearchHit
from telegram_mcp.gateway.protocol import TelegramGateway
from telegram_mcp.prompts import HeadlineDetails, PromptError, PromptFields
from telegram_mcp.tools import (
    DECLS,
    EMOJI_LIMIT,
    MESSAGES_DELETE,
    MESSAGES_EDIT,
    MESSAGES_FORWARD,
    MESSAGES_PIN,
    MESSAGES_PINNED,
    MESSAGES_REACT,
    MESSAGES_READ,
    MESSAGES_READ_LIMIT,
    MESSAGES_SEARCH,
    MESSAGES_UNPIN,
    QUERY_LIMIT,
    SEARCH_LIMIT,
    TEXT_LIMIT,
)


def approved(approval: PendingApproval | None) -> PromptFields:
    """The fields the person approved; a write never runs without them (BR21.4)."""
    if approval is None:
        raise ValueError("a write runs only with its approval")
    return approval.prompt


def approved_chat(approval: PendingApproval | None) -> ChatRef:
    chat = approved(approval).chat
    if chat is None:
        raise ValueError("the approval names no chat")
    return chat


def approved_message_id(approval: PendingApproval | None) -> int:
    number = approved(approval).details.message_id
    if number is None:
        raise ValueError("the approval names no message")
    return number


# tg_messages_edit (BR23.3) ---------------------------------------------------------------------


def _validate_edit(raw: object) -> dict[str, Any]:
    args = Args(MESSAGES_EDIT, raw, ("chat", "message_id", "text"))
    return {
        "chat": args.chat(),
        "message_id": args.required_int("message_id"),
        "text": args.required_text("text", max_len=TEXT_LIMIT),
    }


async def _describe_edit(gateway: TelegramGateway, args: dict[str, Any]) -> PromptFields:
    chat = await gateway.resolve_chat(args["chat"])
    found = await gateway.read_messages(chat.id, 1, ids=[args["message_id"]])
    if not found:
        raise PromptError("not_found", f"Message {args['message_id']} was not found in this chat.")
    message = found[0]
    if not message.outgoing:
        raise PromptError("not_allowed", "You can edit only your own messages.")
    if message.text == args["text"]:
        raise PromptError("invalid_arguments", "The message already has this text.")
    return PromptFields(
        action="edit",
        tool=MESSAGES_EDIT,
        chat=chat,
        content=args["text"],
        previous=message.text,
        details=HeadlineDetails(message_id=message.id),
    )


async def _run_edit(
    gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
) -> Any:
    chat, number = approved_chat(approval), approved_message_id(approval)
    edited = await gateway.edit(chat.id, number, approved(approval).content)
    return {"chat_id": chat.id, "message_id": edited.id, "edited": edited.edited}


# tg_messages_delete (BR23.4) -------------------------------------------------------------------


def _validate_delete(raw: object) -> dict[str, Any]:
    args = Args(MESSAGES_DELETE, raw, ("chat", "message_ids", "for_everyone"))
    return {
        "chat": args.chat(),
        "message_ids": args.ids("message_ids"),
        "for_everyone": args.boolean("for_everyone", False),
    }


async def _describe_delete(gateway: TelegramGateway, args: dict[str, Any]) -> PromptFields:
    chat = await gateway.resolve_chat(args["chat"])
    messages = await named_messages(gateway, chat.id, args["message_ids"])
    return PromptFields(
        action="delete",
        tool=MESSAGES_DELETE,
        chat=chat,
        messages=messages,
        details=HeadlineDetails(for_everyone=args["for_everyone"]),
    )


async def _run_delete(
    gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
) -> Any:
    fields = approved(approval)
    chat = approved_chat(approval)
    ids = [message.id for message in fields.messages]
    everyone = bool(fields.details.for_everyone)
    await gateway.delete(chat.id, ids, everyone)
    return {"chat_id": chat.id, "deleted": ids, "for_everyone": everyone}


# tg_messages_forward (BR23.4) ------------------------------------------------------------------


def _validate_forward(raw: object) -> dict[str, Any]:
    args = Args(MESSAGES_FORWARD, raw, ("from_chat", "message_ids", "to_chat"))
    return {
        "from_chat": args.chat("from_chat"),
        "message_ids": args.ids("message_ids"),
        "to_chat": args.chat("to_chat"),
    }


async def _describe_forward(gateway: TelegramGateway, args: dict[str, Any]) -> PromptFields:
    target = await gateway.resolve_chat(args["to_chat"])
    source = await gateway.resolve_chat(args["from_chat"])
    messages = await named_messages(gateway, source.id, args["message_ids"])
    return PromptFields(
        action="forward",
        tool=MESSAGES_FORWARD,
        chat=target,
        source_chat=source,
        messages=messages,
    )


async def _run_forward(
    gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
) -> Any:
    fields = approved(approval)
    target, source = approved_chat(approval), fields.source_chat
    if source is None:
        raise ValueError("the approval names no source chat")
    ids = [message.id for message in fields.messages]
    new_ids = await gateway.forward(source.id, ids, target.id)
    return {"from_chat": source.id, "to_chat": target.id, "message_ids": new_ids}


# tg_messages_react, tg_messages_pin, tg_messages_unpin -----------------------------------------


def _validate_react(raw: object) -> dict[str, Any]:
    args = Args(MESSAGES_REACT, raw, ("chat", "message_id", "emoji"))
    return {
        "chat": args.chat(),
        "message_id": args.required_int("message_id"),
        "emoji": args.required_text("emoji", max_len=EMOJI_LIMIT),
    }


def _one_message_validator(tool: str) -> Any:
    def validate(raw: object) -> dict[str, Any]:
        args = Args(tool, raw, ("chat", "message_id"))
        return {"chat": args.chat(), "message_id": args.required_int("message_id")}

    return validate


def _one_message_describer(tool: str, action: str) -> Any:
    async def describe(gateway: TelegramGateway, args: dict[str, Any]) -> PromptFields:
        chat = await gateway.resolve_chat(args["chat"])
        messages = await named_messages(gateway, chat.id, [args["message_id"]])
        return PromptFields(
            action=action,
            tool=tool,
            chat=chat,
            messages=messages,
            details=HeadlineDetails(message_id=args["message_id"], emoji=args.get("emoji")),
        )

    return describe


async def _run_react(
    gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
) -> Any:
    chat, number = approved_chat(approval), approved_message_id(approval)
    emoji = approved(approval).details.emoji
    if emoji is None:
        raise ValueError("the approval names no emoji")
    await gateway.react(chat.id, number, emoji)
    return {"chat_id": chat.id, "message_id": number, "emoji": emoji}


async def _run_pin(
    gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
) -> Any:
    chat, number = approved_chat(approval), approved_message_id(approval)
    await gateway.pin(chat.id, number)
    return {"chat_id": chat.id, "message_id": number, "pinned": True}


async def _run_unpin(
    gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
) -> Any:
    chat, number = approved_chat(approval), approved_message_id(approval)
    await gateway.unpin(chat.id, number)
    return {"chat_id": chat.id, "message_id": number, "pinned": False}


# tg_messages_read (BR22.3, BR24) -------------------------------------------------------------


def _validate_read(raw: object) -> dict[str, Any]:
    args = Args(MESSAGES_READ, raw, ("chat", "limit", "before_id", "topic_id", "mark_read"))
    low, high, default = MESSAGES_READ_LIMIT
    return {
        "chat": args.chat(),
        "limit": args.integer("limit", default=default, minimum=low, maximum=high),
        "before_id": args.integer("before_id", minimum=1),
        "topic_id": args.integer("topic_id", minimum=1),
        "mark_read": args.boolean("mark_read", False),
    }


async def _run_read(
    gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
) -> Any:
    found = await gateway.read_messages(
        args["chat"], args["limit"], before_id=args["before_id"], topic_id=args["topic_id"]
    )
    oldest = min((message.id for message in found), default=None)
    return with_notice(
        {
            "chat_id": args["chat"],
            "topic_id": args["topic_id"],
            "messages": [message_dict(message) for message in found],
            "next_before_id": oldest if len(found) >= args["limit"] else None,
        },
        message_fields("messages[]"),
    )


def newest_returned(data: Any) -> int | None:
    """The highest message id a read returned, so a mark never goes past it (BR24.1)."""
    ids = [message["id"] for message in data.get("messages", [])]
    return max(ids) if ids else None


def read_action() -> ConsentAction:
    return ConsentAction(
        DECLS[MESSAGES_READ], _validate_read, run=_run_read, newest_returned=newest_returned
    )


# tg_messages_pinned, tg_messages_search (BR22.3) ---------------------------------------------

PINNED_LIMIT = 50


def _validate_pinned(raw: object) -> dict[str, Any]:
    return {"chat": Args(MESSAGES_PINNED, raw, ("chat",)).chat()}


async def _run_pinned(
    gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
) -> Any:
    found = await gateway.read_pinned(args["chat"], PINNED_LIMIT)
    return with_notice(
        {"chat_id": args["chat"], "messages": [message_dict(m) for m in found]},
        message_fields("messages[]"),
    )


def _validate_search(raw: object) -> dict[str, Any]:
    args = Args(MESSAGES_SEARCH, raw, ("query", "chat", "limit"))
    low, high, default = SEARCH_LIMIT
    return {
        "query": args.required_text("query", max_len=QUERY_LIMIT),
        "chat": args.chat() if args.given("chat") else None,
        "limit": args.integer("limit", default=default, minimum=low, maximum=high),
    }


async def _run_search(
    gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
) -> Any:
    if args["chat"] is not None:
        chat = await gateway.resolve_chat(args["chat"])
        found = await gateway.search_chat(chat.id, args["query"], args["limit"])
        hits = [SearchHit(chat=chat, message=message) for message in found]
    else:
        hits = await gateway.search_global(args["query"], args["limit"])
    shaped = [{"chat": chat_dict(hit.chat), "message": message_dict(hit.message)} for hit in hits]
    return with_notice(
        {"query": args["query"], "hits": shaped},
        ["hits[].chat.name", *message_fields("hits[].message")],
    )


def read_actions() -> list[ConsentAction]:
    return [
        ConsentAction(DECLS[MESSAGES_PINNED], _validate_pinned, run=_run_pinned),
        ConsentAction(DECLS[MESSAGES_SEARCH], _validate_search, run=_run_search),
    ]


def write_actions(context: CatalogContext) -> list[ConsentAction]:
    return [
        ConsentAction(DECLS[MESSAGES_EDIT], _validate_edit, _describe_edit, _run_edit),
        ConsentAction(DECLS[MESSAGES_DELETE], _validate_delete, _describe_delete, _run_delete),
        ConsentAction(DECLS[MESSAGES_FORWARD], _validate_forward, _describe_forward, _run_forward),
        ConsentAction(
            DECLS[MESSAGES_REACT],
            _validate_react,
            _one_message_describer(MESSAGES_REACT, "react"),
            _run_react,
        ),
        ConsentAction(
            DECLS[MESSAGES_PIN],
            _one_message_validator(MESSAGES_PIN),
            _one_message_describer(MESSAGES_PIN, "pin"),
            _run_pin,
        ),
        ConsentAction(
            DECLS[MESSAGES_UNPIN],
            _one_message_validator(MESSAGES_UNPIN),
            _one_message_describer(MESSAGES_UNPIN, "pin"),
            _run_unpin,
        ),
    ]
