"""How the broker schedules Telegram calls: one line for writes, a few places for reads.

Writes run one at a time, in the order they arrived, from one serving task. A write that has
not started 40 s after its request reached the broker, or whose caller's connection has closed,
leaves the line unsent. Nothing ever puts a write back into the line, so a write runs at most
once. Reads never use the line; at most ten of them run against Telegram at once."""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from telegram_mcp.gateway.models import OutcomeUnknown

WRITE_START_LIMIT_SECONDS = 40.0
WRITE_LINE_CAPACITY = 20
READ_PLACES = 10


class WriteState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    DROPPED = "dropped"


class DropReason(StrEnum):
    DEADLINE = "not started in time"
    CALLER_LEFT = "its caller's connection closed"
    STOPPING = "the broker is stopping"


class LineFull(Exception):
    """The line already holds as many waiting writes as it may."""


class WriteDropped(Exception):
    """The write left the line without being sent."""

    def __init__(self, reason: DropReason) -> None:
        super().__init__(str(reason))
        self.reason = reason


@dataclass(eq=False)
class WriteItem:
    """One write waiting in or running from the line. Its approval is already used."""

    tool: str
    approval_id: str
    arrived_at: datetime
    is_open: Callable[[], bool]
    run: Callable[[], Awaitable[Any]]
    state: WriteState = WriteState.QUEUED
    outcome: asyncio.Future[Any] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


DropListener = Callable[[WriteItem, DropReason], None]


class WriteLine:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        start_limit: float = WRITE_START_LIMIT_SECONDS,
        capacity: int = WRITE_LINE_CAPACITY,
        on_drop: DropListener | None = None,
    ) -> None:
        self._clock = clock
        self._start_limit = start_limit
        self._capacity = capacity
        self._on_drop = on_drop
        self._items: deque[WriteItem] = deque()
        self._ready = asyncio.Event()
        self._server: asyncio.Task[None] | None = None
        self.running: WriteItem | None = None

    @property
    def waiting(self) -> int:
        return sum(1 for item in self._items if item.state is WriteState.QUEUED)

    @property
    def is_full(self) -> bool:
        return self.waiting >= self._capacity

    @property
    def busy(self) -> bool:
        return self.running is not None or self.waiting > 0

    def start(self) -> None:
        if self._server is None:
            self._server = asyncio.get_running_loop().create_task(self._serve())

    async def close(self) -> None:
        """Drop every waiting write unsent and stop serving. A running write is cancelled,
        which its caller sees as an unknown outcome; it is never started again."""
        for item in list(self._items):
            self._drop(item, DropReason.STOPPING)
        if self._server is not None:
            self._server.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._server
            self._server = None

    async def submit(self, item: WriteItem) -> Any:
        """Queue ``item`` and return what its run returned, or raise what it raised.
        Raises ``LineFull`` before queueing, ``WriteDropped`` when it leaves unsent."""
        if self.is_full:
            raise LineFull()
        item.outcome.add_done_callback(_retrieve)
        self._items.append(item)
        self._ready.set()
        left = self._start_limit - (self._clock() - item.arrived_at).total_seconds()
        timer = asyncio.get_running_loop().call_later(max(0.0, left), self._expire, item)
        try:
            return await asyncio.shield(item.outcome)
        finally:
            timer.cancel()

    def drop_overdue(self) -> None:
        """Drop every waiting write that is past its start limit or whose caller left."""
        for item in list(self._items):
            if item.state is not WriteState.QUEUED:
                continue
            if self._overdue(item):
                self._drop(item, DropReason.DEADLINE)
            elif not item.is_open():
                self._drop(item, DropReason.CALLER_LEFT)

    def _overdue(self, item: WriteItem) -> bool:
        return (self._clock() - item.arrived_at).total_seconds() >= self._start_limit

    def _expire(self, item: WriteItem) -> None:
        if item.state is WriteState.QUEUED:
            self._drop(item, DropReason.DEADLINE)

    def _drop(self, item: WriteItem, reason: DropReason) -> None:
        if item.state is not WriteState.QUEUED:
            return
        item.state = WriteState.DROPPED
        with contextlib.suppress(ValueError):
            self._items.remove(item)
        if not item.outcome.done():
            item.outcome.set_exception(WriteDropped(reason))
        if self._on_drop is not None:
            self._on_drop(item, reason)

    async def _next(self) -> WriteItem:
        while True:
            self.drop_overdue()
            if self._items:
                return self._items.popleft()
            self._ready.clear()
            await self._ready.wait()

    async def _serve(self) -> None:
        while True:
            item = await self._next()
            item.state = WriteState.RUNNING
            self.running = item
            try:
                result = await item.run()
            except asyncio.CancelledError:
                _settle(item, error=OutcomeUnknown())
                raise
            except Exception as error:
                _settle(item, error=error)
            else:
                _settle(item, result=result)
            finally:
                item.state = WriteState.DONE
                self.running = None


def _settle(item: WriteItem, *, result: Any = None, error: BaseException | None = None) -> None:
    if item.outcome.done():
        return
    if error is not None:
        item.outcome.set_exception(error)
    else:
        item.outcome.set_result(result)


def _retrieve(outcome: asyncio.Future[Any]) -> None:
    """Mark the outcome's exception as seen, also when its caller was cancelled."""
    if not outcome.cancelled():
        outcome.exception()


class ReadPlaces:
    """At most ``size`` reads run against Telegram at once; the others wait for a place."""

    def __init__(self, size: int = READ_PLACES) -> None:
        self._places = asyncio.Semaphore(size)
        self.size = size
        self.in_use = 0

    @contextlib.asynccontextmanager
    async def place(self) -> AsyncIterator[None]:
        async with self._places:
            self.in_use += 1
            try:
                yield
            finally:
                self.in_use -= 1
