"""The broker's secret file: written whole, private, and swapped in one step.

A new secret is written to a new file with a random name in the data folder, created
exclusively, without following a link and with mode 0600, and then renamed over
``broker.secret``. A reader therefore always sees the whole old secret or the whole new one,
and ``broker.secret`` never exists with another mode or as a link."""

from __future__ import annotations

import contextlib
import os
import secrets
from pathlib import Path

SECRET_BYTES = 32
SECRET_FILE_MODE = 0o600
TEMPORARY_PATTERN = "broker.secret.*.tmp"


def new_secret() -> str:
    """32 random bytes from the operating system's secure source, as 64 hex characters."""
    return secrets.token_hex(SECRET_BYTES)


def temporary_name() -> str:
    return f"broker.secret.{secrets.token_hex(8)}.tmp"


def write_secret(path: Path, value: str) -> None:
    """Replace the file at ``path`` with ``value`` in one step. Raises ``OSError`` and leaves
    ``path`` as it was when the new file cannot be written or renamed."""
    temporary = path.with_name(temporary_name())
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        SECRET_FILE_MODE,
    )
    try:
        try:
            os.fchmod(descriptor, SECRET_FILE_MODE)
            data = value.encode("ascii")
            if os.write(descriptor, data) != len(data):
                raise OSError("the secret was not written whole")
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def remove_leftovers(folder: Path) -> None:
    """Delete temporary secret files a stopped broker left behind."""
    for leftover in folder.glob(TEMPORARY_PATTERN):
        with contextlib.suppress(OSError):
            leftover.unlink()
