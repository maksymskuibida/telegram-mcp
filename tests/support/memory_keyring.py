"""An in-memory keyring backend for tests, able to fail on demand."""

from __future__ import annotations

from keyring.backend import KeyringBackend
from keyring.errors import KeyringLocked, PasswordDeleteError, PasswordSetError


class MemoryKeyring(KeyringBackend):
    """Holds items in a dict. Set ``fail_on_write`` to make the n-th write (1-based) fail,
    ``locked`` to make every call raise ``KeyringLocked``, ``fail_deletes`` to refuse every
    delete and ``fail_delete_items`` to refuse deleting the named items only."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        self.items: dict[tuple[str, str], str] = {}
        self.locked = False
        self.fail_on_write: set[int] = set()
        self.fail_deletes = False
        self.fail_delete_items: set[str] = set()
        self.writes = 0

    def _check_locked(self) -> None:
        if self.locked:
            raise KeyringLocked("the keychain is locked")

    def get_password(self, service: str, username: str) -> str | None:
        self._check_locked()
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._check_locked()
        self.writes += 1
        if self.writes in self.fail_on_write:
            raise PasswordSetError("write refused")
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._check_locked()
        if self.fail_deletes or username in self.fail_delete_items:
            raise PasswordDeleteError("delete refused")
        if (service, username) not in self.items:
            raise PasswordDeleteError("no such item")
        del self.items[(service, username)]
