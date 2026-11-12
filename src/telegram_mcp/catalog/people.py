"""People tools: setting admin rights (BR23.6), and reading members, contacts and user
profiles (BR21.5, BR22)."""

from __future__ import annotations

from typing import Any

from telegram_mcp.actions import ConsentAction
from telegram_mcp.catalog.common import Args, CatalogContext
from telegram_mcp.catalog.messages import approved, approved_chat
from telegram_mcp.consent import PendingApproval
from telegram_mcp.gateway.protocol import TelegramGateway
from telegram_mcp.prompts import HeadlineDetails, Person, PromptError, PromptFields, normalize_rights
from telegram_mcp.tools import ADMINS_SET, DECLS, InvalidArguments

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


def write_actions(context: CatalogContext) -> list[ConsentAction]:
    return [ConsentAction(DECLS[ADMINS_SET], _validate_admin, _describe_admin, _run_admin)]
