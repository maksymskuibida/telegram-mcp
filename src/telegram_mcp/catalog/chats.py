"""Chat tools: creating topics and chats, joining, leaving, editing chats and invite links
(BR23.6 to BR23.8), and the reads of folders, chat lookup, chat details and topics (BR21, BR22).

Chats are named by id; ``tg_chats_resolve`` is the one place a title or username becomes an
id, and it never guesses (BR21.2, BR21.3)."""

from __future__ import annotations

import re
from typing import Any

from telegram_mcp.actions import ConsentAction
from telegram_mcp.catalog.common import Args, CatalogContext, chat_dict, require_group, with_notice
from telegram_mcp.catalog.messages import approved, approved_chat
from telegram_mcp.consent import PendingApproval, canonical_digest
from telegram_mcp.files import NO_RECORD_MESSAGE, FileRefused, admit, open_admitted
from telegram_mcp.gateway.models import ChatNotFound
from telegram_mcp.gateway.protocol import TelegramGateway
from telegram_mcp.prompts import FieldChange, HeadlineDetails, Person, PromptError, PromptFields
from telegram_mcp.tools import (
    ABOUT_LIMIT,
    CHATS_CREATE,
    CHATS_EDIT,
    CHATS_JOIN,
    CHATS_LEAVE,
    DECLS,
    INVITES_CREATE,
    InvalidArguments,
    MAX_EXPIRE_SECONDS,
    MAX_USAGE_LIMIT,
    QUERY_LIMIT,
    TITLE_LIMIT,
    TOPICS_CREATE,
)

_LINK_HOST = r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/"
INVITE_LINK = re.compile(rf"^{_LINK_HOST}(?:\+|joinchat/)([A-Za-z0-9_-]{{1,64}})/?$")
USERNAME_LINK = re.compile(rf"^{_LINK_HOST}([A-Za-z][A-Za-z0-9_]{{3,31}})/?$")
USERNAME = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")
JOIN_FORMS = (
    "link_or_username must be @name, name, t.me/name, t.me/+hash or t.me/joinchat/hash"
)


def parse_join(value: str) -> tuple[str | None, str | None]:
    """(username, invite hash) of a join target (BR23.7); anything else is refused."""
    text = value.strip()
    for pattern, is_invite in ((INVITE_LINK, True), (USERNAME_LINK, False), (USERNAME, False)):
        match = pattern.match(text)
        if match is not None:
            return (None, match[1]) if is_invite else (match[1], None)
    raise PromptError("invalid_arguments", JOIN_FORMS)


# tg_topics_create ----------------------------------------------------------------------------


def _validate_topic(raw: object) -> dict[str, Any]:
    args = Args(TOPICS_CREATE, raw, ("chat", "title"))
    return {"chat": args.chat(), "title": args.required_text("title", max_len=TITLE_LIMIT)}


async def _describe_topic(gateway: TelegramGateway, args: dict[str, Any]) -> PromptFields:
    chat = await gateway.resolve_chat(args["chat"])
    if chat.type != "forum":
        raise PromptError("invalid_arguments", "This chat is not a forum, so it has no topics.")
    return PromptFields(
        action="topic_create",
        tool=TOPICS_CREATE,
        chat=chat,
        details=HeadlineDetails(title=args["title"]),
    )


async def _run_topic(
    gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
) -> Any:
    chat, title = approved_chat(approval), approved(approval).details.title
    if title is None:
        raise ValueError("the approval names no title")
    topic_id = await gateway.topic_create(chat.id, title)
    return {"chat_id": chat.id, "topic_id": topic_id, "title": title}


# tg_chats_create -----------------------------------------------------------------------------


def _validate_create(raw: object) -> dict[str, Any]:
    args = Args(CHATS_CREATE, raw, ("type", "title", "members"))
    kind = args.choice("type", ("group", "channel"))
    members = args.ids("members", minimum=0)
    if kind == "channel" and members:
        raise InvalidArguments(
            "members: a channel is created without members; invite people with "
            "tg_invites_create"
        )
    return {
        "type": kind,
        "title": args.required_text("title", max_len=TITLE_LIMIT),
        "members": members,
    }


