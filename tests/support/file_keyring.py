"""A keyring backend for test subprocesses: a JSON file in the test's temporary HOME, outside
the tool's data folder. Selected with PYTHON_KEYRING_BACKEND so that no subprocess started by a
test ever reads or writes the real macOS Keychain."""

from __future__ import annotations

import json
from pathlib import Path

from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

KEYRING_FILE = "test-keyring.json"


class FileKeyring(KeyringBackend):
    priority = 1

    @staticmethod
    def _path() -> Path:
        return Path.home() / KEYRING_FILE

    def _items(self) -> dict[str, str]:
        path = self._path()
        return json.loads(path.read_text()) if path.exists() else {}

    def _save(self, items: dict[str, str]) -> None:
        self._path().write_text(json.dumps(items))

    def get_password(self, service: str, username: str) -> str | None:
        return self._items().get(f"{service}/{username}")

    def set_password(self, service: str, username: str, password: str) -> None:
        items = self._items()
        items[f"{service}/{username}"] = password
        self._save(items)

    def delete_password(self, service: str, username: str) -> None:
        items = self._items()
        if items.pop(f"{service}/{username}", None) is None:
            raise PasswordDeleteError("no such item")
        self._save(items)
