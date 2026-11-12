"""The activity log and the broker's diagnostics file (C8 activity_log, BR8.1, BR8.2).

Entries are built only from LogEntry's fields, any stored secret found in a field is replaced
with ``[redacted]``, and the secret part of an invite link in an entry's content with ``…``."""

from __future__ import annotations

import contextlib
import json
import os
import re
import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

REDACTED = "[redacted]"
FILE_MODE = 0o600
MIN_SECRET_LENGTH = 8
MAX_FILE_BYTES = 10_485_760
KEPT_FILES = 5


# BR15.2: the part of an invite link that grants access, after "t.me/+" or "joinchat/".
_INVITE_SECRET = re.compile(r"(t\.me/\+|joinchat/)[A-Za-z0-9_-]+")


def redact_invite_links(text: str) -> str:
    """Replace the secret part of every invite link with "…", so the log never holds a
    link that works (BR15.2)."""
    return _INVITE_SECRET.sub(r"\1…", text)


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
    """``activity.log`` and up to four older files, ``activity.log.1`` (newest) to
    ``activity.log.4`` (oldest). Before an entry would take ``activity.log`` past
    ``max_bytes``, the files move one place along and the entry starts a new file; an entry is
    never split between two files."""

    def __init__(
        self,
        path: Path,
        redactor: Redactor,
        *,
        max_bytes: int = MAX_FILE_BYTES,
        on_rotation_failure: Callable[[OSError], None] | None = None,
    ) -> None:
        self._path = path
        self._redactor = redactor
        self._max_bytes = max_bytes
        self._on_rotation_failure = on_rotation_failure

    def older_files(self) -> list[Path]:
        """The older files, newest first, as their names say (some may not exist)."""
        return [self._path.with_name(f"{self._path.name}.{n}") for n in range(1, KEPT_FILES)]

    def append(self, entry: LogEntry) -> None:
        """Append one JSON line in a single write, rotating first when it would not fit.
        Raises ``OSError`` only when the entry itself cannot be appended."""
        line = self.render(entry).encode("utf-8") + b"\n"
        self._rotate_if_needed(len(line))
        descriptor = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, FILE_MODE)
        try:
            os.fchmod(descriptor, FILE_MODE)
            written = os.write(descriptor, line)
        finally:
            os.close(descriptor)
        if written != len(line):
            raise OSError(f"only {written} of {len(line)} bytes of a log entry were written")

    def _rotate_if_needed(self, entry_bytes: int) -> None:
        try:
            size = os.lstat(self._path).st_size
        except FileNotFoundError:
            return
        except OSError as error:
            self._rotation_failed(error)
            return
        if size == 0 or size + entry_bytes <= self._max_bytes:
            return
        try:
            self._shift_files()
        except OSError as error:
            # The entry still goes into activity.log; the next entry tries again.
            self._rotation_failed(error)

    def _shift_files(self) -> None:
        """Delete the oldest file, move each older file one place along, then move
        activity.log to activity.log.1. Renames stay in the folder and follow no link."""
        older = self.older_files()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(older[-1])
        for source, target in zip(reversed(older[:-1]), reversed(older[1:]), strict=True):
            with contextlib.suppress(FileNotFoundError):
                os.replace(source, target)
        os.replace(self._path, older[0])

    def _rotation_failed(self, error: OSError) -> None:
        if self._on_rotation_failure is not None:
            self._on_rotation_failure(error)

    def render(self, entry: LogEntry) -> str:
        content = None
        if entry.content is not None:
            content = redact_invite_links(self._redactor.redact(entry.content))
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