async def _describe_create(gateway: TelegramGateway, args: dict[str, Any]) -> PromptFields:
    members: tuple[Person, ...] = ()
    if args["members"]:
        own = await gateway.own_profile()
        if own.id in args["members"]:
            raise PromptError(
                "invalid_arguments", "Do not list yourself: you are in the new chat anyway."
            )
        people = await gateway.user_profile(list(args["members"]))  # one batched call
        members = tuple(Person(person.id, person.name) for person in people)
    return PromptFields(
        action="chat_create",
        tool=CHATS_CREATE,
        details=HeadlineDetails(new_chat_type=args["type"], title=args["title"], members=members),
    )


async def _run_create(
    gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
) -> Any:
    details = approved(approval).details
    title, kind = details.title, details.new_chat_type
    if title is None or kind is None:
        raise ValueError("the approval names no title or type")
    if kind == "channel":
        chat_id = await gateway.create_channel(title)
        return {"chat_id": chat_id, "type": "channel", "title": title}
    members = [person.id for person in details.members or ()]
    chat_id = await gateway.create_group(title, members, supergroup=True)
    return {"chat_id": chat_id, "type": "supergroup", "title": title}


# tg_chats_join (BR23.7) ----------------------------------------------------------------------


def _validate_join(raw: object) -> dict[str, Any]:
    args = Args(CHATS_JOIN, raw, ("link_or_username",))
    value = args.required_text("link_or_username", max_len=QUERY_LIMIT)
    try:
        parse_join(value)
    except PromptError as error:
        raise InvalidArguments(error.message) from error
    return {"link_or_username": value}


async def _describe_join(gateway: TelegramGateway, args: dict[str, Any]) -> PromptFields:
    username, invite = parse_join(args["link_or_username"])
    if invite is not None:
        link = f"https://t.me/+{invite}"
        return PromptFields(action="join", tool=CHATS_JOIN, details=HeadlineDetails(link=link))
    assert username is not None  # noqa: S101 (one of the two is set)
    try:
        found = await gateway.chat_info(username)
    except ChatNotFound as error:
        raise PromptError("not_found", f"No public chat has the username @{username}.") from error
    if found.type == "user":
        raise PromptError(
            "invalid_arguments", "That username is a person, not a chat; use tg_users_info."
        )
    if found.my_role in ("member", "admin", "creator"):
        raise PromptError("invalid_arguments", "You are already a member of this chat.")
    return PromptFields(action="join", tool=CHATS_JOIN, chat=found.ref())


async def _run_join(
    gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
) -> Any:
    approved(approval)
    username, invite = parse_join(args["link_or_username"])  # the digest-matched argument
    chat = await gateway.join(username, invite)
    return with_notice({"chat": chat_dict(chat)}, ["chat.name"])


# tg_chats_leave ------------------------------------------------------------------------------


def _validate_chat_only(tool: str) -> Any:
    def validate(raw: object) -> dict[str, Any]:
        return {"chat": Args(tool, raw, ("chat",)).chat()}

    return validate


async def _describe_leave(gateway: TelegramGateway, args: dict[str, Any]) -> PromptFields:
    chat = await gateway.resolve_chat(args["chat"])
    require_group(chat)
    return PromptFields(action="leave", tool=CHATS_LEAVE, chat=chat)


async def _run_leave(
    gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
) -> Any:
    chat = approved_chat(approval)
    await gateway.leave(chat.id)
    return {"chat_id": chat.id, "left": True}


# tg_chats_edit (BR23.6, BR25) ------------------------------------------------------------------


def _validate_edit(raw: object) -> dict[str, Any]:
    args = Args(CHATS_EDIT, raw, ("chat", "title", "description", "photo_path"))
    edited = {
        "chat": args.chat(),
        "title": args.text("title", min_len=1, max_len=TITLE_LIMIT),
        "description": args.text("description", max_len=ABOUT_LIMIT),
        "photo_path": args.text("photo_path", min_len=1, max_len=4096),
    }
    if all(edited[name] is None for name in ("title", "description", "photo_path")):
        raise InvalidArguments("nothing to change: give title, description or photo_path")
    return edited


