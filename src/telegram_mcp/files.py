"""The FileSandbox: which local file may be sent, and where a download goes (BR25, BR26).

A file is sent only from the outbox folder, after every link in its path is followed, never
when it is one of the tool's own files (by path or by device and inode), and only when it is a
non-empty regular file within Telegram's own limit: 2000 MiB for a send, 10 MB for a photo. Its
identity is kept in memory, keyed by the call's digest, from the question until the upload is
accepted; then the same path is opened without following a link at the last step, the open
file is compared with that identity, and the upload reads only from that open file. After the
last part the file is compared once more before the final send request (BR28.5), so what
leaves the machine is what the person approved (NFR3.2, NFR3.3).

A download goes into the downloads folder only, within a budget: at most 4000 MiB per file, at
most 8000 MiB still to come over every unfinished download, and never more than the free space
less 100 MB (BR26.1). It is written to a new hidden partial file, then given its name only if
nothing exists under that name, and the partial file is removed on every failure (NFR2.4).
Partial files a killed broker left behind are removed when the next broker starts (BR28.9)."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
import unicodedata
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from telegram_mcp.paths import FixedPaths
from telegram_mcp.prompts import FileFields, PromptError
from telegram_mcp.settings import Settings, SettingsError

MiB = 1024 * 1024
MAX_SEND_BYTES = 2_097_152_000
"""2000 MiB: 4000 parts of 512 KB, the most Telegram takes for one file without Premium."""
MAX_SEND_LABEL = "2000 MiB"
MAX_PHOTO_BYTES = 10_485_760
"""10 MB, Telegram's limit for a photo (BR25.3)."""
MAX_PHOTO_LABEL = "10 MB"
MAX_DOWNLOAD_BYTES = 4_194_304_000
"""4000 MiB, the largest file Telegram stores (BR26.1)."""
MAX_DOWNLOAD_LABEL = "4000 MiB"
DOWNLOAD_BUDGET_BYTES = 8000 * MiB
"""What every unfinished download may still bring in, together (BR26.1)."""
FREE_SPACE_FLOOR_BYTES = 100 * MiB
"""The free space a download always leaves in the downloads folder."""
RECORD_LIFETIME = timedelta(seconds=180)
"""The 120 s question plus the 60 s to use an Allow."""
MAX_RECORDS = 100
"""As many as U2's open approvals."""
NAME_LIMIT = 200
CLASH_LIMIT = 999
PRIVATE_FILE = 0o600
PARTIAL_PREFIX = ".telegram-mcp-"
PARTIAL_NAME = re.compile(r"^\.telegram-mcp-[0-9a-f]{16}\.part$")
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


def admit(
    given: str,
    paths: FixedPaths,
    *,
    max_bytes: int = MAX_SEND_BYTES,
    max_label: str = MAX_SEND_LABEL,
) -> AdmittedFile:
    """BR25.1 to BR25.3: the file at ``given`` if it may be sent, else :class:`FileRefused`.
    ``max_bytes`` is Telegram's limit: a send's by default, a photo's for ``photo_path``.
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
    if info.st_size > max_bytes:
        raise FileRefused(
            "invalid_arguments",
            f"{resolved} is {info.st_size} bytes; Telegram takes at most {max_label} "
            f"({max_bytes} bytes) here.",
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


def admit_photo(given: str, paths: FixedPaths) -> AdmittedFile:
    """BR25.3: a ``photo_path`` is admitted as any file, with Telegram's 10 MB photo limit."""
    return admit(given, paths, max_bytes=MAX_PHOTO_BYTES, max_label=MAX_PHOTO_LABEL)


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
        current = admit(record.resolved_path, paths, max_bytes=max(record.size_bytes, 1))
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


def unchanged(record: AdmittedFile, opened: BinaryIO) -> bool:
    """BR28.5: after an upload's last part, the open file still has the approved size and
    last-modified time, and the stored path still names that same file (device and inode,
    without following a link at the last step)."""
    try:
        now_open = os.fstat(opened.fileno())
        at_path = os.stat(record.resolved_path, follow_symlinks=False)
    except (OSError, ValueError):
        return False
    return (
        stat.S_ISREG(at_path.st_mode)
        and (now_open.st_size, now_open.st_mtime_ns) == (record.size_bytes, record.modified_ns)
        and (at_path.st_dev, at_path.st_ino) == (record.device, record.inode)
    )


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


def create_partial(folder: Path) -> tuple[Path, int]:
    """BR26.3: a new hidden partial file in ``folder``, readable by the person only, and its
    open descriptor; a download transfer appends to it as its parts arrive."""
    partial = folder / f"{PARTIAL_PREFIX}{secrets.token_hex(8)}.part"
    descriptor = os.open(
        partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, PRIVATE_FILE
    )
    return partial, descriptor


def place_partial(folder: Path, partial: Path, wanted: str) -> Placed:
    """BR26.3: link the complete partial file to the first free name of ``wanted``,
    ``wanted (1)`` … ``(999)``, never replacing a file; the partial name is always removed."""
    try:
        size = os.stat(partial, follow_symlinks=False).st_size
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


def free_space(folder: Path) -> int:
    """The bytes free on the disk of ``folder``."""
    return shutil.disk_usage(folder).free


def check_download_budget(
    folder: Path, size_bytes: int | None, reserved_bytes: int, free_bytes: int
) -> None:
    """BR26.1: refuse a download over 4000 MiB, one that would take the bytes still to come of
    every unfinished download past 8000 MiB, and one the downloads folder's free space less
    100 MB cannot hold together with them. An unknown size counts as 4000 MiB."""
    if size_bytes is not None and size_bytes > MAX_DOWNLOAD_BYTES:
        raise FileRefused(
            "invalid_arguments",
            f"The file is {size_bytes} bytes; Telegram stores at most {MAX_DOWNLOAD_LABEL} "
            f"({MAX_DOWNLOAD_BYTES} bytes) per file.",
        )
    counted = size_bytes if size_bytes is not None else MAX_DOWNLOAD_BYTES
    if reserved_bytes + counted > DOWNLOAD_BUDGET_BYTES:
        raise FileRefused(
            "invalid_arguments",
            "Too much is already being downloaded; wait for a download to finish.",
        )
    needed = reserved_bytes + counted + FREE_SPACE_FLOOR_BYTES
    if free_bytes < needed:
        raise FileRefused(
            "invalid_arguments", f"Not enough free space in {folder}: {needed} bytes needed."
        )


def sweep_partials(folder: Path) -> list[str]:
    """BR28.9: remove the partial files a killed broker left directly in ``folder``: regular
    files named ``.telegram-mcp-<16 hex>.part`` owned by this user, and nothing else. Returns
    the names removed."""
    removed: list[str] = []
    try:
        names = os.listdir(folder)
    except OSError:
        return removed
    for name in names:
        if not PARTIAL_NAME.match(name):
            continue
        path = folder / name
        try:
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                continue
            path.unlink()
        except OSError:
            continue
        removed.append(name)
    return removed
