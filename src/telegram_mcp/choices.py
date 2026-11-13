"""The last scope choices: whether the person last deleted messages for everyone or only for
themselves, and whether they last created a basic group or a supergroup.

The choice question before a delete or a group creation offers the last choice first. The file
``choices.json`` in the data folder (mode 0600) holds only these two values. It is written by
the broker after a run the person Allowed, never from a tool argument, and a missing,
unreadable or malformed file simply means there is no last choice."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Literal

from telegram_mcp.paths import FixedPaths

CHOICES_NAME = "choices.json"
PRIVATE_FILE = 0o600
DeleteScope = Literal["everyone", "me"]
GroupKind = Literal["basic", "supergroup"]
_ALLOWED = {"delete_scope": ("everyone", "me"), "group_kind": ("basic", "supergroup")}


class ChoiceStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def for_paths(cls, paths: FixedPaths) -> ChoiceStore:
        return cls(paths.data_dir / CHOICES_NAME)

    def _read(self) -> dict[str, str]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            key: value for key, value in raw.items() if key in _ALLOWED and value in _ALLOWED[key]
        }

    def last_delete_scope(self) -> DeleteScope | None:
        value = self._read().get("delete_scope")
        return "everyone" if value == "everyone" else "me" if value == "me" else None

    def last_group_kind(self) -> GroupKind | None:
        value = self._read().get("group_kind")
        return "basic" if value == "basic" else "supergroup" if value == "supergroup" else None

    def remember(
        self, *, delete_scope: DeleteScope | None = None, group_kind: GroupKind | None = None
    ) -> bool:
        """Store an Allowed choice. False when it could not be written; the action itself has
        run, so the caller reports it instead of failing."""
        current = self._read()
        if delete_scope is not None:
            current["delete_scope"] = delete_scope
        if group_kind is not None:
            current["group_kind"] = group_kind
        partial = self.path.with_name(f".{CHOICES_NAME}.{os.getpid()}.part")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, PRIVATE_FILE
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                json.dump(current, target, sort_keys=True)
            os.chmod(partial, PRIVATE_FILE)
            os.replace(partial, self.path)
        except OSError:
            with contextlib.suppress(OSError):
                partial.unlink()
            return False
        return True


CHOICE_NOT_SAVED = "your choice could not be remembered for next time (choices.json)"
