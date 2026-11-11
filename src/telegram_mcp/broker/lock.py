"""The single-broker lock (C8 lock; rules BR1.4, BR2.2).

The exclusive flock decides who holds the lock; the JSON content only explains it. Only a
broker process or the login command ever holds the flock. Callers only test it, and release
the test at once. The file is never deleted."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from enum import StrEnum
from pathlib import Path
from typing import Any

from telegram_mcp.activity_log import iso_utc, utc_now

PROTOCOL_VERSION = 1
LOCK_MODE = 0o600
HOLD_ATTEMPTS = 10
HOLD_PAUSE_SECONDS = 0.02


class LockState(StrEnum):
    RUNNING = "running"
    LOGIN = "login"
    RELEASED = "released"


class LockProbe(StrEnum):
    FREE = "free"
    RUNNING = "running"
    LOGIN = "login"


class BrokerLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._descriptor: int | None = None

    @property
    def held(self) -> bool:
        return self._descriptor is not None

    def try_hold(self, state: LockState) -> bool:
        """Take the flock without blocking and write this process's pid and ``state``.

        A caller's probe holds the flock for an instant, so a few short retries keep a probe
        from making a starting broker give up. False means another broker or a login holds it.
        """
        descriptor = _open_lock(self._path)
        for attempt in range(HOLD_ATTEMPTS):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if attempt == HOLD_ATTEMPTS - 1:
                    os.close(descriptor)
                    return False
                time.sleep(HOLD_PAUSE_SECONDS)
        os.fchmod(descriptor, LOCK_MODE)
        self._descriptor = descriptor
        self.set_state(state)
        return True

    def set_state(self, state: LockState) -> None:
        if self._descriptor is None:
            raise RuntimeError("the lock is not held")
        content = {
            "pid": os.getpid(),
            "started_at": iso_utc(utc_now()),
            "protocol": PROTOCOL_VERSION,
            "state": str(state),
        }
        data = json.dumps(content).encode("utf-8")
        os.ftruncate(self._descriptor, 0)
        os.pwrite(self._descriptor, data, 0)

    def release(self) -> None:
        """Write ``released`` and drop the flock. The file stays."""
        if self._descriptor is None:
            return
        descriptor = self._descriptor
        try:
            self.set_state(LockState.RELEASED)
        finally:
            self._descriptor = None
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _open_lock(path: Path) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW
    try:
        return os.open(path, flags | os.O_CREAT | os.O_EXCL, LOCK_MODE)
    except FileExistsError:
        return os.open(path, flags)


def read_lock(path: Path) -> dict[str, Any] | None:
    """The lock's content, or None when the file is missing or unreadable."""
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return content if isinstance(content, dict) else None


def probe_lock(path: Path) -> LockProbe:
    """What a caller sees: free (no file, released, or the holder died), or who holds it.
    The test flock is released at once; callers never hold the lock."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return LockProbe.FREE
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            content = read_lock(path) or {}
            return LockProbe.LOGIN if content.get("state") == "login" else LockProbe.RUNNING
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        return LockProbe.FREE
    finally:
        os.close(descriptor)
