"""Test-only stand-in actions: one per write kind (pin and unpin apart), the mark tool and the
read tool with its mark_read option. They carry the tool names of the full catalog so that the
kind table's per-tool questions apply, but they are registered only in tests, never in the
shipped catalog; the real tools come later. Each describer fills PromptFields from the fake
gateway; each run records itself on the fake."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from telegram_mcp.actions import ActionRegistry, ConsentAction, default_registry
from telegram_mcp.consent import ActionDecl, ActionKind, PendingApproval
from telegram_mcp.gateway.fake import FakeGateway, FakeMessage
from telegram_mcp.prompts import (
    FieldChange,
    FileFields,
    HeadlineDetails,
    MessageExcerpt,
    Person,
    PromptFields,
    check_message_ids,
    normalize_rights,
)
from telegram_mcp.tools import InvalidArguments, parse_chat_id

MARK_TOOL = "tg_chats_mark_read"
READ_TOOL = "tg_messages_read"

# tool -> (write kind, {argument: (type, required)})
_INT, _STR, _BOOL, _IDS, _MAP = int, str, bool, list, dict
SCHEMAS: dict[str, tuple[str, dict[str, tuple[type, bool]]]] = {
    "tg_files_send": ("file_send", {"chat": (_INT, True), "path": (_STR, True)}),
    "tg_messages_edit": (
        "edit",
        {"chat": (_INT, True), "message_id": (_INT, True), "text": (_STR, True)},
    ),
    "tg_messages_delete": (
        "delete",
        {"chat": (_INT, True), "ids": (_IDS, True), "for_everyone": (_BOOL, False)},
    ),
    "tg_messages_forward": (
        "forward",
        {"chat": (_INT, True), "from_chat": (_INT, True), "ids": (_IDS, True)},
    ),
    "tg_messages_react": (
        "react",
        {"chat": (_INT, True), "message_id": (_INT, True), "emoji": (_STR, True)},
    ),
    "tg_messages_pin": ("pin", {"chat": (_INT, True), "message_id": (_INT, True)}),
    "tg_messages_unpin": ("pin", {"chat": (_INT, True), "message_id": (_INT, True)}),
    "tg_topics_create": ("topic_create", {"chat": (_INT, True), "title": (_STR, True)}),
    "tg_chats_create": (
        "chat_create",
        {"title": (_STR, True), "type": (_STR, True), "members": (_IDS, False)},
    ),
    "tg_chats_join": ("join", {"link": (_STR, True)}),
    "tg_chats_leave": ("leave", {"chat": (_INT, True)}),
    "tg_chats_edit": ("chat_edit", {"chat": (_INT, True), "title": (_STR, True)}),
    "tg_invites_create": (
        "invite",
        {"chat": (_INT, True), "expire_seconds": (_INT, False), "usage_limit": (_INT, False)},
    ),
    "tg_admins_set": (
        "admin",
        {"chat": (_INT, True), "user": (_INT, True), "rights": (_MAP, True)},
    ),
    "tg_profile_edit": ("profile_edit", {"first_name": (_STR, True)}),
}

# A call per stand-in that its describer can build a question for, given seeded_fake().
SAMPLE_ARGS: dict[str, dict[str, Any]] = {
    "tg_messages_send": {"chat": 101, "text": "hello"},
    "tg_files_send": {"chat": 101, "path": "/Users/p/outbox/report.pdf"},
    "tg_messages_edit": {"chat": 101, "message_id": 1, "text": "fixed"},
    "tg_messages_delete": {"chat": 101, "ids": [1, 2]},
    "tg_messages_forward": {"chat": -202, "from_chat": 101, "ids": [2]},
    "tg_messages_react": {"chat": 101, "message_id": 1, "emoji": "👍"},
    "tg_messages_pin": {"chat": 101, "message_id": 1},
    "tg_messages_unpin": {"chat": 101, "message_id": 1},
    "tg_topics_create": {"chat": -1000000000505, "title": "Ideas"},
    "tg_chats_create": {"title": "Trip", "type": "group", "members": [101]},
    "tg_chats_join": {"link": "https://t.me/+AbCdEf"},
    "tg_chats_leave": {"chat": -202},
    "tg_chats_edit": {"chat": -202, "title": "Family 2026"},
    "tg_invites_create": {"chat": -202, "usage_limit": 3},
    "tg_admins_set": {"chat": -202, "user": 101, "rights": {"pin_messages": True}},
    "tg_profile_edit": {"first_name": "Maks"},
}


def validator(tool: str, schema: Mapping[str, tuple[type, bool]]) -> Any:
    def validate(raw: object) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise InvalidArguments("arguments must be an object")
        for name in raw:
            if name not in schema:
                raise InvalidArguments(f"{name} is not an argument of {tool}")
        args: dict[str, Any] = {}
        for name, (kind, required) in schema.items():
            value = raw.get(name)
            if value is None:
                if required:
                    raise InvalidArguments(f"{name} is required")
                args[name] = None
                continue
            if name in ("chat", "from_chat"):
                args[name] = parse_chat_id(value)
                continue
            if (isinstance(value, bool) and kind is not bool) or not isinstance(value, kind):
                raise InvalidArguments(f"{name} has the wrong type")
            args[name] = list(value) if isinstance(value, list) else value
        if tool == "tg_admins_set":
            try:
                args["rights"] = normalize_rights(args["rights"])
            except Exception as error:
                raise InvalidArguments(str(error)) from error
        if tool == "tg_messages_delete" and args["for_everyone"] is None:
            args["for_everyone"] = True
        return args

    return validate


def _excerpt(message: FakeMessage) -> MessageExcerpt:
    return MessageExcerpt(
        id=message.id,
        sender=message.sender,
        date=message.date,
        text=message.text,
        media=message.media,
    )


async def _messages(gateway: Any, chat: int, ids: Sequence[int]) -> tuple[MessageExcerpt, ...]:
    found = await gateway.get_messages(chat, list(ids))
    return check_message_ids(list(ids), [_excerpt(m) for m in found])


async def describe(tool: str, gateway: Any, args: dict[str, Any]) -> PromptFields:
    kind = SCHEMAS[tool][0]
    chat = await gateway.resolve_chat(args["chat"]) if args.get("chat") is not None else None
    base: dict[str, Any] = {"action": kind, "tool": tool, "chat": chat}
    if kind == "file_send":
        path = args["path"]
        return PromptFields(**base, file=FileFields(path, path.rsplit("/", 1)[-1], 2048))
    if kind == "edit":
        (message,) = await _messages(gateway, args["chat"], [args["message_id"]])
        return PromptFields(
            **base,
            content=args["text"],
            previous=message.text,
            details=HeadlineDetails(message_id=args["message_id"]),
        )
    if kind == "delete":
        messages = await _messages(gateway, args["chat"], args["ids"])
        details = HeadlineDetails(for_everyone=args["for_everyone"])
        return PromptFields(**base, messages=messages, details=details)
    if kind == "forward":
        source = await gateway.resolve_chat(args["from_chat"])
        messages = await _messages(gateway, args["from_chat"], args["ids"])
        return PromptFields(**base, source_chat=source, messages=messages)
    if kind in ("react", "pin"):
        messages = await _messages(gateway, args["chat"], [args["message_id"]])
        details = HeadlineDetails(message_id=args["message_id"], emoji=args.get("emoji"))
        return PromptFields(**base, messages=messages, details=details)
    if kind == "topic_create":
        return PromptFields(**base, details=HeadlineDetails(title=args["title"]))
    if kind == "chat_create":
        members = []
        for member in args["members"] or []:
            resolved = await gateway.resolve_chat(member)
            members.append(Person(resolved.id, resolved.name))
        details = HeadlineDetails(
            new_chat_type=args["type"], title=args["title"], members=tuple(members)
        )
        return PromptFields(**base, details=details)
    if kind == "join":
        return PromptFields(**base, details=HeadlineDetails(link=args["link"]))
    if kind == "chat_edit":
        assert chat is not None
        change = FieldChange("title", chat.name, args["title"])
        return PromptFields(**base, details=HeadlineDetails(changes=(change,)))
    if kind == "invite":
        details = HeadlineDetails(
            expire_seconds=args["expire_seconds"], usage_limit=args["usage_limit"]
        )
        return PromptFields(**base, details=details)
    if kind == "admin":
        user = await gateway.resolve_chat(args["user"])
        details = HeadlineDetails(
            user=Person(user.id, user.name), rights=args["rights"], current_rights={}
        )
        return PromptFields(**base, details=details)
    if kind == "profile_edit":
        change = FieldChange("first_name", "Test", args["first_name"])
        return PromptFields(**base, details=HeadlineDetails(changes=(change,)))
    return PromptFields(**base)  # leave


def write_action(tool: str) -> ConsentAction:
    kind, schema = SCHEMAS[tool]

    async def describer(gateway: Any, args: dict[str, Any]) -> PromptFields:
        return await describe(tool, gateway, args)

    async def run(gateway: Any, approval: PendingApproval | None, args: dict[str, Any]) -> Any:
        assert approval is not None
        await gateway.record_write(tool, approval_id=approval.id, args=args)
        if kind == "invite":
            return {"link": "https://t.me/+SecretPart123"}
        return {"done": tool}

    return ConsentAction(
        decl=ActionDecl(tool=tool, kind=ActionKind.WRITE, write_kind=kind),
        validate=validator(tool, schema),
        describe=describer,
        run=run,
    )


def mark_action() -> ConsentAction:
    return ConsentAction(
        decl=ActionDecl(tool=MARK_TOOL, kind=ActionKind.MARK),
        validate=validator(MARK_TOOL, {"chat": (_INT, True), "topic_id": (_INT, False)}),
    )


def read_action() -> ConsentAction:
    schema = {
        "chat": (_INT, True),
        "limit": (_INT, False),
        "before_id": (_INT, False),
        "topic_id": (_INT, False),
        "mark_read": (_BOOL, False),
    }
    base_validate = validator(READ_TOOL, schema)

    def validate(raw: object) -> dict[str, Any]:
        args: dict[str, Any] = base_validate(raw)
        args["limit"] = args["limit"] or 20
        args["mark_read"] = bool(args["mark_read"])
        return args

    async def run(gateway: Any, approval: PendingApproval | None, args: dict[str, Any]) -> Any:
        found = await gateway.read_messages(
            args["chat"], args["limit"], args["topic_id"], args["before_id"]
        )
        return {"messages": [{"id": m.id, "text": m.text} for m in found]}

    def newest(data: Any) -> int | None:
        ids = [m["id"] for m in data["messages"]]
        return max(ids) if ids else None

    return ConsentAction(
        decl=ActionDecl(tool=READ_TOOL, kind=ActionKind.READ, marks_read_option=True),
        validate=validate,
        run=run,
        newest_returned=newest,
    )


def failing_action(tool: str, error: Exception | None) -> ConsentAction:
    """A leave whose describer raises ``error``, or hangs when it is None."""

    async def describer(gateway: Any, args: dict[str, Any]) -> PromptFields:
        if error is None:
            await asyncio.Event().wait()
        assert error is not None
        raise error

    action = write_action("tg_chats_leave")
    return ConsentAction(
        decl=ActionDecl(tool=tool, kind=ActionKind.WRITE, write_kind="leave"),
        validate=action.validate,
        describe=describer,
        run=action.run,
    )


def rowless_action(tool: str = "tg_no_row") -> ConsentAction:
    """A catalog tool whose declaration has no row in the kind table."""
    action = write_action("tg_chats_leave")
    return ConsentAction(
        decl=ActionDecl(tool=tool, kind=ActionKind.WRITE, write_kind=None),
        validate=action.validate,
        describe=action.describe,
        run=action.run,
    )


def stand_in_registry(*extra: ConsentAction) -> ActionRegistry:
    registry = default_registry()
    for tool in SCHEMAS:
        registry.register(write_action(tool))
    registry.register(mark_action())
    registry.register(read_action())
    for action in extra:
        registry.register(action)
    return registry


def seeded_fake(fake: FakeGateway) -> FakeGateway:
    """Two messages in Alice's chat and a forum with one topic, as SAMPLE_ARGS expects."""
    fake.add_message(101, "the first message", sender="Alice")
    fake.add_message(101, "the second message", sender="Alice", media="photo")
    fake.topics[-1000000000505] = {9: "Ideas"}
    return fake
