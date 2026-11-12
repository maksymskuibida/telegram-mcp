"""The FileSandbox: which local file may be sent, and where a download goes (BR25, BR26).

A file is sent only from the outbox folder, after every link in its path is followed, never
when it is one of the tool's own files (by path or by device and inode), and only when it is a
non-empty regular file of at most 20 MB. Its identity is kept in memory, keyed by the call's
digest, from the question until the send; the send opens the same path without following a
link at the last step, compares the open file with that identity, and uploads from that open
file, so what leaves the machine is what the person approved (NFR3.2).

A download goes into the downloads folder only: it is written to a new hidden partial file,
then given its name only if nothing exists under that name, and the partial file is removed
on every failure (NFR2.4)."""

from __future__ import annotations

import os
import secrets
import stat
import unicodedata
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from telegram_mcp.paths import FixedPaths
from telegram_mcp.prompts import FileFields, PromptError
from telegram_mcp.settings import Settings, SettingsError

MAX_FILE_BYTES = 20 * 1024 * 1024
"""Sends and downloads are capped so one transfer fits in the 30 s call limit at 10 Mbit/s."""
MAX_FILE_LABEL = "20 MB"
RECORD_LIFETIME = timedelta(seconds=180)
"""The 120 s question plus the 60 s to use an Allow."""
MAX_RECORDS = 100
"""As many as U2's open approvals."""
NAME_LIMIT = 200
CLASH_LIMIT = 999
PRIVATE_FILE = 0o600
MEDIA_EXTENSIONS = {"photo": ".jpg", "video": ".mp4", "voice": ".ogg"}
_HIDDEN = {"Cc", "Cf", "Zl", "Zp", "Zs", "Cs", "Co", "Cn"}
CHANGED_MESSAGE = (
    "The file changed after the question; call the tool again to be asked about the new file."
)
NO_RECORD_MESSAGE = (
    "The approved file is no longer known to the tool; nothing was sent. Call the tool again "
    "to ask again."
)
OWN_FILE_MESSAGE = "This file belongs to the tool and cannot be sent."


class FileRefused(PromptError):
    """A local file the sandbox will not use; ``code`` is not_allowed, not_found or
    invalid_arguments."""


@dataclass(frozen=True)
class AdmittedFile:
    """A local file let through for a send, with the identity the send checks again."""

    given_path: str
    resolved_path: str
    name: str
    size_bytes: int
    device: int
    inode: int
    modified_ns: int

    def fields(self) -> FileFields:
        """What the question shows (BR25.4)."""
        return FileFields(path=self.resolved_path, name=self.name, size_bytes=self.size_bytes)

    def identity(self) -> tuple[str, int, int, int, int]:
        return (self.resolved_path, self.device, self.inode, self.size_bytes, self.modified_ns)


def folder_for(paths: FixedPaths, key: str) -> Path:
    """The outbox or downloads folder from the settings, created when missing; a refused
    setting is not_allowed naming it (BR25.1, BR26.1)."""
    try:
        return Settings.ensure_folder(Settings.folder(paths, key), key, paths)
    except SettingsError as error:
        raise FileRefused(
            "not_allowed", f"The {key} setting is refused, so this cannot run: {error}"
        ) from error


def _expand(given: str, outbox: Path, home: Path) -> str:
    if given == "~":
        return str(home)
    if given.startswith("~/"):
        return str(home / given[2:])
    if os.path.isabs(given):
        return given
    return str(outbox / given)


def admit(given: str, paths: FixedPaths) -> AdmittedFile:
    """BR25.1 to BR25.3: the file at ``given`` if it may be sent, else :class:`FileRefused`.
    The file is never opened here."""
    outbox = Path(os.path.realpath(folder_for(paths, "outbox_dir")))
    resolved = Path(os.path.realpath(_expand(given, outbox, paths.home)))
    data_dir = Path(os.path.realpath(paths.data_dir))
    if resolved.is_relative_to(data_dir):
        raise FileRefused("not_allowed", OWN_FILE_MESSAGE)
    if not resolved.is_relative_to(outbox) or resolved == outbox:
        raise FileRefused(
            "not_allowed", f"Files can be sent only from {outbox}; put the file there first."
        )
    try:
        info = os.stat(resolved)
    except FileNotFoundError as error:
        raise FileRefused("not_found", f"There is no file at {resolved}.") from error
    except OSError as error:
        raise FileRefused("not_found", f"{resolved} cannot be read: {error.strerror}.") from error
    if (info.st_dev, info.st_ino) in _own_files(data_dir):
        raise FileRefused("not_allowed", OWN_FILE_MESSAGE)
    if not stat.S_ISREG(info.st_mode):
        raise FileRefused("invalid_arguments", f"{resolved} is not a regular file.")
    if info.st_size == 0:
        raise FileRefused("invalid_arguments", f"{resolved}: the file is empty.")
    if info.st_size > MAX_FILE_BYTES:
        raise FileRefused(
            "invalid_arguments",
            f"{resolved} is {info.st_size} bytes; files over {MAX_FILE_LABEL} are not sent by "
            "this tool. Send it in Telegram yourself.",
        )
    return AdmittedFile(
        given_path=given,
        resolved_path=str(resolved),
        name=resolved.name,
        size_bytes=info.st_size,
        device=info.st_dev,
        inode=info.st_ino,
        modified_ns=info.st_mtime_ns,
    )


