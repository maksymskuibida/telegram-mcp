"""`telegram-mcp log [--lines N] [--follow] [--json]`: print the activity log.

The command reads the fixed files ``activity.log.4`` to ``.1`` and ``activity.log`` directly,
read-only, line by line: no broker, no Keychain, no Telegram, and nothing is ever written to
the data folder. Logged text came from Telegram and from the AI, so every control or format
character is shown as a visible escape and can never drive the terminal.

The one-shot read is a snapshot: ``activity.log`` is opened first, then ``.1`` to ``.4``, and a
file whose inode was already opened is skipped, so a rotation during the read neither repeats
nor drops an entry. ``--follow`` then keeps reading the open ``activity.log`` and, when the
path's inode changes, finishes the old file before starting on the new one."""

from __future__ import annotations

import functools
import json
import os
import re
import sys
import time
import unicodedata
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from telegram_mcp.paths import FixedPaths

ROTATED = 4
POLL_SECONDS = 0.5
UNREADABLE = "(unreadable entry) "
_QUIET_RESULTS = ("call", "prompt", "decision")  # their result is always "ok"

Opener = Callable[[Path], BinaryIO]
Say = Callable[[str], None]


@dataclass(frozen=True)
class LogOptions:
    lines: int | None = None
    follow: bool = False
    as_json: bool = False


class LogUnreadable(Exception):
    def __init__(self, path: Path, error: OSError) -> None:
        super().__init__(f"The log could not be read ({path}: {error.strerror or error}).")


def log_files(paths: FixedPaths) -> list[Path]:
    """The only files the command reads, newest first: activity.log, then .1 to .4."""
    rotated = [paths.log.with_name(f"{paths.log.name}.{n}") for n in range(1, ROTATED + 1)]
    return [paths.log, *rotated]


# Escaping ------------------------------------------------------------------------------------


@functools.cache
def _hidden() -> re.Pattern[str]:
    """Every control (Cc) and format (Cf) character, the line and paragraph separators and
    lone surrogates, except a line feed and a tab: one compiled character class."""
    points = [
        c
        for c in range(sys.maxunicode + 1)
        if unicodedata.category(chr(c)) in ("Cc", "Cf") and chr(c) not in "\n\t"
    ]
    points += [0x2028, 0x2029]
    ranges = "".join(re.escape(chr(c)) for c in sorted(set(points)))
    return re.compile(f"[{ranges}\\ud800-\\udfff]")


@functools.cache
def _readable_hidden() -> re.Pattern[str]:
    return re.compile(f"{_hidden().pattern}|\\\\")


def _visible(match: re.Match[str]) -> str:
    char = match.group()
    if char == "\\":
        return "\\\\"
    code = ord(char)
    if code <= 0xFF:
        return f"\\x{code:02x}"
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


def escape_text(text: str) -> str:
    """Text for the readable view: hidden characters and the backslash as visible escapes."""
    return _readable_hidden().sub(_visible, text)


def escape_field(text: str) -> str:
    """A field of an entry's first line: like ``escape_text``, and a line feed or a tab is
    shown as ``\\n`` or ``\\t`` too, so no field can start a line of its own and pass for
    another entry."""
    return escape_text(text).replace("\n", "\\n").replace("\t", "\\t")


def _json_escape(match: re.Match[str]) -> str:
    units = match.group().encode("utf-16-be", "surrogatepass")
    return "".join(f"\\u{int.from_bytes(units[i : i + 2]):04x}" for i in range(0, len(units), 2))


def json_line(value: Any) -> str:
    """One JSON line with every hidden character inside its strings as a \\uNNNN escape."""
    return _hidden().sub(_json_escape, json.dumps(value, ensure_ascii=False))


# Rendering -----------------------------------------------------------------------------------


def parse_entry(line: str) -> dict[str, Any] | None:
    """The entry, or None when the line is not a JSON object with ts, event and tool."""
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    if not isinstance(entry, dict):
        return None
    if not all(isinstance(entry.get(key), str) for key in ("ts", "event", "tool")):
        return None
    try:
        datetime.fromisoformat(entry["ts"])
    except ValueError:
        return None
    return entry


def render(line: str, as_json: bool) -> str:
    entry = parse_entry(line)
    if as_json:
        return json_line(entry if entry is not None else {"unreadable": line})
    if entry is None:
        return UNREADABLE + escape_text(line)
    return readable(entry)


def readable(entry: dict[str, Any]) -> str:
    """`<local time>  <event>  <tool>` and the fields present, then the content in full."""
    when = datetime.fromisoformat(entry["ts"]).astimezone()
    parts = [when.strftime("%Y-%m-%d %H:%M:%S"), entry["event"], entry["tool"]]
    labelled = (("chat", "chat_id"), ("approval", "approval_id"))
    parts += [f"{label} {entry[key]}" for label, key in labelled if entry.get(key) is not None]
    if entry.get("decision") is not None:
        parts.append(str(entry["decision"]))
    if entry.get("result") is not None and entry["event"] not in _QUIET_RESULTS:
        parts.append(f"→ {entry['result']}")
    if entry.get("client") is not None:
        parts.append(f"client {entry['client']}")
    head = "  ".join(escape_field(str(part)) for part in parts)
    content = entry.get("content")
    if content is None or content == "":
        return head
    text = str(content)
    if "\n" not in text:
        return f"{head} | {escape_text(text)}"
    return "\n".join([head] + [f"    {escape_text(part)}" for part in text.split("\n")])


