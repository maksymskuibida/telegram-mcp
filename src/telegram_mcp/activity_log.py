"""The activity log and the broker's diagnostics file (C8 activity_log, BR8.1, BR8.2).

Entries are built only from LogEntry's fields, and any stored secret found in a field is
replaced with ``[redacted]``."""

from __future__ import annotations

import json
import os
import traceback
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

REDACTED = "[redacted]"
FILE_MODE = 0o600
MIN_SECRET_LENGTH = 8

EventKind = Literal["call", "prompt", "decision", "result"]
DecisionKind = Literal["allowed", "declined", "timed_out"]


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass(frozen=True)
class LogEntry:
    client: str
    event: EventKind
    tool: str
    result: str
    chat_id: int | None = None
    approval_id: str | None = None
    content: str | None = None
    decision: DecisionKind | None = None
    ts: datetime | None = None


class Redactor:
    """Replaces the stored session and api_hash values wherever they appear."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets: tuple[str, ...] = ()
        self.set_secrets(secrets)

    def set_secrets(self, secrets: Iterable[str]) -> None:
        unique = {s for s in secrets if s and len(s) >= MIN_SECRET_LENGTH}
        self._secrets = tuple(sorted(unique, key=len, reverse=True))

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text


class ActivityLog:
    def __init__(self, path: Path, redactor: Redactor) -> None:
        self._path = path
        self._redactor = redactor

    def append(self, entry: LogEntry) -> None:
        """Append one JSON line in a single write. Raises ``OSError`` when it cannot."""
        line = self.render(entry).encode("utf-8") + b"\n"
        descriptor = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, FILE_MODE)
        try:
            os.fchmod(descriptor, FILE_MODE)
            written = os.write(descriptor, line)
        finally:
            os.close(descriptor)
        if written != len(line):
            raise OSError(f"only {written} of {len(line)} bytes of a log entry were written")

    def render(self, entry: LogEntry) -> str:
        content = self._redactor.redact(entry.content) if entry.content is not None else None
        fields = {
            "ts": iso_utc(entry.ts or utc_now()),
            "client": self._redactor.redact(entry.client),
            "event": entry.event,
            "tool": self._redactor.redact(entry.tool),
            "chat_id": entry.chat_id,
            "approval_id": entry.approval_id,
            "content": content,
            "decision": entry.decision,
            "result": self._redactor.redact(entry.result),
        }
        return json.dumps(fields, ensure_ascii=False, separators=(",", ":"))


class Diagnostics:
    """The broker's ``broker.err``: start, stop, lock take-over, settings refusals and
    unexpected exceptions, redacted. Truncated when the broker starts (NFR7.6)."""

    def __init__(self, path: Path, redactor: Redactor) -> None:
        self._path = path
        self._redactor = redactor

    def start(self) -> None:
        descriptor = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        try:
            os.fchmod(descriptor, FILE_MODE)
        finally:
            os.close(descriptor)

    def write(self, message: str) -> None:
        line = f"{iso_utc(utc_now())} {self._redactor.redact(message)}\n".encode()
        try:
            descriptor = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, FILE_MODE)
            try:
                os.write(descriptor, line)
            finally:
                os.close(descriptor)
        except OSError:
            return  # diagnostics must never take the broker down

    def exception(self, context: str, error: BaseException) -> None:
        where = traceback.extract_tb(error.__traceback__)[-1:] if error.__traceback__ else []
        location = f" at {where[0].filename}:{where[0].lineno}" if where else ""
        self.write(f"{context}: {type(error).__name__}: {error}{location}")
