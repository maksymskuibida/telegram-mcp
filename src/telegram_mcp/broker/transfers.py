"""Background file transfers in the broker (BR28).

An upload (``tg_files_send``, a ``photo_path``) or a download (``tg_media_download``) is accepted
as a :class:`Transfer` and the tool call returns its id at once. At most one upload and one
download are transferring at a time and at most 20 wait, each starting in the order accepted
when its direction is free (BR28.1). Bytes move in parts of at most 512 KB, each part one
Telegram request with the usual 30 s limit. There is no total time limit: a transfer stops
only when no part has completed for 60 s (BR28.3). A part that timed out or lost its
connection is tried again after a short pause, a download whose file reference expired reads
its message again and goes on, and a flood wait of at most 30 s is waited out with the stall
clock paused; a longer one ends the transfer.

An upload's parts are stored under a random id nobody can see; only its single final request
(send the file, set a photo) makes it visible, so an upload that ends earlier has sent nothing
(BR28.4). After the last part the open file is compared with what the person approved, and
only then does the final request enter the write line, once, with the 30 s limit; a timeout
there is ``unknown`` and nothing is ever sent again (BR28.5, BR28.8). A download appends to a
hidden partial file and gives it its name only when complete (BR26.3).

Every limit is measured on the broker's injected clock and waited with its injected sleep, so a
Mac asleep for more than 60 s counts as no progress, and tests reach every limit on a fake
clock. Transfers live in memory only; a stop or a logout ends them (BR28.9)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import math
import os
import secrets
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, BinaryIO

from telegram_mcp.activity_log import iso_utc
from telegram_mcp.files import (
    MAX_DOWNLOAD_BYTES,
    AdmittedFile,
    FileRefused,
    create_partial,
    place_partial,
    unchanged,
)
from telegram_mcp.gateway.models import (
    ChatNotFound,
    FileReferenceExpired,
    FloodWait,
    GatewayError,
    GatewayTimeout,
    NotAuthorized,
    OutcomeUnknown,
    PartRetryable,
    TelegramError,
    UploadedFile,
)
from telegram_mcp.gateway.protocol import TelegramGateway

PART_BYTES = 512 * 1024
STALL_LIMIT_SECONDS = 60.0
REQUEST_LIMIT_SECONDS = 30.0
RETRY_PAUSE_SECONDS = 1.0
LONGEST_FLOOD_WAIT_SECONDS = 30
WAITING_LIMIT = 20
KEEP_SECONDS = 3600.0
KEEP_COUNT = 100
SMALL_UPLOAD_BYTES = 10 * 1024 * 1024
"""Uploads of at most 10 MB are stored as small-file parts with their checksum."""
LIST_WAIT_LIMIT_SECONDS = 25
TICK_SECONDS = 1.0
"""How often a request in flight checks its limits on the clock."""
CLOSE_GRACE_SECONDS = 5.0

ACCEPTING_CLIENT: ContextVar[str] = ContextVar("accepting_client", default="unknown")
"""The label of the client whose tool call accepts a transfer, for its end entry (BR28.10)."""


class TransferState(StrEnum):
    QUEUED = "queued"
    TRANSFERRING = "transferring"
    SENDING = "sending"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class Direction(StrEnum):
    UPLOAD = "upload"
    DOWNLOAD = "download"


FINAL_STATES = frozenset(
    {TransferState.DONE, TransferState.FAILED, TransferState.CANCELLED, TransferState.UNKNOWN}
)
MOVES: dict[TransferState, frozenset[TransferState]] = {
    TransferState.QUEUED: frozenset(
        {TransferState.TRANSFERRING, TransferState.FAILED, TransferState.CANCELLED}
    ),
    TransferState.TRANSFERRING: frozenset(
        {
            TransferState.SENDING,
            TransferState.DONE,
            TransferState.FAILED,
            TransferState.CANCELLED,
        }
    ),
    TransferState.SENDING: frozenset(
        {TransferState.DONE, TransferState.FAILED, TransferState.UNKNOWN}
    ),
}
"""BR28.2: the only moves; a final state has none. ``sending`` is for uploads only."""

NOTHING_SENT = "Nothing was sent. Call the tool again to be asked again."
MESSAGES = {
    "stalled": "Nothing moved for 60 seconds; the transfer was stopped.",
    "changed": (
        "The file changed during the upload; nothing was sent. Call the tool again to be asked "
        "about the new file."
    ),
    "outcome_unknown": (
        "The file may or may not have been sent; look at the chat before sending it again."
    ),
    "cancelled": "The transfer was cancelled.",
    "broker_stopped": "The broker stopped before the transfer finished.",
    "no_session": "Not logged in: run `telegram-mcp login` in a terminal.",
    "not_found": "This account cannot see that chat or message any more.",
    "too_large": "The file passed 4000 MiB, the largest file Telegram stores.",
    "not_started": "Other sends were still running, so the file was not sent.",
}
ACCEPTED_NOTE = (
    "The transfer runs in the background. Follow it with tg_transfers_list (transfer_id, and "
    "wait_seconds up to 25 to wait for its end); stop it with tg_transfers_cancel."
)

FinalCall = Callable[[TelegramGateway, UploadedFile], Awaitable[dict[str, Any]]]
"""An upload's final request (send the file, set a photo) and the result it returns."""
GatewayOpener = Callable[[], Awaitable[TelegramGateway]]
FinalLine = Callable[["Transfer", Callable[[], Awaitable[Any]]], Awaitable[Any]]
"""Runs a final request from the write line, once; raises :class:`FinalNotStarted` when it
left the line unsent."""
EndListener = Callable[["Transfer"], None]
Sleep = Callable[[float], Awaitable[None]]
PartWriter = Callable[[int, bytes], int]


