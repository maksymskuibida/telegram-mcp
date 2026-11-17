"""The person's own profile: changing it (BR23.5, BR25) and reading it (BR22.4)."""

from __future__ import annotations

import re
from typing import Any

from telegram_mcp.actions import ConsentAction
from telegram_mcp.catalog.common import Args, CatalogContext, with_notice
from telegram_mcp.catalog.media import start_upload, starts_with_photo
from telegram_mcp.catalog.messages import approved
from telegram_mcp.consent import PendingApproval, canonical_digest
from telegram_mcp.files import admit_photo
from telegram_mcp.gateway.models import UploadedFile
from telegram_mcp.gateway.protocol import TelegramGateway
from telegram_mcp.prompts import FieldChange, HeadlineDetails, PromptFields
from telegram_mcp.tools import ABOUT_LIMIT, DECLS, PROFILE_EDIT, PROFILE_GET, InvalidArguments

FIELDS = ("first_name", "last_name", "bio", "username")
USERNAME = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def _validate(raw: object) -> dict[str, Any]:
    args = Args(PROFILE_EDIT, raw, (*FIELDS, "photo_path"))
    edited = {
        "first_name": args.text("first_name", min_len=1, max_len=64),
        "last_name": args.text("last_name", max_len=64),
        "bio": args.text("bio", max_len=ABOUT_LIMIT),
        "username": args.text("username", max_len=32),
        "photo_path": args.text("photo_path", min_len=1, max_len=4096),
    }
    username = edited["username"]
    if username and not USERNAME.match(username):
        raise InvalidArguments(
            "username must be 5 to 32 letters, digits or underscores, or empty to remove it"
        )
    if all(value is None for value in edited.values()):
        raise InvalidArguments(
            "nothing to change: give first_name, last_name, bio, username or photo_path"
        )
    return edited


def profile_edit_action(context: CatalogContext) -> ConsentAction:
    async def describe(gateway: TelegramGateway, args: dict[str, Any]) -> PromptFields:
        own = await gateway.own_profile()
        changes = tuple(
            FieldChange(name, getattr(own, name), args[name])
            for name in FIELDS
            if args[name] is not None
        )
        file = None
        if args["photo_path"] is not None:
            admitted = admit_photo(args["photo_path"], context.paths)
            context.files.put(canonical_digest(PROFILE_EDIT, args), admitted)
            file = admitted.fields()
        return PromptFields(
            action="profile_edit",
            tool=PROFILE_EDIT,
            file=file,
            details=HeadlineDetails(changes=changes),
        )

    async def run(
        gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
    ) -> Any:
        """BR28.11: names, bio and username change at once; the photo is a transfer, which is
        not created when changing a field fails."""
        fields = approved(approval)
        values = {change.field: change.after for change in fields.details.changes}
        if values:
            await gateway.edit_own_profile(
                values.get("first_name"),
                values.get("last_name"),
                values.get("bio"),
                values.get("username"),
            )
        result: dict[str, Any] = {"changed": list(values)}
        if fields.file is None:
            return result
        assert approval is not None  # noqa: S101 (approved() checked it)
        transfer = start_upload(
            context,
            approval,
            tool=PROFILE_EDIT,
            chat_id=None,
            final=lambda record: _set_profile_photo,
            final_request="profile_photo",
        )
        return {**result, "photo_transfer_id": transfer.transfer_id}

    return ConsentAction(
        DECLS[PROFILE_EDIT], _validate, describe, run, starts_transfer=starts_with_photo
    )


async def _set_profile_photo(gateway: TelegramGateway, uploaded: UploadedFile) -> dict[str, Any]:
    """The final request of a profile photo (BR28.5)."""
    await gateway.set_profile_photo(uploaded)
    return {"changed": ["photo"]}


def _validate_get(raw: object) -> dict[str, Any]:
    Args(PROFILE_GET, raw, ())
    return {}


async def _run_get(
    gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
) -> Any:
    own = await gateway.own_profile()
    profile = {
        "id": own.id,
        "first_name": own.first_name,
        "last_name": own.last_name,
        "username": own.username,
        "bio": own.bio,
        "phone": own.phone,
        "has_photo": own.has_photo,
    }
    return with_notice(
        {"profile": profile}, ["profile.first_name", "profile.last_name", "profile.bio"]
    )


def read_actions() -> list[ConsentAction]:
    return [ConsentAction(DECLS[PROFILE_GET], _validate_get, run=_run_get)]


def write_actions(context: CatalogContext) -> list[ConsentAction]:
    return [profile_edit_action(context)]
