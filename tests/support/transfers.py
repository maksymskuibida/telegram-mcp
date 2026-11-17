"""A clock that moves only when a test moves it, and helpers to follow background transfers.

The transfer queue measures its stall limit, its part limit and the final request's limit on the
broker's clock and waits with the broker's sleep. With :class:`ManualClock` as both, a part that
hangs stays in flight until the test moves the clock, so every limit is reached on the clock,
never by sleeping."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from support.catalog import call
from support.rpc import RawClient

LIST = "tg_transfers_list"
CANCEL = "tg_transfers_cancel"


class ManualClock:
    """``sleep(seconds)`` waits until the test has advanced the clock past its end."""

    def __init__(self) -> None:
        self.now = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
        self._waiters: list[tuple[datetime, asyncio.Future[None]]] = []

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        due = [(at, waiter) for at, waiter in self._waiters if at <= self.now]
        self._waiters = [(at, waiter) for at, waiter in self._waiters if at > self.now]
        for _, waiter in due:
            if not waiter.done():
                waiter.set_result(None)

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append((self.now + timedelta(seconds=seconds), waiter))
        await waiter

    @property
    def sleepers(self) -> int:
        return sum(1 for _, waiter in self._waiters if not waiter.done())


async def settle() -> None:
    """Let every task that can run, run (parts, the write line, the end of a transfer)."""
    for _ in range(5):
        await asyncio.sleep(0.002)


async def drive(
    clock: ManualClock, until: Callable[[], bool], *, step: float = 1.0, limit: float = 900.0
) -> float:
    """Move the clock ``step`` seconds at a time until ``until()`` holds; the seconds moved.
    Before each move it lets work that is not waiting on the clock (a part's disk read or write
    on a thread) reach its next wait, so a slow machine does not move the clock early."""
    moved = 0.0
    await settle()
    while not until():
        if moved >= limit:
            pytest.fail(f"the condition did not hold within {limit} s on the clock")
        await _parked(clock, until)
        clock.advance(step)
        moved += step
        await settle()
    return moved


async def _parked(clock: ManualClock, until: Callable[[], bool], seconds: float = 0.25) -> None:
    """Wait (real time, at most ``seconds``) until something waits on the clock."""
    for _ in range(int(seconds / 0.002)):
        if clock.sleepers or until():
            return
        await asyncio.sleep(0.002)


async def transfer(client: RawClient, transfer_id: str) -> dict[str, Any]:
    """The transfer's record, read with tg_transfers_list."""
    listed = await call(client, LIST, {"transfer_id": transfer_id})
    assert listed["ok"], listed
    (record,) = listed["data"]["transfers"]
    assert isinstance(record, dict)
    return record
