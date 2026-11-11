"""The broker lock (BR1.4, BR2.2, C8 lock, NFR2.6)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from telegram_mcp.broker.lock import (
    BrokerLock,
    LockProbe,
    LockState,
    probe_lock,
    read_lock,
)

HOLDER = Path(__file__).parent / "support" / "hold_lock.py"


@pytest.fixture
def lock_path(home: Path) -> Path:
    return home / "broker.lock"


@pytest.fixture
def processes() -> Iterator[list[subprocess.Popen[str]]]:
    started: list[subprocess.Popen[str]] = []
    yield started
    for process in started:
        if process.poll() is None:
            process.kill()
        process.wait()


def hold_elsewhere(
    lock_path: Path, state: str, processes: list[subprocess.Popen[str]], seconds: float = 30
) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [sys.executable, str(HOLDER), str(lock_path), state, str(seconds)],
        stdout=subprocess.PIPE,
        text=True,
    )
    processes.append(process)
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "held"
    return process


def test_a_fresh_lock_is_created_0600_with_its_content(lock_path: Path) -> None:
    lock = BrokerLock(lock_path)
    assert lock.try_hold(LockState.RUNNING)
    try:
        assert lock_path.stat().st_mode & 0o777 == 0o600
        content = read_lock(lock_path)
        assert content is not None
        assert content["pid"] == os.getpid()
        assert content["state"] == "running"
        assert content["protocol"] == 1
        assert content["started_at"].endswith("Z")
        assert probe_lock(lock_path) is LockProbe.RUNNING
    finally:
        lock.release()


def test_a_lock_held_by_a_live_process_is_refused(
    lock_path: Path, processes: list[subprocess.Popen[str]]
) -> None:
    hold_elsewhere(lock_path, "running", processes)
    assert probe_lock(lock_path) is LockProbe.RUNNING
    assert not BrokerLock(lock_path).try_hold(LockState.RUNNING)


def test_a_killed_holder_leaves_the_lock_free_to_take_over(
    lock_path: Path, processes: list[subprocess.Popen[str]]
) -> None:
    holder = hold_elsewhere(lock_path, "running", processes)
    holder.send_signal(signal.SIGKILL)
    holder.wait()
    assert probe_lock(lock_path) is LockProbe.FREE
    lock = BrokerLock(lock_path)
    assert lock.try_hold(LockState.RUNNING)
    content = read_lock(lock_path)
    assert content is not None
    assert content["pid"] == os.getpid()
    lock.release()


def test_a_login_holding_the_lock_is_reported_as_login(
    lock_path: Path, processes: list[subprocess.Popen[str]]
) -> None:
    hold_elsewhere(lock_path, "login", processes)
    assert probe_lock(lock_path) is LockProbe.LOGIN


def test_a_released_lock_is_free_and_never_deleted(lock_path: Path) -> None:
    lock = BrokerLock(lock_path)
    assert lock.try_hold(LockState.LOGIN)
    lock.release()
    lock.release()
    assert lock_path.exists()
    content = read_lock(lock_path)
    assert content is not None
    assert content["state"] == "released"
    assert probe_lock(lock_path) is LockProbe.FREE
    with pytest.raises(RuntimeError):
        lock.set_state(LockState.RUNNING)


def test_a_crashed_login_leaves_the_lock_free(
    lock_path: Path, processes: list[subprocess.Popen[str]]
) -> None:
    holder = hold_elsewhere(lock_path, "login", processes)
    holder.send_signal(signal.SIGKILL)
    holder.wait()
    content = read_lock(lock_path)
    assert content is not None
    assert content["state"] == "login"
    assert probe_lock(lock_path) is LockProbe.FREE


def test_ten_concurrent_holders_yield_one(
    lock_path: Path, processes: list[subprocess.Popen[str]]
) -> None:
    started = [
        subprocess.Popen(
            [sys.executable, str(HOLDER), str(lock_path), "running", "5"],
            stdout=subprocess.PIPE,
            text=True,
        )
        for _ in range(10)
    ]
    processes.extend(started)
    outcomes = []
    for process in started:
        assert process.stdout is not None
        outcomes.append(process.stdout.readline().strip())
    assert outcomes.count("held") == 1
    assert outcomes.count("refused") == 9


def test_a_probe_never_keeps_the_lock_and_a_missing_file_is_free(lock_path: Path) -> None:
    assert probe_lock(lock_path) is LockProbe.FREE
    assert read_lock(lock_path) is None
    lock_path.write_text("not json")
    assert probe_lock(lock_path) is LockProbe.FREE
    lock = BrokerLock(lock_path)
    assert lock.try_hold(LockState.RUNNING)
    lock.release()
