"""Helpers to run brokers in tests: in this process on a fake gateway, or as subprocesses."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from support.samples import fake_secrets, sample_chats
from telegram_mcp.broker.lock import read_lock
from telegram_mcp.broker.server import Broker
from telegram_mcp.gateway.fake import FAKE_CONFIG_NAME, FakeGateway
from telegram_mcp.paths import FixedPaths
from telegram_mcp.vault import SessionVault, StoredSession


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        """A sleep that steps this clock instead of waiting, for the broker and the fake."""
        self.advance(seconds)
        await asyncio.sleep(0)


async def wait_until(condition: Callable[[], bool], seconds: float = 2.0) -> None:
    """Poll ``condition`` until it holds; fail the test after ``seconds``."""
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail("the condition did not hold in time")
        await asyncio.sleep(0.01)


def save_fake_session() -> StoredSession:
    values = fake_secrets()
    stored = StoredSession(values.session, values.api_id, values.api_hash)
    SessionVault().save(stored)
    return stored


def make_broker(paths: FixedPaths, gateway: FakeGateway, **options: Any) -> Broker:
    return Broker(paths, gateway_factory=lambda session, api_id, api_hash: gateway, **options)


@contextlib.asynccontextmanager
async def running_broker(
    paths: FixedPaths, gateway: FakeGateway, **options: Any
) -> AsyncIterator[Broker]:
    broker = make_broker(paths, gateway, **options)
    await broker.start()
    try:
        yield broker
    finally:
        await broker.stop()


@contextlib.asynccontextmanager
async def broker_starter(
    paths: FixedPaths, gateway: FakeGateway, clock: FakeClock
) -> AsyncIterator[Callable[..., Awaitable[Broker]]]:
    """Start brokers on the fake gateway and ``clock``; every one is stopped at the end."""
    started: list[Broker] = []

    async def start(**options: Any) -> Broker:
        broker = make_broker(paths, gateway, clock=clock, **options)
        await broker.start()
        started.append(broker)
        return broker

    try:
        yield start
    finally:
        for broker in started:
            await broker.stop()


def write_fake_gateway_file(home: Path, **extra: Any) -> Path:
    """Describe the fake gateway a subprocess broker will use."""
    chats = [
        {
            "id": chat.id,
            "name": chat.name,
            "type": chat.type,
            "unread": chat.unread,
            "muted": chat.muted,
            "last_preview": chat.last_preview,
            "folder_ids": list(chat.folder_ids),
        }
        for chat in sample_chats()
    ]
    path = home / FAKE_CONFIG_NAME
    path.write_text(json.dumps({"chats": chats, **extra}))
    return path


def kill_subprocess_broker(paths: FixedPaths) -> None:
    content = read_lock(paths.lock) or {}
    pid = content.get("pid")
    if isinstance(pid, int) and pid != os.getpid() and content.get("state") == "running":
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)


@pytest.fixture
def subprocess_brokers(home: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], None]]:
    """Lets subprocess brokers use the fake gateway, and stops any broker left running."""
    monkeypatch.setenv("TELEGRAM_MCP_TEST_FAKE_GATEWAY", "1")
    write_fake_gateway_file(home)
    paths = FixedPaths.from_home(home)
    yield lambda: kill_subprocess_broker(paths)
    kill_subprocess_broker(paths)
