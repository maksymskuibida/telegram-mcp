"""Local files: sending a file from the outbox (BR25) and downloading media into the downloads
folder (BR26). Both go through the FileSandbox (``files.py``)."""

from __future__ import annotations

from typing import Any

from telegram_mcp.actions import ConsentAction
from telegram_mcp.activity_log import iso_utc
from telegram_mcp.catalog.common import Args, CatalogContext
from telegram_mcp.catalog.messages import approved, approved_chat
from telegram_mcp.consent import PendingApproval, canonical_digest
from telegram_mcp.files import NO_RECORD_MESSAGE, FileRefused, admit, open_admitted
from telegram_mcp.gateway.protocol import TelegramGateway
from telegram_mcp.prompts import PromptFields
from telegram_mcp.tools import CAPTION_LIMIT, DECLS, FILES_SEND


def _validate_send(raw: object) -> dict[str, Any]:
    args = Args(FILES_SEND, raw, ("chat", "path", "caption"))
    return {
        "chat": args.chat(),
        "path": args.required_text("path", max_len=4096),
        "caption": args.text("caption", default="", max_len=CAPTION_LIMIT),
    }


def file_send_action(context: CatalogContext) -> ConsentAction:
    async def describe(gateway: TelegramGateway, args: dict[str, Any]) -> PromptFields:
        chat = await gateway.resolve_chat(args["chat"])
        admitted = admit(args["path"], context.paths)
        context.files.put(canonical_digest(FILES_SEND, args), admitted)
        return PromptFields(
            action="file_send",
            tool=FILES_SEND,
            chat=chat,
            content=args["caption"],
            file=admitted.fields(),
        )

    async def run(
        gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
    ) -> Any:
        fields, chat = approved(approval), approved_chat(approval)
        assert approval is not None  # noqa: S101 (approved() checked it)
        record = context.files.take(approval.args_digest)
        if record is None or fields.file is None or record.resolved_path != fields.file.path:
            raise FileRefused("not_allowed", NO_RECORD_MESSAGE)
        with open_admitted(record, context.paths) as upload:
            sent = await gateway.send_file(chat.id, upload, record.name, fields.content)
        return {
            "chat_id": chat.id,
            "message_id": sent.id,
            "date": iso_utc(sent.date),
            "path": record.resolved_path,
            "name": record.name,
            "size_bytes": record.size_bytes,
        }

    return ConsentAction(DECLS[FILES_SEND], _validate_send, describe, run)


def write_actions(context: CatalogContext) -> list[ConsentAction]:
    return [file_send_action(context)]