class InvalidMove(Exception):
    """A state change BR28.2 does not allow."""


class QueueFull(Exception):
    """20 transfers already wait, or the broker is stopping; nothing was created."""


class TransferNotFound(Exception):
    """No transfer with that id is known (it ended over an hour ago, or the broker restarted)."""


class FinalSendRunning(Exception):
    """The transfer is sending its final request, which can no longer be stopped."""


class FinalNotStarted(Exception):
    """The final request left the write line without being sent."""


class _Ended(Exception):
    """Ends a transfer with a code (internal)."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        state: TransferState = TransferState.FAILED,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.message = message or MESSAGES.get(code, code)
        self.state = state
        self.retry_after = retry_after


class _RequestTimedOut(Exception):
    """One request passed its 30 s limit on the clock (internal)."""


@dataclass(eq=False)
class Transfer:
    """One upload or download in the background (entities.md Transfer). The record a tool
    returns never holds file content, the caption or a secret (BR28.7)."""

    transfer_id: str
    direction: Direction
    tool: str
    name: str
    size_bytes: int | None
    created_at: datetime
    client: str = "unknown"
    chat_id: int | None = None
    message_id: int | None = None
    approval_id: str | None = None
    path: str | None = None
    final_request: str | None = None
    state: TransferState = TransferState.QUEUED
    bytes_done: int = 0
    updated_at: datetime | None = None
    started_at: datetime | None = None
    last_progress_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    code: str | None = None
    message: str | None = None
    retry_after: int | None = None
    media_kind: str = "other"
    # Working parts, never shown: the open file and its record (uploads), the final request
    # (which holds the caption), the downloads folder and the partial file.
    source: BinaryIO | None = field(default=None, repr=False)
    admitted: AdmittedFile | None = field(default=None, repr=False)
    final: FinalCall | None = field(default=None, repr=False)
    folder: Path | None = field(default=None, repr=False)
    partial: Path | None = field(default=None, repr=False)
    partial_fd: int | None = field(default=None, repr=False)
    stop_code: str | None = field(default=None, repr=False)
    ended: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def __post_init__(self) -> None:
        if self.updated_at is None:
            self.updated_at = self.created_at

    @property
    def is_final(self) -> bool:
        return self.state in FINAL_STATES

    def move(self, state: TransferState, now: datetime) -> None:
        """BR28.2: one step forward, or :class:`InvalidMove`."""
        if state not in MOVES.get(self.state, frozenset()):
            raise InvalidMove(f"{self.state} -> {state}")
        if state is TransferState.SENDING and self.direction is not Direction.UPLOAD:
            raise InvalidMove("only an upload sends")
        self.state = state
        self.updated_at = now
        if state is TransferState.TRANSFERRING:
            self.started_at = now
            self.last_progress_at = now
        if state in FINAL_STATES:
            self.finished_at = now
            self.ended.set()

    def progress(self, done: int, now: datetime) -> None:
        """A part completed: bytes_done only grows and resets the stall clock (BR28.3)."""
        if done < self.bytes_done:
            raise InvalidMove("bytes_done only grows")
        self.bytes_done = done
        self.last_progress_at = now
        self.updated_at = now

    def accepted(self) -> dict[str, Any]:
        """What the accepting call returns at once (BR28.1)."""
        return {
            "transfer_id": self.transfer_id,
            "state": str(self.state),
            "direction": str(self.direction),
            "tool": self.tool,
            "chat_id": self.chat_id,
            "name": self.name,
            "size_bytes": self.size_bytes,
            "note": ACCEPTED_NOTE,
        }

    def record(self, now: datetime) -> dict[str, Any]:
        """BR28.7: progress and outcome, never content."""
        percent = None
        if self.size_bytes:
            percent = round(100 * self.bytes_done / self.size_bytes, 1)
        since = None
        if self.state is TransferState.TRANSFERRING and self.last_progress_at is not None:
            since = max(0, int((now - self.last_progress_at).total_seconds()))
        return {
            "transfer_id": self.transfer_id,
            "direction": str(self.direction),
            "tool": self.tool,
            "state": str(self.state),
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "name": self.name,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "bytes_done": self.bytes_done,
            "percent": percent,
            "created_at": iso_utc(self.created_at),
            "updated_at": iso_utc(self.updated_at or self.created_at),
            "finished_at": iso_utc(self.finished_at) if self.finished_at else None,
            "seconds_since_progress": since,
            "result": self.result,
            "code": self.code,
            "message": self.message,
            "retry_after": self.retry_after,
        }


class TransferQueue:
    """The broker's transfers: one upload and one download slot, 20 waiting (BR28.1)."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        sleep: Sleep,
        open_gateway: GatewayOpener,
        final_line: FinalLine,
        on_end: EndListener | None = None,
        part_bytes: int = PART_BYTES,
        stall_limit: float = STALL_LIMIT_SECONDS,
        request_limit: float = REQUEST_LIMIT_SECONDS,
        retry_pause: float = RETRY_PAUSE_SECONDS,
        waiting_limit: int = WAITING_LIMIT,
        keep_seconds: float = KEEP_SECONDS,
        keep_count: int = KEEP_COUNT,
        max_download_bytes: int = MAX_DOWNLOAD_BYTES,
        small_upload_bytes: int = SMALL_UPLOAD_BYTES,
        write_part: PartWriter = os.write,
    ) -> None:
        self.clock = clock
        self.sleep = sleep
        self.part_bytes = part_bytes
        self.stall_limit = stall_limit
        self.request_limit = request_limit
        self.retry_pause = retry_pause
        self.waiting_limit = waiting_limit
        self.keep_seconds = keep_seconds
        self.keep_count = keep_count
        self.max_download_bytes = max_download_bytes
        self.small_upload_bytes = small_upload_bytes
        self.write_part = write_part
        self._open_gateway = open_gateway
        self._final_line = final_line
        self._on_end = on_end
        self._transfers: dict[str, Transfer] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False
        self.in_flight: dict[Direction, int] = {Direction.UPLOAD: 0, Direction.DOWNLOAD: 0}
        """Part requests in flight per direction; at most one each (NFR11.13)."""
        self.most_in_flight: dict[Direction, int] = {Direction.UPLOAD: 0, Direction.DOWNLOAD: 0}

    # What the broker and the tools ask ---------------------------------------------------------

    @property
    def waiting(self) -> int:
        return sum(1 for t in self._transfers.values() if t.state is TransferState.QUEUED)

    @property
    def is_full(self) -> bool:
        return self._closed or self.waiting >= self.waiting_limit

    @property
    def busy(self) -> bool:
        """True while a transfer is queued, transferring or sending (BR28.9)."""
        return any(not t.is_final for t in self._transfers.values())

    def reserved_download_bytes(self) -> int:
        """BR26.1: the bytes still to come of every download that is queued or transferring;
        an unknown size counts as 4000 MiB."""
        return sum(
            max(0, (t.size_bytes or self.max_download_bytes) - t.bytes_done)
            for t in self._transfers.values()
            if t.direction is Direction.DOWNLOAD and not t.is_final
        )

    def find(self, transfer_id: str) -> Transfer | None:
        self.prune()
        return self._transfers.get(transfer_id)

    def records(self) -> list[dict[str, Any]]:
        """Every known transfer's record, newest first (BR28.7)."""
        self.prune()
        now = self.clock()
        return [t.record(now) for t in reversed(self._transfers.values())]

    def prune(self) -> None:
        """BR28.7: final records are kept for 1 hour, and at most the newest 100."""
        now = self.clock()
        keep = timedelta(seconds=self.keep_seconds)
        finals = [t for t in self._transfers.values() if t.is_final]
        for t in finals:
            if t.finished_at is not None and now - t.finished_at >= keep:
                del self._transfers[t.transfer_id]
        finals = sorted(
            (t for t in self._transfers.values() if t.is_final),
            key=lambda t: t.finished_at or t.created_at,
        )
        for t in finals[: max(0, len(finals) - self.keep_count)]:
            del self._transfers[t.transfer_id]

    # Accepting -----------------------------------------------------------------------------

    def accept_upload(
        self,
        *,
        tool: str,
        chat_id: int | None,
        approval_id: str,
        admitted: AdmittedFile,
        source: BinaryIO,
        final: FinalCall,
        final_request: str,
    ) -> Transfer:
        """BR28.1: an upload of the file opened and checked at acceptance (BR25.5); its
        approval is already used. Raises :class:`QueueFull` with nothing created."""
        transfer = self._new(
            Direction.UPLOAD,
            tool,
            admitted.name,
            admitted.size_bytes,
            chat_id=chat_id,
            approval_id=approval_id,
            path=admitted.resolved_path,
            final_request=final_request,
        )
        transfer.source, transfer.admitted, transfer.final = source, admitted, final
        return self._accept(transfer)

    def accept_download(
        self,
        *,
        tool: str,
        chat_id: int,
        message_id: int,
        name: str,
        size_bytes: int | None,
        folder: Path,
        media_kind: str = "other",
    ) -> Transfer:
        """BR28.1: a download that passed the size limit and the budget (BR26.1)."""
        transfer = self._new(
            Direction.DOWNLOAD, tool, name, size_bytes, chat_id=chat_id, message_id=message_id
        )
        transfer.folder, transfer.media_kind = folder, media_kind
        return self._accept(transfer)

    def _new(
        self,
        direction: Direction,
        tool: str,
        name: str,
        size_bytes: int | None,
        **fields: Any,
    ) -> Transfer:
        if self.is_full:
            raise QueueFull()
        return Transfer(
            transfer_id=f"tr_{secrets.token_hex(6)}",
            direction=direction,
            tool=tool,
            name=name,
            size_bytes=size_bytes,
            created_at=self.clock(),
            client=ACCEPTING_CLIENT.get(),
            **fields,
        )

    def _accept(self, transfer: Transfer) -> Transfer:
        self.prune()
        if self.is_full:
            raise QueueFull()
        self._transfers[transfer.transfer_id] = transfer
        self._pump()
        return transfer

    def _pump(self) -> None:
        """Start the oldest queued transfer of each direction whose place is free."""
        if self._closed:
            return
        for direction in Direction:
            if any(
                t.direction is direction and t.transfer_id in self._tasks
                for t in self._transfers.values()
            ):
                continue
            waiting = next(
                (
                    t
                    for t in self._transfers.values()
                    if t.direction is direction and t.state is TransferState.QUEUED
                ),
                None,
            )
            if waiting is None:
                continue
            waiting.move(TransferState.TRANSFERRING, self.clock())
            task = asyncio.get_running_loop().create_task(self._run(waiting))
            self._tasks[waiting.transfer_id] = task

    # Stopping ------------------------------------------------------------------------------

    async def cancel(self, transfer_id: str) -> tuple[Transfer, bool]:
        """BR28.6: end a queued or transferring transfer ``cancelled``; (the transfer, True when
        it had already finished). Raises :class:`TransferNotFound` or
        :class:`FinalSendRunning`."""
        transfer = self.find(transfer_id)
        if transfer is None:
            raise TransferNotFound(transfer_id)
        if transfer.is_final:
            return transfer, True
        if transfer.state is TransferState.SENDING:
            raise FinalSendRunning(transfer_id)
        await self._stop(transfer, "cancelled")
        return transfer, False

    async def _stop(self, transfer: Transfer, code: str) -> None:
        state = TransferState.CANCELLED if code == "cancelled" else TransferState.FAILED
        task = self._tasks.get(transfer.transfer_id)
        if task is None:
            self._end(transfer, _Ended(code, state=state))
            self._clean_up(transfer)
            return
        transfer.stop_code = code
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if not transfer.is_final:  # cancelled before its first step ran
            self._end(transfer, _Ended(code, state=state))
            self._clean_up(transfer)
            self._tasks.pop(transfer.transfer_id, None)
            self._pump()

    async def close(self) -> None:
        """BR28.9: a stop or a logout. Refuse new transfers, end every queued or transferring
        one ``broker_stopped``, and let a sending one finish within its own 30 s limit."""
        self._closed = True
        for transfer in list(self._transfers.values()):
            if transfer.state in (TransferState.QUEUED, TransferState.TRANSFERRING):
                await self._stop(transfer, "broker_stopped")
        sending = [t for t in self._transfers.values() if t.state is TransferState.SENDING]
        tasks = [self._tasks[t.transfer_id] for t in sending if t.transfer_id in self._tasks]
        if tasks:
            _, late = await asyncio.wait(tasks, timeout=self.request_limit + CLOSE_GRACE_SECONDS)
            for task in late:
                task.cancel()
            for task in late:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        for transfer in sending:
            if not transfer.is_final:  # pragma: no cover (the task ends it when cancelled)
                self._end(transfer, _Ended("outcome_unknown", state=TransferState.UNKNOWN))

    async def wait(self, transfer_id: str, seconds: float) -> Transfer:
        """BR28.7: return once the transfer is final or ``seconds`` have passed."""
        transfer = self.find(transfer_id)
        if transfer is None:
            raise TransferNotFound(transfer_id)
        if transfer.is_final or seconds <= 0:
            return transfer
        ended = asyncio.ensure_future(transfer.ended.wait())
        timer = asyncio.ensure_future(self.sleep(seconds))
        try:
            await asyncio.wait({ended, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in (ended, timer):
                waiter.cancel()
        return transfer

    # Running a transfer ------------------------------------------------------------------------

    async def _run(self, transfer: Transfer) -> None:
        try:
            if transfer.direction is Direction.UPLOAD:
                await self._upload(transfer)
            else:
                await self._download(transfer)
        except _Ended as ended:
            self._end(transfer, ended)
        except asyncio.CancelledError:
            code = transfer.stop_code or "broker_stopped"
            if transfer.state is TransferState.SENDING:
                self._end(transfer, _Ended("outcome_unknown", state=TransferState.UNKNOWN))
            else:
                state = TransferState.CANCELLED if code == "cancelled" else TransferState.FAILED
                self._end(transfer, _Ended(code, state=state))
        except Exception as error:  # every failure ends the transfer, never silently
            if transfer.state is TransferState.SENDING:
                self._end(transfer, _Ended("outcome_unknown", state=TransferState.UNKNOWN))
            else:
                message = f"The transfer failed: {type(error).__name__}."
                self._end(transfer, _Ended("telegram_error", message))
        finally:
            self._clean_up(transfer)
            self._tasks.pop(transfer.transfer_id, None)
            self._pump()

    def _end(self, transfer: Transfer, ended: _Ended) -> None:
        if transfer.is_final:
            return
        message = ended.message
        early = transfer.state in (TransferState.QUEUED, TransferState.TRANSFERRING)
        if transfer.direction is Direction.UPLOAD and early and ended.code != "changed":
            message = f"{message} {NOTHING_SENT}"
        transfer.code, transfer.message, transfer.retry_after = (
            ended.code,
            message,
            ended.retry_after,
        )
        transfer.move(ended.state, self.clock())
        if self._on_end is not None:
            self._on_end(transfer)

    def _clean_up(self, transfer: Transfer) -> None:
        """Close the upload's file, close and remove a download's partial file (BR26.3)."""
        if transfer.source is not None:
            with contextlib.suppress(OSError):
                transfer.source.close()
            transfer.source = None
        if transfer.partial_fd is not None:
            with contextlib.suppress(OSError):
                os.close(transfer.partial_fd)
            transfer.partial_fd = None
        if transfer.partial is not None:
            transfer.partial.unlink(missing_ok=True)
            transfer.partial = None

    async def _upload(self, transfer: Transfer) -> None:
        source, admitted, final = transfer.source, transfer.admitted, transfer.final
        if source is None or admitted is None or final is None:
            raise _Ended("telegram_error", "The upload has no file to read.")
        size = admitted.size_bytes
        total = max(1, math.ceil(size / self.part_bytes))
        big = size > self.small_upload_bytes
        file_id = secrets.randbits(63)
        checksum = hashlib.md5()  # noqa: S324 (Telegram's small-file checksum, not security)
        descriptor = source.fileno()
        for index in range(total):
            offset = index * self.part_bytes
            data = os.pread(descriptor, min(self.part_bytes, size - offset), offset)
            if len(data) != min(self.part_bytes, size - offset):
                raise _Ended("changed")
            checksum.update(data)

            def store(gateway: TelegramGateway, index: int = index, data: bytes = data) -> Any:
                return gateway.upload_part(file_id, index, total, data, big)

            await self._part(transfer, store)
            transfer.progress(offset + len(data), self.clock())
        if not unchanged(admitted, source):
            raise _Ended("changed")
        uploaded = UploadedFile(
            file_id=file_id,
            parts=total,
            name=admitted.name,
            big=big,
            md5="" if big else checksum.hexdigest(),
        )
        transfer.move(TransferState.SENDING, self.clock())
        transfer.result = await self._send_final(transfer, final, uploaded)
        transfer.move(TransferState.DONE, self.clock())
        if self._on_end is not None:
            self._on_end(transfer)

    async def _send_final(
        self, transfer: Transfer, final: FinalCall, uploaded: UploadedFile
    ) -> dict[str, Any]:
        """BR28.5: the one final request, from the write line, under the 30 s limit."""

        async def run() -> dict[str, Any]:
            gateway = await self._open_gateway()
            return await final(gateway, uploaded)

        try:
            result: dict[str, Any] = await self._final_line(
                transfer, lambda: self._bounded(None, run())
            )
            return result
        except FinalNotStarted as unsent:
            raise _Ended("telegram_error", f"{MESSAGES['not_started']} {NOTHING_SENT}") from unsent
        except FloodWait as flood:
            message = f"Telegram asked to wait {flood.seconds} s; nothing was sent. {NOTHING_SENT}"
            raise _Ended("flood_wait", message, retry_after=flood.seconds) from flood
        except NotAuthorized as error:
            raise _Ended("no_session") from error
        except ChatNotFound as error:
            raise _Ended("not_found") from error
        except TelegramError as error:
            raise _Ended("telegram_error", f"Telegram refused the request: {error.reason}.") from (
                error
            )
        except (
            _RequestTimedOut,
            TimeoutError,
            OutcomeUnknown,
            GatewayTimeout,
            PartRetryable,
            ConnectionError,
        ) as error:
            raise _Ended("outcome_unknown", state=TransferState.UNKNOWN) from error

    async def _download(self, transfer: Transfer) -> None:
        folder = transfer.folder
        chat_id, message_id = transfer.chat_id, transfer.message_id
        if folder is None or chat_id is None or message_id is None:
            raise _Ended("telegram_error", "The download has no place to go.")
        try:
            transfer.partial, transfer.partial_fd = create_partial(folder)
        except OSError as error:
            raise _Ended("disk_full", f"The partial file could not be created: {error}.") from error
        size = transfer.size_bytes
        offset = 0
        while True:

            def fetch(gateway: TelegramGateway, offset: int = offset) -> Any:
                return gateway.download_part(chat_id, message_id, offset, self.part_bytes)

            data: bytes = await self._part(transfer, fetch)
            if size is not None:
                data = data[: max(0, size - offset)]
            if data:
                self._write(transfer, data)
            offset += len(data)
            transfer.progress(offset, self.clock())
            if size is None and offset > self.max_download_bytes:
                raise _Ended("too_large")
            if (size is not None and offset >= size) or len(data) < self.part_bytes:
                break
        if size is not None and offset < size:
            raise _Ended("telegram_error", "Telegram sent fewer bytes than the file has.")
        if transfer.partial_fd is not None:
            os.close(transfer.partial_fd)
            transfer.partial_fd = None
        partial = transfer.partial
        transfer.partial = None
        if partial is None:  # pragma: no cover (set above)
            raise _Ended("telegram_error")
        try:
            placed = place_partial(folder, partial, transfer.name)
        except FileRefused as refused:
            raise _Ended("telegram_error", refused.message) from refused
        except OSError as error:
            raise _Ended("disk_full", f"The file could not be placed: {error}.") from error
        transfer.path, transfer.name, transfer.size_bytes = placed.path, placed.name, offset
        transfer.result = {
            "chat_id": chat_id,
            "message_id": message_id,
            "path": placed.path,
            "name": placed.name,
            "size_bytes": placed.size_bytes,
        }
        transfer.move(TransferState.DONE, self.clock())
        if self._on_end is not None:
            self._on_end(transfer)

    def _write(self, transfer: Transfer, data: bytes) -> None:
        """Append one part to the partial file; a full disk ends the download (BR26.3)."""
        descriptor = transfer.partial_fd
        if descriptor is None:  # pragma: no cover (opened before the first part)
            raise _Ended("disk_full")
        view = memoryview(data)
        try:
            while view:
                written = self.write_part(descriptor, bytes(view))
                view = view[written:]
        except OSError as error:
            reason = error.strerror or type(error).__name__
            message = f"The downloads folder could not take the file ({reason})."
            raise _Ended("disk_full", message) from error

    # One part ------------------------------------------------------------------------------

    async def _part(
        self, transfer: Transfer, request: Callable[[TelegramGateway], Awaitable[Any]]
    ) -> Any:
        """One part request, tried again inside the stall limit (BR28.3)."""
        while True:
            self._check_stall(transfer)
            direction = transfer.direction
            self.in_flight[direction] += 1
            self.most_in_flight[direction] = max(
                self.most_in_flight[direction], self.in_flight[direction]
            )
            try:
                gateway = await self._bounded(transfer, self._open_gateway())
                return await self._bounded(transfer, request(gateway))
            except FloodWait as flood:
                if flood.seconds > LONGEST_FLOOD_WAIT_SECONDS:
                    message = f"Telegram asked to wait {flood.seconds} s; the transfer stopped."
                    raise _Ended("flood_wait", message, retry_after=flood.seconds) from flood
                wait = flood.seconds
            except FileReferenceExpired:
                await self._refresh_reference(transfer)
                continue
            except (_RequestTimedOut, PartRetryable, GatewayTimeout, TimeoutError, ConnectionError):
                wait = None
            except NotAuthorized as error:
                raise _Ended("no_session") from error
            except ChatNotFound as error:
                raise _Ended("not_found") from error
            except TelegramError as error:
                raise _Ended(
                    "telegram_error", f"Telegram refused a part: {error.reason}."
                ) from error
            except GatewayError as error:
                raise _Ended("telegram_error", f"A part failed: {type(error).__name__}.") from error
            finally:
                self.in_flight[direction] -= 1
            if wait is None:
                await self.sleep(self.retry_pause)
                continue
            await self.sleep(wait)  # waited out with the stall clock paused (BR28.3)
            if transfer.last_progress_at is not None:
                transfer.last_progress_at += timedelta(seconds=wait)

    async def _refresh_reference(self, transfer: Transfer) -> None:
        """A download's file reference expired: read the message again (one read with the usual
        limit); that counts as a retry inside the stall limit (BR28.3)."""
        chat_id, message_id = transfer.chat_id, transfer.message_id
        if chat_id is None or message_id is None:  # pragma: no cover (downloads have both)
            raise _Ended("telegram_error")
        with contextlib.suppress(_RequestTimedOut, PartRetryable, GatewayTimeout, TimeoutError):
            gateway = await self._bounded(transfer, self._open_gateway())
            await self._bounded(transfer, gateway.media_info(chat_id, message_id))

    def _check_stall(self, transfer: Transfer) -> None:
        if self._stalled(transfer):
            raise _Ended("stalled")

    def _stalled(self, transfer: Transfer) -> bool:
        if transfer.last_progress_at is None:
            return False
        since = (self.clock() - transfer.last_progress_at).total_seconds()
        return since >= self.stall_limit

    async def _bounded[T](self, transfer: Transfer | None, awaitable: Awaitable[T]) -> T:
        """Await one request under its 30 s limit and, for a part, the stall limit, both on
        the clock. The request is cancelled when either passes."""
        task: asyncio.Future[T] = asyncio.ensure_future(awaitable)
        started = self.clock()
        try:
            while True:
                if task.done():
                    return task.result()
                elapsed = (self.clock() - started).total_seconds()
                waits = [TICK_SECONDS, self.request_limit - elapsed]
                if transfer is not None:
                    if self._stalled(transfer):
                        raise _Ended("stalled")
                    if transfer.last_progress_at is not None:
                        since = (self.clock() - transfer.last_progress_at).total_seconds()
                        waits.append(self.stall_limit - since)
                if elapsed >= self.request_limit:
                    raise _RequestTimedOut()
                timer = asyncio.ensure_future(self.sleep(max(0.0, min(waits))))
                both: set[asyncio.Future[Any]] = {task, timer}
                try:
                    await asyncio.wait(both, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    timer.cancel()
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
