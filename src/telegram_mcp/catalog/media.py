"""Local files: sending a file from the outbox (BR25) and downloading media into the downloads
folder (BR26). Both go through the FileSandbox (``files.py``)."""

from __future__ import annotations

from typing import Any, BinaryIO

from telegram_mcp.actions import ConsentAction
from telegram_mcp.activity_log import iso_utc
from telegram_mcp.catalog.common import Args, CatalogContext, with_notice
from telegram_mcp.catalog.messages import approved, approved_chat
from telegram_mcp.consent import PendingApproval, canonical_digest
from telegram_mcp.files import (
    MAX_FILE_BYTES,
    MAX_FILE_LABEL,
    NO_RECORD_MESSAGE,
    FileRefused,
    admit,
    folder_for,
    open_admitted,
    place_download,
    safe_name,
)
from telegram_mcp.gateway.protocol import TelegramGateway
from telegram_mcp.prompts import PromptError, PromptFields
from telegram_mcp.tools import CAPTION_LIMIT, DECLS, FILES_SEND, MEDIA_DOWNLOAD


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


# tg_media_download (BR26) ----------------------------------------------------------------------


def _validate_download(raw: object) -> dict[str, Any]:
    args = Args(MEDIA_DOWNLOAD, raw, ("chat", "message_id"))
    return {"chat": args.chat(), "message_id": args.required_int("message_id")}


def download_action(context: CatalogContext) -> ConsentAction:
    """A read: no question, no mark. The tool takes no path; the file goes into the downloads
    folder under a safe name that never replaces an existing file (BR26.1 to BR26.4)."""

    async def run(
        gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
    ) -> Any:
        chat_id, message_id = args["chat"], args["message_id"]
        found = await gateway.read_messages(chat_id, 1, ids=[message_id])
        if not found:
            raise PromptError("not_found", f"Message {message_id} was not found in this chat.")
        media = found[0].media
        if media is None:
            raise PromptError("invalid_arguments", "This message has no file to download.")
        if media.size_bytes > MAX_FILE_BYTES:
            raise PromptError(
                "invalid_arguments",
                f"The file is {media.size_bytes} bytes; files over {MAX_FILE_LABEL} are not "
                "downloaded by this tool. Download it in Telegram yourself.",
            )
        folder = folder_for(context.paths, "downloads_dir")
        wanted = safe_name(media.file_name, chat_id, message_id, media.kind)

        async def write(target: BinaryIO) -> int:
            return await gateway.download_media(chat_id, message_id, target, MAX_FILE_BYTES)

        placed = await place_download(folder, wanted, write)
        return with_notice(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "path": placed.path,
                "name": placed.name,
                "size_bytes": placed.size_bytes,
            },
            ["path", "name"],
        )

    return ConsentAction(DECLS[MEDIA_DOWNLOAD], _validate_download, run=run)


def read_actions(context: CatalogContext) -> list[ConsentAction]:
    return [download_action(context)]
