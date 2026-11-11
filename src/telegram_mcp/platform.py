"""Platform check and process-wide file permissions, applied at the start of every command."""

from __future__ import annotations

import os
import sys

MACOS_ONLY_MESSAGE = "telegram-mcp runs on macOS only"
EXIT_REFUSED = 2
PRIVATE_UMASK = 0o077


class UnsupportedPlatform(SystemExit):
    """Raised (as a SystemExit with code 2) when the tool runs anywhere but macOS."""

    def __init__(self) -> None:
        super().__init__(EXIT_REFUSED)


def is_macos(platform_name: str | None = None) -> bool:
    return (platform_name if platform_name is not None else sys.platform) == "darwin"


def require_macos(platform_name: str | None = None) -> None:
    """Exit 2 with the fixed message on any platform other than macOS (NFR10.1)."""
    if not is_macos(platform_name):
        print(MACOS_ONLY_MESSAGE, file=sys.stderr)
        raise UnsupportedPlatform()


def secure_umask() -> int:
    """Make every file and socket this process creates private to the user (NFR7.1).

    Returns the previous umask."""
    return os.umask(PRIVATE_UMASK)
