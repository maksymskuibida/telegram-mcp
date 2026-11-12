"""People tools: setting admin rights (BR23.6), and reading members, contacts and user
profiles (BR21.5, BR22)."""

from __future__ import annotations

import re
from typing import Any

from telegram_mcp.actions import ConsentAction
from telegram_mcp.catalog.common import Args, CatalogContext, person_dict, with_notice
from telegram_mcp.catalog.messages import approved, approved_chat
from telegram_mcp.consent import PendingApproval
from telegram_mcp.gateway.protocol import TelegramGateway
from telegram_mcp.prompts import (
    HeadlineDetails,
    Person,
    PromptError,
    PromptFields,
    normalize_rights,
)
from telegram_mcp.tools import (
    ADMINS_SET,
    CONTACTS_LIST,
    DECLS,
    MEMBERS_LIMIT,
    MEMBERS_LIST,
    USERS_INFO,
    InvalidArguments,
)

ADMIN_CHATS = ("supergroup", "channel", "forum")


def _validate_admin(raw: object) -> dict[str, Any]:
    args = Args(ADMINS_SET, raw, ("chat", "user", "rights"))
    try:
        rights = normalize_rights(args.raw.get("rights"))
    except PromptError as error:
        raise InvalidArguments(error.message) from error
    return {"chat": args.chat(), "user": args.required_int("user"), "rights": rights}


async def _describe_admin(gateway: TelegramGateway, args: dict[str, Any]) -> PromptFields:
    chat = await gateway.resolve_chat(args["chat"])
    if chat.saved_messages or chat.type not in ADMIN_CHATS:
        raise PromptError(
            "invalid_arguments", "Admin rights can be set only in a supergroup or a channel."
        )
    (person,) = await gateway.user_profile([args["user"]])
    current = await gateway.admins(chat.id, person.id)
    return PromptFields(
        action="admin",
        tool=ADMINS_SET,
        chat=chat,
        details=HeadlineDetails(
            user=Person(person.id, person.name),
            rights=args["rights"],
            current_rights=normalize_rights(current),
        ),
    )


async def _run_admin(
    gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
) -> Any:
    details, chat = approved(approval).details, approved_chat(approval)
    if details.user is None or details.rights is None:
        raise ValueError("the approval names no user or rights")
    rights = normalize_rights(details.rights)
    await gateway.set_admin_rights(chat.id, details.user.id, rights)
    return {"chat_id": chat.id, "user_id": details.user.id, "rights": rights}


# Reads: members, contacts, users (BR21.5, BR22.3, BR22.4) --------------------------------------

CONTACTS_CAP = 10_000
_USER_LINK = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{2,31})/?$"
)
_USERNAME = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{2,31})$")


def _validate_members(raw: object) -> dict[str, Any]:
    args = Args(MEMBERS_LIST, raw, ("chat", "filter", "limit"))
    low, high, default = MEMBERS_LIMIT
    return {
        "chat": args.chat(),
        "filter": args.choice("filter", ("all", "admins", "bots"), "all"),
        "limit": args.integer("limit", default=default, minimum=low, maximum=high),
    }


async def _run_members(
    gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
) -> Any:
    found = await gateway.members(args["chat"], args["filter"], args["limit"])
    return with_notice(
        {"chat_id": args["chat"], "members": [person_dict(p) for p in found[: args["limit"]]]},
        ["members[].name"],
    )


def _validate_contacts(raw: object) -> dict[str, Any]:
    Args(CONTACTS_LIST, raw, ())
    return {}


async def _run_contacts(
    gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
) -> Any:
    found = await gateway.contacts()
    return with_notice(
        {
            "contacts": [person_dict(p) for p in found[:CONTACTS_CAP]],
            "left_out": max(0, len(found) - CONTACTS_CAP),
        },
        ["contacts[].name"],
    )


def parse_user(value: object) -> int | str:
    """A user by id, or a public username from @name, name or a t.me link (BR21.5)."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
        for pattern in (_USER_LINK, _USERNAME):
            match = pattern.match(text)
            if match is not None:
                return match[1].lower()
    raise InvalidArguments("user must be a user id, an @username or a t.me/<username> link")


def _validate_user(raw: object) -> dict[str, Any]:
    args = Args(USERS_INFO, raw, ("user",))
    return {"user": parse_user(args.raw.get("user"))}


async def _run_user(
    gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
) -> Any:
    wanted = args["user"]
    found = await gateway.user_profile(wanted if isinstance(wanted, str) else [wanted])
    return with_notice({"user": person_dict(found[0])}, ["user.name", "user.bio"])


def read_actions() -> list[ConsentAction]:
    return [
        ConsentAction(DECLS[MEMBERS_LIST], _validate_members, run=_run_members),
        ConsentAction(DECLS[CONTACTS_LIST], _validate_contacts, run=_run_contacts),
        ConsentAction(DECLS[USERS_INFO], _validate_user, run=_run_user),
    ]


def write_actions(context: CatalogContext) -> list[ConsentAction]:
    return [ConsentAction(DECLS[ADMINS_SET], _validate_admin, _describe_admin, _run_admin)]
