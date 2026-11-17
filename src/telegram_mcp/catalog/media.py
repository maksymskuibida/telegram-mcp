"""Local files: sending a file from the outbox (BR25) and downloading media into the downloads
folder (BR26). Both go through the FileSandbox (``files.py``) and run as background transfers in
the broker (BR28): the call returns the accepted transfer at once."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from telegram_mcp.actions import ConsentAction
from telegram_mcp.activity_log import iso_utc
from telegram_mcp.broker.transfers import QUEUE_FULL_MESSAGE, FinalCall, QueueFull, Transfer
from telegram_mcp.catalog.common import Args, CatalogContext, with_notice
from telegram_mcp.catalog.messages import approved, approved_chat
from telegram_mcp.consent import PendingApproval, canonical_digest
from telegram_mcp.files import (
    NO_RECORD_MESSAGE,
    AdmittedFile,
    FileRefused,
    admit,
    check_download_budget,
    folder_for,
    open_admitted,
    safe_name,
)
from telegram_mcp.gateway.models import TelegramError, UploadedFile
from telegram_mcp.gateway.protocol import TelegramGateway
from telegram_mcp.prompts import PromptError, PromptFields
from telegram_mcp.tools import CAPTION_LIMIT, DECLS, FILES_SEND, MEDIA_DOWNLOAD


def queue_full() -> PromptError:
    """BR28.1: the refusal when 20 transfers already wait."""
    return PromptError("telegram_error", QUEUE_FULL_MESSAGE)


def start_upload(
    context: CatalogContext,
    approval: PendingApproval,
    *,
    tool: str,
    chat_id: int | None,
    final: Callable[[AdmittedFile], FinalCall],
    final_request: str,
) -> Transfer:
    """BR25.5, BR28.1: take the file record kept since the question, open and check the file,
    and accept an upload transfer that reads only from that open file. The approval is used:
    whatever happens to the transfer, it is never run again from it."""
    record = context.files.take(approval.args_digest)
    fields = approval.prompt
    if record is None or fields.file is None or record.resolved_path != fields.file.path:
        raise FileRefused("not_allowed", NO_RECORD_MESSAGE)
    queue = context.queue()
    source = open_admitted(record, context.paths)
    try:
        return queue.accept_upload(
            tool=tool,
            chat_id=chat_id,
            approval_id=approval.id,
            admitted=record,
            source=source,
            final=final(record),
            final_request=final_request,
        )
    except QueueFull as full:
        source.close()
        raise queue_full() from full
    except BaseException:
        source.close()
        raise


def start_photo_upload(
    context: CatalogContext,
    approval: PendingApproval,
    changed: list[str],
    *,
    tool: str,
    chat_id: int | None,
    final: Callable[[AdmittedFile], FinalCall],
    final_request: str,
) -> Transfer:
    """BR28.11: the photo of a chat or profile change, after its other fields changed. When the
    photo cannot start, the refusal says which fields were already changed, so the person is
    never told nothing happened when something did."""
    try:
        return start_upload(
            context,
            approval,
            tool=tool,
            chat_id=chat_id,
            final=final,
            final_request=final_request,
        )
    except PromptError as refused:
        if not changed:
            raise
        applied = ", ".join(changed)
        message = f"{refused.message} The photo was not set, but these were changed: {applied}."
        raise type(refused)(refused.code, message) from refused


def starts_with_photo(args: dict[str, Any]) -> bool:
    return args.get("photo_path") is not None


def _validate_send(raw: object) -> dict[str, Any]:
    args = Args(FILES_SEND, raw, ("chat", "path", "caption"))
    return {
        "chat": args.chat(),
        "path": args.required_text("path", max_len=4096),
        "caption": args.text("caption", default="", max_len=CAPTION_LIMIT),
    }


def _send_final(chat_id: int, caption: str) -> Callable[[AdmittedFile], FinalCall]:
    """The final request of a send (BR28.5): the uploaded parts as one file with the approved
    caption, which only this closure holds (never a record or the log)."""

    def final(record: AdmittedFile) -> FinalCall:
        async def send(gateway: TelegramGateway, uploaded: UploadedFile) -> dict[str, Any]:
            sent = await gateway.send_uploaded_file(chat_id, uploaded, caption)
            return {
                "chat_id": chat_id,
                "message_id": sent.id,
                "date": iso_utc(sent.date),
                "path": record.resolved_path,
                "name": record.name,
                "size_bytes": record.size_bytes,
            }

        return send

    return final


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
        transfer = start_upload(
            context,
            approval,
            tool=FILES_SEND,
            chat_id=chat.id,
            final=_send_final(chat.id, fields.content),
            final_request="send_file",
        )
        return {**transfer.accepted(), "path": transfer.path}

    return ConsentAction(
        DECLS[FILES_SEND],
        _validate_send,
        describe,
        run,
        log_result=file_line,
        starts_transfer=lambda args: True,
    )


def file_line(data: Any) -> str | None:
    """The result entry of an accepted transfer: its id, the path, name and size, never
    content (NFR8.5, BR28.10)."""
    if not isinstance(data, dict) or "transfer_id" not in data:
        return None
    where = data.get("path") or data["name"]
    size = data.get("size_bytes")
    sized = f"{size} bytes" if size is not None else "size unknown"
    return f"transfer {data['transfer_id']} {data['state']}: {where} ({data['name']}, {sized})"


def write_actions(context: CatalogContext) -> list[ConsentAction]:
    return [file_send_action(context)]


# tg_media_download (BR26, BR28) ----------------------------------------------------------------


def _validate_download(raw: object) -> dict[str, Any]:
    args = Args(MEDIA_DOWNLOAD, raw, ("chat", "message_id"))
    return {"chat": args.chat(), "message_id": args.required_int("message_id")}


def download_action(context: CatalogContext) -> ConsentAction:
    """A read: no question, no mark. The tool takes no path; the file goes into the downloads
    folder under a safe name that never replaces an existing file (BR26.1 to BR26.4). The call
    reads the media's size, checks the limit and the budget, and accepts a download transfer."""

    async def run(
        gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
    ) -> Any:
        chat_id, message_id = args["chat"], args["message_id"]
        try:
            media = await gateway.media_info(chat_id, message_id)
        except TelegramError as error:
            if "MESSAGE_ID_INVALID" not in error.reason:
                raise
            raise PromptError(
                "not_found", f"Message {message_id} was not found in this chat."
            ) from error
        if media is None:
            raise PromptError("invalid_arguments", "This message has no file to download.")
        size = media.size_bytes or None  # Telegram never stores an empty file (BR26.1)
        folder = folder_for(context.paths, "downloads_dir")
        queue = context.queue()
        check_download_budget(
            folder, size, queue.reserved_download_bytes(), context.free_space(folder)
        )
        wanted = safe_name(media.file_name, chat_id, message_id, media.kind)
        try:
            transfer = queue.accept_download(
                tool=MEDIA_DOWNLOAD,
                chat_id=chat_id,
                message_id=message_id,
                name=wanted,
                size_bytes=size,
                folder=folder,
                media_kind=media.kind,
            )
        except QueueFull as full:
            raise queue_full() from full
        return with_notice({**transfer.accepted(), "message_id": message_id}, ["name"])

    return ConsentAction(DECLS[MEDIA_DOWNLOAD], _validate_download, run=run, log_result=file_line)


def read_actions(context: CatalogContext) -> list[ConsentAction]:
    return [download_action(context)]
