"""Log rotation: a new file before an entry would pass 10 MB, five files at most, private
modes, whole entries in time order, and nothing lost when a rename fails."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from telegram_mcp.activity_log import (
    MAX_FILE_BYTES,
    ActivityLog,
    Diagnostics,
    LogEntry,
    Redactor,
)
from telegram_mcp.broker.server import Broker
from telegram_mcp.gateway.fake import FakeGateway
from telegram_mcp.paths import FixedPaths

SMALL = 1000


@pytest.fixture
def folder(home: Path) -> Iterator[Path]:
    path = home / "data"
    path.mkdir(mode=0o700)
    previous = os.umask(0o022)
    try:
        yield path
    finally:
        os.umask(previous)


def entry(number: int, padding: int = 100) -> LogEntry:
    content = f"{number}:" + "x" * padding
    return LogEntry("mcp-server:claude-code", "call", "tg_chats_list", "ok", content=content)


def all_files(log_path: Path) -> list[Path]:
    """Oldest first: activity.log.4 to .1, then activity.log."""
    older = [log_path.with_name(f"{log_path.name}.{n}") for n in range(4, 0, -1)]
    return [path for path in [*older, log_path] if path.exists()]


def read_numbers(log_path: Path) -> list[int]:
    numbers = []
    for path in all_files(log_path):
        for line in path.read_text(encoding="utf-8").splitlines():
            numbers.append(int(json.loads(line)["content"].split(":")[0]))
    return numbers


def test_the_log_moves_on_only_when_the_next_entry_would_pass_the_limit(folder: Path) -> None:
    path = folder / "activity.log"
    log = ActivityLog(path, Redactor(), max_bytes=SMALL)
    log.append(entry(0))
    size = path.stat().st_size
    fits = SMALL - size
    padding = 100 + fits - size  # an entry of exactly the space that is left
    log.append(entry(1, padding))
    assert path.stat().st_size == SMALL
    assert not path.with_name("activity.log.1").exists()
    log.append(entry(2))
    assert path.with_name("activity.log.1").stat().st_size == SMALL
    assert read_numbers(path) == [0, 1, 2]
    assert MAX_FILE_BYTES == 10_485_760


def test_six_rotations_keep_five_private_files(folder: Path) -> None:
    path = folder / "activity.log"
    log = ActivityLog(path, Redactor(), max_bytes=SMALL)
    # Two entries fit in a file, a third does not: 14 entries make six rotations.
    for number in range(14):
        log.append(entry(number, 300))
    names = sorted(p.name for p in folder.iterdir())
    assert names == [f"activity.log{suffix}" for suffix in ("", ".1", ".2", ".3", ".4")]
    assert {p.stat().st_mode & 0o777 for p in folder.iterdir()} == {0o600}
    assert read_numbers(path) == list(range(4, 14))


def test_entries_are_never_split_and_read_oldest_first_in_time_order(folder: Path) -> None:
    path = folder / "activity.log"
    log = ActivityLog(path, Redactor(), max_bytes=SMALL)
    for number in range(40):
        log.append(entry(number, 37 * (number % 7)))
    for file in all_files(path):
        text = file.read_text(encoding="utf-8")
        assert text.endswith("\n")
        assert all(json.loads(line) for line in text.splitlines())
        assert file.stat().st_size <= SMALL
    numbers = read_numbers(path)
    assert numbers == sorted(numbers)
    assert numbers[-1] == 39


def test_an_entry_bigger_than_the_limit_starts_a_file_of_its_own(folder: Path) -> None:
    path = folder / "activity.log"
    log = ActivityLog(path, Redactor(), max_bytes=SMALL)
    log.append(entry(0))
    log.append(entry(1, SMALL * 2))
    log.append(entry(2))
    assert len(path.with_name("activity.log.1").read_text().splitlines()) == 1
    assert read_numbers(path) == [0, 1, 2]


def test_a_failed_rename_keeps_the_entry_and_the_next_entry_tries_again(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = folder / "activity.log"
    failures: list[OSError] = []
    log = ActivityLog(path, Redactor(), max_bytes=SMALL, on_rotation_failure=failures.append)
    log.append(entry(0, SMALL - 200))
    real_replace = os.replace
    calls = {"count": 0}

    def fail_once(source: Any, target: Any) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise PermissionError("cannot rename")
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_once)
    log.append(entry(1, 300))
    assert len(failures) == 1
    assert path.stat().st_size > SMALL
    assert read_numbers(path) == [0, 1]
    log.append(entry(2))
    assert path.with_name("activity.log.1").exists()
    assert read_numbers(path) == [0, 1, 2]


def test_a_log_whose_size_cannot_be_read_still_takes_the_entry(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = folder / "activity.log"
    log = ActivityLog(path, Redactor(), max_bytes=SMALL)  # no failure listener given
    log.append(entry(0))

    def refuse(target: Any) -> Any:
        raise PermissionError("cannot read the size")

    monkeypatch.setattr(os, "lstat", refuse)
    log.append(entry(1))
    monkeypatch.undo()
    assert read_numbers(path) == [0, 1]


def test_a_rotation_failure_is_named_in_the_broker_diagnostics(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = FixedPaths.from_home(home)
    paths.data_dir.mkdir(parents=True, mode=0o700)
    broker = Broker(paths, gateway_factory=lambda s, i, h: FakeGateway())
    Diagnostics(paths.diagnostics, Redactor()).start()
    paths.log.write_bytes(b"x" * MAX_FILE_BYTES)

    def refuse(source: Any, target: Any) -> None:
        raise PermissionError("cannot rename")

    monkeypatch.setattr(os, "replace", refuse)
    broker.log.append(entry(1))
    assert paths.log.read_bytes().endswith(b"\n")
    assert "activity log rotation failed (PermissionError)" in paths.diagnostics.read_text()


def test_rotating_five_full_files_takes_under_100_ms(folder: Path) -> None:
    path = folder / "activity.log"
    for file in [path, *[path.with_name(f"activity.log.{n}") for n in range(1, 5)]]:
        with file.open("wb") as handle:
            handle.truncate(MAX_FILE_BYTES)
        file.chmod(0o600)
    log = ActivityLog(path, Redactor())
    started = time.perf_counter()
    log.append(entry(1))
    elapsed = time.perf_counter() - started
    assert elapsed < 0.1
    assert path.stat().st_size < 1000
    assert path.with_name("activity.log.1").stat().st_size == MAX_FILE_BYTES
    print(f"log rotation of five 10 MB files: {elapsed * 1000:.2f} ms")