# Reading -------------------------------------------------------------------------------------


def _open_read_only(path: Path) -> BinaryIO:
    return path.open("rb")


@dataclass
class _Open:
    path: Path
    handle: BinaryIO
    inode: tuple[int, int]


class LogReader:
    def __init__(
        self,
        paths: FixedPaths,
        say: Say,
        *,
        opener: Opener = _open_read_only,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._paths = paths
        self._say = say
        self._opener = opener
        self._sleep = sleep
        self._partial = b""

    def run(self, options: LogOptions) -> None:
        """Raises LogUnreadable when a file exists but cannot be read."""
        snapshot = self._snapshot()
        live = snapshot[0] if snapshot and snapshot[0].path == self._paths.log else None
        if not snapshot:
            self._say(f"Nothing has been logged yet ({self._paths.log}).")
        pending = self._print_snapshot(snapshot, live, options)
        if options.follow:
            self._follow(live, pending, options.as_json)

    def _snapshot(self) -> list[_Open]:
        opened: list[_Open] = []
        seen: set[tuple[int, int]] = set()
        for path in log_files(self._paths):
            handle = self._open(path)
            if handle is None:
                continue
            stat = os.fstat(handle.fileno())
            inode = (stat.st_dev, stat.st_ino)
            if inode in seen:  # rotated while the snapshot was taken: already open
                handle.close()
                continue
            seen.add(inode)
            opened.append(_Open(path, handle, inode))
        return opened

    def _open(self, path: Path) -> BinaryIO | None:
        try:
            return self._opener(path)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise LogUnreadable(path, error) from error

    def _print_snapshot(
        self, snapshot: list[_Open], live: _Open | None, options: LogOptions
    ) -> bytes:
        kept: deque[str] | None = deque(maxlen=options.lines) if options.lines else None
        pending = b""
        for item in reversed(snapshot):  # oldest first
            hold = options.follow and item is live
            for line in self._complete_lines(item, hold):
                if kept is not None:
                    kept.append(line)
                else:
                    self._say(render(line, options.as_json))
            if hold:
                pending = self._partial
            if item is not live:
                item.handle.close()
        for line in kept or ():
            self._say(render(line, options.as_json))
        return pending

    def _complete_lines(self, item: _Open, hold_partial: bool) -> Iterator[str]:
        """Each line of the file, read one at a time. With ``hold_partial`` a last line with
        no line feed is kept in ``self._partial`` instead of being returned."""
        self._partial = b""
        while True:
            try:
                raw = item.handle.readline()
            except OSError as error:
                raise LogUnreadable(item.path, error) from error
            if not raw:
                return
            if not raw.endswith(b"\n") and hold_partial:
                self._partial = raw
                return
            yield _decode(raw)

    def _follow(self, live: _Open | None, pending: bytes, as_json: bool) -> None:
        while True:
            self._sleep(POLL_SECONDS)
            if live is not None:
                pending = self._drain(live, pending, as_json)
            current = _inode_of(self._paths.log)
            if current is None or (live is not None and current == live.inode):
                continue
            if live is not None:  # rotated: finish the old file, then start the new one
                pending = self._drain(live, pending, as_json)
                if pending:
                    self._say(render(_decode(pending), as_json))
                live.handle.close()
            live, pending = self._reopen(), b""
            if live is not None:
                pending = self._drain(live, pending, as_json)

    def _reopen(self) -> _Open | None:
        handle = self._open(self._paths.log)
        if handle is None:
            return None
        stat = os.fstat(handle.fileno())
        return _Open(self._paths.log, handle, (stat.st_dev, stat.st_ino))

    def _drain(self, live: _Open, pending: bytes, as_json: bool) -> bytes:
        while True:
            try:
                raw = live.handle.readline()
            except OSError as error:
                raise LogUnreadable(live.path, error) from error
            if not raw:
                return pending
            pending += raw
            if pending.endswith(b"\n"):
                self._say(render(_decode(pending), as_json))
                pending = b""


def _decode(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    return text[:-1] if text.endswith("\n") else text


def _inode_of(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino


def run_log(
    paths: FixedPaths,
    options: LogOptions,
    *,
    say: Say | None = None,
    complain: Say | None = None,
    opener: Opener = _open_read_only,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """0 when printed (or stopped with Ctrl-C while following); 1 when a file is unreadable."""
    out = say or (lambda text: print(text, flush=True))
    err = complain or (lambda text: print(text, file=sys.stderr, flush=True))
    try:
        LogReader(paths, out, opener=opener, sleep=sleep).run(options)
    except LogUnreadable as error:
        err(str(error))
        return 1
    except KeyboardInterrupt:
        return 0
    return 0