def _own_files(data_dir: Path) -> set[tuple[int, int]]:
    """The device and inode of every file in the data folder (BR25.2)."""
    found: set[tuple[int, int]] = set()
    for folder, _, names in os.walk(data_dir):
        for name in names:
            try:
                info = os.lstat(os.path.join(folder, name))
            except OSError:
                continue
            found.add((info.st_dev, info.st_ino))
    return found


def open_admitted(record: AdmittedFile, paths: FixedPaths) -> BinaryIO:
    """BR25.5: check the stored path again, open it without following a link at the last step,
    compare the open file with the record, and return that open file for the upload."""
    try:
        current = admit(record.resolved_path, paths)
    except FileRefused as refused:
        raise FileRefused("not_allowed", CHANGED_MESSAGE) from refused
    if current.identity() != record.identity():
        raise FileRefused("not_allowed", CHANGED_MESSAGE)
    try:
        descriptor = os.open(record.resolved_path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise FileRefused("not_allowed", CHANGED_MESSAGE) from error
    info = os.fstat(descriptor)
    opened = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    expected = (record.device, record.inode, record.size_bytes, record.modified_ns)
    if not stat.S_ISREG(info.st_mode) or opened != expected:
        os.close(descriptor)
        raise FileRefused("not_allowed", CHANGED_MESSAGE)
    return os.fdopen(descriptor, "rb")


class FileStore:
    """The admitted files of open questions, keyed by the call's canonical digest, in memory
    only. A record is dropped when it is used, after 180 s, or as the oldest past 100."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        lifetime: timedelta = RECORD_LIFETIME,
        limit: int = MAX_RECORDS,
    ) -> None:
        self._clock = clock
        self.lifetime = lifetime
        self._limit = limit
        self._records: OrderedDict[str, tuple[AdmittedFile, datetime]] = OrderedDict()

    def _drop_expired(self) -> None:
        now = self._clock()
        for digest in [d for d, (_, until) in self._records.items() if now > until]:
            del self._records[digest]

    def put(self, digest: str, record: AdmittedFile) -> None:
        self._drop_expired()
        self._records.pop(digest, None)
        self._records[digest] = (record, self._clock() + self.lifetime)
        while len(self._records) > self._limit:
            self._records.popitem(last=False)

    def take(self, digest: str) -> AdmittedFile | None:
        """The record for this digest, used once; None when there is none or it expired."""
        self._drop_expired()
        found = self._records.pop(digest, None)
        return found[0] if found is not None else None

    def clear(self) -> None:
        self._records.clear()

    def __len__(self) -> int:
        self._drop_expired()
        return len(self._records)


# Downloads (BR26) ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Placed:
    """Where a download was saved: the result names these only, never the content (BR26.4)."""

    path: str
    name: str
    size_bytes: int


def safe_name(name: str | None, chat_id: int, message_id: int, kind: str) -> str:
    """BR26.2: Telegram's file name with path separators, control, format and separator
    characters replaced by ``_``, no leading dots, at most 200 characters keeping the
    extension; or ``<chat_id>_<message_id>`` plus the media type's extension."""
    cleaned = ""
    if name:
        cleaned = "".join(
            "_" if char in "/\\:" or unicodedata.category(char) in _HIDDEN else char
            for char in name
        ).lstrip(".")
    if not cleaned.strip("_"):
        return f"{chat_id}_{message_id}{MEDIA_EXTENSIONS.get(kind, '.bin')}"
    stem, extension = os.path.splitext(cleaned)
    if len(extension) > 16:
        stem, extension = cleaned, ""
    return stem[: NAME_LIMIT - len(extension)] + extension


def _candidates(name: str) -> list[str]:
    stem, extension = os.path.splitext(name)
    return [name] + [f"{stem} ({number}){extension}" for number in range(1, CLASH_LIMIT + 1)]


async def place_download(
    folder: Path, wanted: str, write: Callable[[BinaryIO], Awaitable[int]]
) -> Placed:
    """BR26.3: write into a new hidden partial file in ``folder``, then link it to the first
    free name of ``wanted``, ``wanted (1)`` … ``(999)``; the partial name is always removed,
    and on a failure nothing is left behind."""
    partial = folder / f".telegram-mcp-{secrets.token_hex(8)}.part"
    descriptor = os.open(
        partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, PRIVATE_FILE
    )
    try:
        with os.fdopen(descriptor, "wb") as target:
            await write(target)
            target.flush()
        size = os.stat(partial).st_size
        for name in _candidates(wanted):
            final = folder / name
            try:
                os.link(partial, final, follow_symlinks=False)
            except FileExistsError:
                continue
            return Placed(path=str(final), name=name, size_bytes=size)
        raise FileRefused(
            "invalid_arguments", "There are too many files with this name in the downloads folder."
        )
    finally:
        partial.unlink(missing_ok=True)
