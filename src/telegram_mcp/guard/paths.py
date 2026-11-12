"""Path resolution and the protected-path test (BR1.1, BR1.4).

A path is protected when it is the data folder or lies inside it, compared without regard to
letter case, as macOS file names are. Both the path as written (with ``.`` and ``..`` collapsed)
and the path after following every link in its existing part are checked. Resolution reads only
link metadata (``lstat`` and ``readlink``); it never opens a file."""

from __future__ import annotations

import contextlib
import errno
import os
import posixpath
import stat
from collections import deque

from telegram_mcp.guard.model import ProtectedSet

MAX_LINKS = 40
# A part of the path does not exist (or cannot exist): the rest is taken as written.
MISSING = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ENAMETOOLONG})


class Unresolvable(Exception):
    """A path that cannot be resolved for a reason other than not existing (BR1.4)."""


def expand_home(path: str, home: str) -> str:
    """``~`` and ``~/…`` name the home folder; nothing else is expanded."""
    if path == "~":
        return home
    if path.startswith("~/"):
        return home + path[1:]
    return path


def absolute(path: str, base: str) -> str:
    return path if path.startswith("/") else posixpath.join(base, path)


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder + "/")


class Resolver:
    """Decides whether paths are protected, following links the way the system does."""

    def __init__(self, protected: ProtectedSet) -> None:
        self._known_plain: set[str] = set()
        self._decided: dict[str, bool] = {}
        self.data_dir = posixpath.normpath(protected.data_dir)
        self._folded = {self.data_dir.casefold()}
        # When the data folder's own links cannot be followed, the path as written still counts.
        with contextlib.suppress(Unresolvable):
            self._folded.add(self.resolve(self.data_dir).casefold())

    def resolve(self, path: str) -> str:
        """Follow every link in the part of the absolute ``path`` that exists."""
        resolved = "/"
        remaining = deque(path.split("/"))
        links = 0
        while remaining:
            part = remaining.popleft()
            if part in ("", "."):
                continue
            if part == "..":
                resolved = posixpath.dirname(resolved)
                continue
            candidate = posixpath.join(resolved, part)
            if candidate in self._known_plain:
                resolved = candidate
                continue
            target = self._read_link(candidate)
            if target is None:
                self._known_plain.add(candidate)
                resolved = candidate
            elif target is _MISSING:
                return posixpath.normpath(posixpath.join(candidate, *remaining))
            else:
                links += 1
                if links > MAX_LINKS:
                    raise Unresolvable(path)
                if target.startswith("/"):
                    resolved = "/"
                remaining.extendleft(reversed(target.split("/")))
        return resolved

    @staticmethod
    def _read_link(candidate: str) -> str | None:
        """The link's target, None for anything that is not a link, or _MISSING."""
        try:
            info = os.lstat(candidate)
        except ValueError:  # an embedded NUL: no such file can exist
            return _MISSING
        except OSError as error:
            if error.errno in MISSING:
                return _MISSING
            raise Unresolvable(candidate) from error
        if not stat.S_ISLNK(info.st_mode):
            return None
        try:
            return os.readlink(candidate)
        except OSError as error:
            raise Unresolvable(candidate) from error

    def is_protected(self, path: str) -> bool:
        """True when the absolute ``path``, as written or resolved, is in the data folder.

        Raises Unresolvable when its links cannot be followed. A path asked again within the
        same decision is answered from memory."""
        known = self._decided.get(path)
        if known is not None:
            return known
        written = posixpath.normpath(path).casefold()
        protected = any(_inside(written, folder) for folder in self._folded)
        if not protected:
            resolved = self.resolve(path).casefold()
            protected = any(_inside(resolved, folder) for folder in self._folded)
        self._decided[path] = protected
        return protected


_MISSING = "\0missing"
