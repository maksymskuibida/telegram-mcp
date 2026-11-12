"""The hourly secret: swapped whole in one step, private, never written anywhere else, and a
failed swap keeps the old one."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from support.brokers import FakeClock, broker_starter, save_fake_session
from support.rpc import RawClient
from support.writes import confirm, list_chats, send
from telegram_mcp.broker import secret as secret_file
from telegram_mcp.broker.server import Broker
from telegram_mcp.gateway.fake import FakeGateway
from telegram_mcp.paths import FixedPaths

HOUR = 3600


@pytest.fixture
def paths(home: Path) -> FixedPaths:
    return FixedPaths.from_home(home)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def start_broker(
    paths: FixedPaths, fake_gateway: FakeGateway, clock: FakeClock
) -> AsyncIterator[Callable[..., Awaitable[Broker]]]:
    save_fake_session()
    async with broker_starter(paths, fake_gateway, clock) as start:
        yield start


async def hello(paths: FixedPaths, secret: str) -> dict[str, Any]:
    raw = await RawClient.open(paths)
    params = {"protocol": 1, "secret": secret, "client": "cli", "package_version": "test"}
    return await raw.call("hello", params)


def mode(path: Path) -> int:
    return os.lstat(path).st_mode & 0o777


async def test_the_secret_is_replaced_after_an_hour_whole_and_private(
    start_broker: Callable[..., Awaitable[Broker]], paths: FixedPaths, clock: FakeClock
) -> None:
    broker = await start_broker()
    old = paths.secret.read_text()
    clock.advance(HOUR - 1)
    broker.rotate_secret_if_due()
    assert paths.secret.read_text() == old
    clock.advance(1)
    broker.rotate_secret_if_due()
    new = paths.secret.read_text()
    assert new != old
    assert re.fullmatch(r"[0-9a-f]{64}", new)
    assert mode(paths.secret) == 0o600
    assert not paths.secret.is_symlink()
    assert list(paths.data_dir.glob("broker.secret.*.tmp")) == []
    assert "secret rotated" in paths.diagnostics.read_text()


async def test_after_a_rotation_the_old_secret_is_refused_and_open_connections_stay(
    start_broker: Callable[..., Awaitable[Broker]], paths: FixedPaths, clock: FakeClock
) -> None:
    broker = await start_broker()
    old = paths.secret.read_text()
    before = await RawClient.connect(paths)
    clock.advance(HOUR)
    broker.rotate_secret_if_due()
    assert (await list_chats(before))["ok"] is True
    assert (await before.result("broker.status"))["logged_in"] is True
    refused = await hello(paths, old)
    assert refused["error"]["message"] == "bad_secret"
    accepted = await hello(paths, paths.secret.read_text())
    assert "result" in accepted


async def test_a_link_planted_at_the_temporary_name_is_never_followed(
    start_broker: Callable[..., Awaitable[Broker]],
    paths: FixedPaths,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
) -> None:
    broker = await start_broker()
    old = paths.secret.read_text()
    target = home / "elsewhere.txt"
    target.write_text("untouched")
    planted = paths.data_dir / "broker.secret.planted.tmp"
    planted.symlink_to(target)
    monkeypatch.setattr(secret_file, "temporary_name", lambda: planted.name)
    clock.advance(HOUR)
    broker.rotate_secret_if_due()
    assert target.read_text() == "untouched"
    assert paths.secret.read_text() == old
    assert "result" in await hello(paths, old)
    assert "secret rotation failed (FileExistsError)" in paths.diagnostics.read_text()


async def test_a_failed_rotation_keeps_the_old_secret_and_tries_again_an_hour_later(
    start_broker: Callable[..., Awaitable[Broker]], paths: FixedPaths, clock: FakeClock
) -> None:
    broker = await start_broker()
    old = paths.secret.read_text()
    os.chmod(paths.data_dir, 0o500)
    try:
        clock.advance(HOUR)
        broker.rotate_secret_if_due()
        assert paths.secret.read_text() == old
        assert "result" in await hello(paths, old)
        assert "the old secret stays in force" in paths.diagnostics.read_text()
    finally:
        os.chmod(paths.data_dir, 0o700)
    clock.advance(HOUR - 1)
    broker.rotate_secret_if_due()
    assert paths.secret.read_text() == old
    clock.advance(1)
    broker.rotate_secret_if_due()
    assert paths.secret.read_text() != old


async def test_temporary_secret_files_left_behind_are_removed_at_start(
    start_broker: Callable[..., Awaitable[Broker]], paths: FixedPaths
) -> None:
    paths.data_dir.mkdir(parents=True, mode=0o700)
    leftover = paths.data_dir / "broker.secret.0123456789abcdef.tmp"
    leftover.write_text("half")
    await start_broker()
    assert not leftover.exists()


async def test_no_secret_ever_reaches_the_log_diagnostics_or_a_result(
    start_broker: Callable[..., Awaitable[Broker]],
    paths: FixedPaths,
    clock: FakeClock,
) -> None:
    broker = await start_broker()
    seen = [paths.secret.read_text()]
    results: list[Any] = []
    for round_number in range(2):
        server = await RawClient.connect(paths)
        await confirm(paths, f"round {round_number}")
        results.append(await list_chats(server))
        results.append(await send(server, f"round {round_number}"))
        results.append(await server.result("broker.status"))
        clock.advance(HOUR)
        broker.rotate_secret_if_due()
        seen.append(paths.secret.read_text())
    assert len(set(seen)) == 3
    written = paths.log.read_text() + paths.diagnostics.read_text() + repr(results)
    for value in seen:
        assert value not in written


@pytest.mark.parametrize("failing", ["write", "replace"])
def test_a_secret_that_cannot_be_written_whole_leaves_the_file_and_no_temporary(
    home: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    path = home / "broker.secret"
    path.write_text("old")
    if failing == "write":
        monkeypatch.setattr(os, "write", lambda descriptor, data: 0)
    else:

        def refuse(source: Any, target: Any) -> None:
            raise PermissionError("cannot rename")

        monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError, match=r"not written whole|cannot rename"):
        secret_file.write_secret(path, secret_file.new_secret())
    monkeypatch.undo()
    assert path.read_text() == "old"
    assert list(home.glob("broker.secret.*.tmp")) == []


async def test_a_rotation_is_not_activity(
    start_broker: Callable[..., Awaitable[Broker]], paths: FixedPaths, clock: FakeClock
) -> None:
    broker = await start_broker(secret_rotation=300)
    clock.advance(300)
    broker.sweep()
    clock.advance(300)
    broker.sweep()
    # Two rotations, no request: the broker still stops ten minutes after its start.
    assert paths.diagnostics.read_text().count("secret rotated") == 2
    await asyncio.wait_for(broker.wait_stopped(), 3)
    assert not paths.secret.exists()
