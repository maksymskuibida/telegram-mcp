"""The person's own profile: changing it (BR23.5, BR25) and reading it (BR22.4)."""

from __future__ import annotations

import re
from typing import Any

from telegram_mcp.actions import ConsentAction
from telegram_mcp.catalog.common import Args, CatalogContext
from telegram_mcp.catalog.messages import approved
from telegram_mcp.consent import PendingApproval, canonical_digest
from telegram_mcp.files import NO_RECORD_MESSAGE, FileRefused, admit, open_admitted
from telegram_mcp.gateway.protocol import TelegramGateway
from telegram_mcp.prompts import FieldChange, HeadlineDetails, PromptFields
from telegram_mcp.tools import ABOUT_LIMIT, DECLS, PROFILE_EDIT, InvalidArguments

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
            admitted = admit(args["photo_path"], context.paths)
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
        fields = approved(approval)
        values = {change.field: change.after for change in fields.details.changes}
        named = [values.get(name) for name in FIELDS]
        if fields.file is None:
            await gateway.edit_own_profile(*named, None, None)
            return {"changed": list(values)}
        assert approval is not None  # noqa: S101 (approved() checked it)
        record = context.files.take(approval.args_digest)
        if record is None:
            raise FileRefused("not_allowed", NO_RECORD_MESSAGE)
        with open_admitted(record, context.paths) as photo:
            await gateway.edit_own_profile(*named, photo, record.name)
        return {"changed": [*values, "photo"]}

    return ConsentAction(DECLS[PROFILE_EDIT], _validate, describe, run)


def write_actions(context: CatalogContext) -> list[ConsentAction]:
    return [profile_edit_action(context)]
