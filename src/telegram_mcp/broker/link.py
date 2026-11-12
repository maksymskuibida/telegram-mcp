"""The broker's one Telegram connection and how it is opened again.

However many calls need Telegram at once, there is at most one connection and at most one
attempt to open it: every call that arrives while it is opening waits for that same attempt.
The attempt runs as its own task, and callers wait through ``asyncio.shield``, so a caller
that gives up (an approval check reaching its budget) never cancels it for the others.

After a failed connect, calls fail at once for 1 s, then 2, 4, 8, 16 and at most 30 s after
each further failure in a row; the first call after the wait tries again, and a success resets
the count. A session Telegram no longer accepts is not a connect failure: the gateway is closed
and dropped, and the next call builds a new one from the session read again."""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum

from telegram_mcp.gateway.models import GatewayError, NotAuthorized
from telegram_mcp.gateway.protocol import GatewayFactory, TelegramGateway
from telegram_mcp.vault import StoredSession

CONNECT_LIMIT_SECONDS = 30.0
LONGEST_BACKOFF_SECONDS = 30
DISCONNECT_LIMIT_SECONDS = 2.0


class LinkState(StrEnum):
    CLOSED = "closed"
    CONNECTING = "connecting"
    OPEN = "open"
    BACKING_OFF = "backing_off"


class Unreachable(Exception):
    """Telegram cannot be reached now; ``retry_after`` says when the next attempt may run."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"Telegram could not be reached; trying again in {retry_after} s")
        self.retry_after = retry_after


class SessionRefused(Exception):
    """Telegram no longer accepts the stored session."""


def backoff_seconds(failures_in_a_row: int) -> int:
    """1, 2, 4, 8, 16, then 30 s for every further failure."""
    return min(LONGEST_BACKOFF_SECONDS, int(2 ** max(0, failures_in_a_row - 1)))


class TelegramLink:
    def __init__(
        self,
        factory: GatewayFactory,
        *,
        clock: Callable[[], datetime],
        connect_limit: float = CONNECT_LIMIT_SECONDS,
        report: Callable[[str], None] | None = None,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._limit = connect_limit
        self._report = report or (lambda message: None)
        self._gateway: TelegramGateway | None = None
        self._attempt: asyncio.Task[TelegramGateway] | None = None
        self.state = LinkState.CLOSED
        self.failures_in_a_row = 0
        self.retry_at: datetime | None = None
        self.connects_started = 0

    async def open(self, stored: StoredSession) -> TelegramGateway:
        """The open gateway, with the session checked. Raises ``Unreachable`` during a
        back-off or when the shared attempt fails, ``SessionRefused`` when Telegram no longer
        accepts the session."""
        if self.state is LinkState.OPEN and self._gateway is not None:
            gateway = await self._still_open(self._gateway)
            if gateway is not None:
                return gateway
        self._refuse_during_backoff()
        if self._attempt is None:
            self.connects_started += 1
            self._attempt = asyncio.get_running_loop().create_task(self._connect(stored))
            self._attempt.add_done_callback(_retrieve)
        return await asyncio.shield(self._attempt)

    def mark_dropped(self) -> None:
        """The connection broke (a call timed out or lost it): the next call opens it again."""
        if self.state is LinkState.OPEN:
            self.state = LinkState.CLOSED

    async def close(self, limit: float = DISCONNECT_LIMIT_SECONDS) -> None:
        """Disconnect from Telegram, waiting at most ``limit`` seconds."""
        if self._attempt is not None:
            self._attempt.cancel()
            with contextlib.suppress(BaseException):
                await self._attempt
            self._attempt = None
        await self._discard(limit)

    async def _still_open(self, gateway: TelegramGateway) -> TelegramGateway | None:
        try:
            async with asyncio.timeout(self._limit):
                authorized = await gateway.is_authorized()
        except NotAuthorized:
            authorized = False
        except (GatewayError, TimeoutError, OSError):
            self.mark_dropped()
            return None
        if not authorized:
            await self._discard()
            raise SessionRefused()
        return gateway

    def _refuse_during_backoff(self) -> None:
        if self.state is not LinkState.BACKING_OFF or self.retry_at is None:
            return
        left = (self.retry_at - self._clock()).total_seconds()
        if left > 0:
            raise Unreachable(math.ceil(left))

    async def _connect(self, stored: StoredSession) -> TelegramGateway:
        self.state = LinkState.CONNECTING
        try:
            if self._gateway is None:
                self._gateway = self._factory(stored.session, stored.api_id, stored.api_hash)
            gateway = self._gateway
            async with asyncio.timeout(self._limit):
                await gateway.connect()
                authorized = await gateway.is_authorized()
        except NotAuthorized:
            authorized = False
        except (GatewayError, TimeoutError, OSError) as error:
            self._attempt = None
            raise self._failed(error) from error
        except asyncio.CancelledError:
            self._attempt = None
            self.state = LinkState.CLOSED
            raise
        self._attempt = None
        if not authorized:
            await self._discard()
            raise SessionRefused()
        self._opened()
        return gateway

    def _failed(self, error: BaseException) -> Unreachable:
        self.failures_in_a_row += 1
        wait = backoff_seconds(self.failures_in_a_row)
        self.retry_at = self._clock() + timedelta(seconds=wait)
        self.state = LinkState.BACKING_OFF
        self._report(
            f"connect to Telegram failed ({type(error).__name__}, {self.failures_in_a_row} in a "
            f"row); calls fail at once for {wait} s"
        )
        return Unreachable(wait)

    def _opened(self) -> None:
        if self.failures_in_a_row:
            self._report(f"Telegram reachable again after {self.failures_in_a_row} failed connects")
        self.failures_in_a_row = 0
        self.retry_at = None
        self.state = LinkState.OPEN

    async def _discard(self, limit: float = DISCONNECT_LIMIT_SECONDS) -> None:
        gateway, self._gateway = self._gateway, None
        self.state = LinkState.CLOSED
        if gateway is None:
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(gateway.disconnect(), limit)


def _retrieve(task: asyncio.Task[TelegramGateway]) -> None:
    """Mark the attempt's failure as seen, also when every caller gave up waiting."""
    if not task.cancelled():
        task.exception()
