"""The activity log, redaction and diagnostics (BR8.1, BR8.2, NFR1.5, NFR7.5, NFR7.6)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from support.samples import fake_secrets
from telegram_mcp.activity_log import REDACTED, ActivityLog, Diagnostics, LogEntry, Redactor

C8_FIELDS = {
    "ts",
    "client",
    "event",
    "tool",
    "chat_id",
    "approval_id",
    "content",
    "decision",
    "result",
}


@pytest.fixture
def log_path(home: Path) -> Path:
    return home / "activity.log"


def test_each_entry_is_one_json_line_with_the_c8_fields(log_path: Path) -> None:
    log = ActivityLog(log_path, Redactor())
    when = datetime(2026, 11, 11, 10, 0, 0, tzinfo=UTC)
    log.append(LogEntry("mcp-server:claude-code", "call", "tg_messages_send", "ok", ts=when))
    log.append(
        LogEntry(
            "approval-hook",
            "decision",
            "tg_messages_send",
            "ok",
            chat_id=101,
            approval_id="abcdefghij",
            decision="allowed",
        )
    )
    lines = log_path.read_text().splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert set(first) == C8_FIELDS
    assert first["ts"] == "2026-11-11T10:00:00.000Z"
    assert second["decision"] == "allowed"
    assert second["chat_id"] == 101


def test_the_log_is_created_0600_and_only_appended(log_path: Path) -> None:
    log_path.write_text("existing\n")
    os.chmod(log_path, 0o600)
    ActivityLog(log_path, Redactor()).append(LogEntry("cli", "result", "x", "ok"))
    assert log_path.read_text().startswith("existing\n")
    fresh = log_path.with_name("fresh.log")
    ActivityLog(fresh, Redactor()).append(LogEntry("cli", "result", "x", "ok"))
    assert fresh.stat().st_mode & 0o777 == 0o600


def test_secrets_in_any_field_are_redacted(log_path: Path) -> None:
    values = fake_secrets()
    log = ActivityLog(log_path, Redactor([values.session, values.api_hash]))
    log.append(
        LogEntry(
            "cli",
            "call",
            "tg_messages_send",
            f"telegram_error {values.session}",
            content=f"my hash is {values.api_hash}!",
        )
    )
    text = log_path.read_text()
    assert values.session not in text
    assert values.api_hash not in text
    assert f"my hash is {REDACTED}!" in text


def test_short_or_empty_values_are_not_treated_as_secrets() -> None:
    redactor = Redactor(["", "ab"])
    assert redactor.redact("ab cd") == "ab cd"


def test_a_failed_write_raises(home: Path) -> None:
    log = ActivityLog(home / "missing-folder" / "activity.log", Redactor())
    with pytest.raises(OSError):  # noqa: PT011 - any OSError means the entry was not written
        log.append(LogEntry("cli", "call", "x", "ok"))


def test_diagnostics_are_truncated_at_start_redacted_and_0600(home: Path) -> None:
    values = fake_secrets()
    path = home / "broker.err"
    path.write_text("old run\n")
    diagnostics = Diagnostics(path, Redactor([values.api_hash]))
    diagnostics.start()
    try:
        raise RuntimeError(f"boom with {values.api_hash}")
    except RuntimeError as error:
        diagnostics.exception("tool.call failed", error)
    text = path.read_text()
    assert "old run" not in text
    assert "tool.call failed: RuntimeError: boom with [redacted]" in text
    assert values.api_hash not in text
    assert path.stat().st_mode & 0o777 == 0o600


def test_diagnostics_never_raise_when_the_file_cannot_be_written(home: Path) -> None:
    diagnostics = Diagnostics(home / "no" / "such" / "broker.err", Redactor())
    diagnostics.write("still fine")
    diagnostics.exception("context", ValueError("no traceback"))
