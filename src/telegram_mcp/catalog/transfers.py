"""Following and stopping background file transfers: ``tg_transfers_list`` and
``tg_transfers_cancel`` (BR28.6, BR28.7).

Both act only on the broker's own transfers and never on Telegram, so they are reads: they ask
nothing, are not in the approval hook's matcher, and make no Telegram call (NFR2.10). A record's
``name`` can be Telegram's file name, so results list it as Telegram content (NFR4.4)."""

from __future__ import annotations

from typing import Any

from telegram_mcp.actions import ConsentAction
from telegram_mcp.broker.transfers import FinalSendRunning, TransferNotFound
from telegram_mcp.catalog.common import Args, CatalogContext, with_notice
from telegram_mcp.consent import PendingApproval
from telegram_mcp.gateway.protocol import TelegramGateway
from telegram_mcp.prompts import PromptError
from telegram_mcp.tools import (
    DECLS,
    LIST_WAIT_LIMIT,
    TRANSFERS_CANCEL,
    TRANSFERS_LIST,
    InvalidArguments,
)

ID_LIMIT = 64
CONTENT_FIELDS = ["transfers[].name"]
SENDING_MESSAGE = "The file is being sent now and this can no longer be stopped."


def unknown(transfer_id: str) -> PromptError:
    return PromptError(
        "not_found", f"No transfer {transfer_id} is known; the broker may have restarted."
    )


def _validate_list(raw: object) -> dict[str, Any]:
    args = Args(TRANSFERS_LIST, raw, ("transfer_id", "wait_seconds"))
    transfer_id = args.text("transfer_id", min_len=1, max_len=ID_LIMIT)
    wait = args.integer("wait_seconds", default=0, minimum=0, maximum=LIST_WAIT_LIMIT)
    if wait and transfer_id is None:
        raise InvalidArguments("wait_seconds is only for one transfer: give transfer_id too")
    return {"transfer_id": transfer_id, "wait_seconds": wait}


def _validate_cancel(raw: object) -> dict[str, Any]:
    args = Args(TRANSFERS_CANCEL, raw, ("transfer_id",))
    return {"transfer_id": args.required_text("transfer_id", max_len=ID_LIMIT)}


def transfer_actions(context: CatalogContext) -> list[ConsentAction]:
    async def run_list(
        gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
    ) -> Any:
        """BR28.7: every known transfer newest first, or one, waiting up to 25 s for its end."""
        queue = context.queue()
        transfer_id = args["transfer_id"]
        if transfer_id is None:
            return with_notice({"transfers": queue.records()}, CONTENT_FIELDS)
        try:
            found = await queue.wait(transfer_id, args["wait_seconds"])
        except TransferNotFound as missing:
            raise unknown(transfer_id) from missing
        return with_notice({"transfers": [found.record(queue.clock())]}, CONTENT_FIELDS)

    async def run_cancel(
        gateway: TelegramGateway, approval: PendingApproval | None, args: dict[str, Any]
    ) -> Any:
        """BR28.6: stop a queued or transferring transfer; a final send cannot be stopped."""
        queue = context.queue()
        transfer_id = args["transfer_id"]
        try:
            found, already = await queue.cancel(transfer_id)
        except TransferNotFound as missing:
            raise unknown(transfer_id) from missing
        except FinalSendRunning as sending:
            raise PromptError("invalid_arguments", SENDING_MESSAGE) from sending
        record = found.record(queue.clock())
        return with_notice({"transfers": [record], "already_finished": already}, CONTENT_FIELDS)

    return [
        ConsentAction(DECLS[TRANSFERS_LIST], _validate_list, run=run_list, uses_telegram=False),
        ConsentAction(
            DECLS[TRANSFERS_CANCEL], _validate_cancel, run=run_cancel, uses_telegram=False
        ),
    ]


def read_actions(context: CatalogContext) -> list[ConsentAction]:
    return transfer_actions(context)