def chat_edit_actions(context: CatalogContext) -> ConsentAction:
    async def describe(gateway: TelegramGateway, args: dict[str, Any]) -> PromptFields:
        found = await gateway.chat_info(args["chat"])
        chat = found.ref()
        require_group(chat)
        changes = []
        if args["title"] is not None:
            changes.append(FieldChange("title", found.name, args["title"]))
        if args["description"] is not None:
            changes.append(FieldChange("description", found.description, args["description"]))
        file = None
        if args["photo_path"] is not None:
            admitted = admit(args["photo_path"], context.paths)
            context.files.put(canonical_digest(CHATS_EDIT, args), admitted)
            file = admitted.fields()
        return PromptFields(
            action="chat_edit",
            tool=CHATS_EDIT,
            chat=chat,
            file=file,
            details=HeadlineDetails(changes=tuple(changes)),
        )

    async def run(
        gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
    ) -> Any:
        fields, chat = approved(approval), approved_chat(approval)
        values = {change.field: change.after for change in fields.details.changes}
        changed = list(values)
        if fields.file is None:
            await gateway.edit_chat(
                chat.id, values.get("title"), values.get("description"), None, None
            )
            return {"chat_id": chat.id, "changed": changed}
        assert approval is not None  # noqa: S101 (approved() checked it)
        record = context.files.take(approval.args_digest)
        if record is None:
            raise FileRefused("not_allowed", NO_RECORD_MESSAGE)
        with open_admitted(record, context.paths) as photo:
            await gateway.edit_chat(
                chat.id, values.get("title"), values.get("description"), photo, record.name
            )
        return {"chat_id": chat.id, "changed": [*changed, "photo"]}

    return ConsentAction(DECLS[CHATS_EDIT], _validate_edit, describe, run)


# tg_invites_create (BR23.8) --------------------------------------------------------------------


def _validate_invite(raw: object) -> dict[str, Any]:
    args = Args(INVITES_CREATE, raw, ("chat", "expire_seconds", "usage_limit"))
    return {
        "chat": args.chat(),
        "expire_seconds": args.integer("expire_seconds", minimum=60, maximum=MAX_EXPIRE_SECONDS),
        "usage_limit": args.integer("usage_limit", minimum=1, maximum=MAX_USAGE_LIMIT),
    }


async def _describe_invite(gateway: TelegramGateway, args: dict[str, Any]) -> PromptFields:
    chat = await gateway.resolve_chat(args["chat"])
    require_group(chat)
    return PromptFields(
        action="invite",
        tool=INVITES_CREATE,
        chat=chat,
        details=HeadlineDetails(
            expire_seconds=args["expire_seconds"], usage_limit=args["usage_limit"]
        ),
    )


async def _run_invite(
    gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
) -> Any:
    fields, chat = approved(approval), approved_chat(approval)
    expire, limit = fields.details.expire_seconds, fields.details.usage_limit
    link = await gateway.invite_link(chat.id, expire, limit)
    return {"chat_id": chat.id, "link": link, "expire_seconds": expire, "usage_limit": limit}


def write_actions(context: CatalogContext) -> list[ConsentAction]:
    return [
        ConsentAction(DECLS[TOPICS_CREATE], _validate_topic, _describe_topic, _run_topic),
        ConsentAction(DECLS[CHATS_CREATE], _validate_create, _describe_create, _run_create),
        ConsentAction(DECLS[CHATS_JOIN], _validate_join, _describe_join, _run_join),
        ConsentAction(
            DECLS[CHATS_LEAVE], _validate_chat_only(CHATS_LEAVE), _describe_leave, _run_leave
        ),
        chat_edit_actions(context),
        ConsentAction(DECLS[INVITES_CREATE], _validate_invite, _describe_invite, _run_invite),
    ]
