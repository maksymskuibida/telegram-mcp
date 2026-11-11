"""The SessionVault: the session and app credentials, in the macOS Keychain only (C8 vault).

There is no fallback store: a Keychain that is locked or refuses access is an error."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import keyring
from keyring.errors import KeyringError, KeyringLocked

from telegram_mcp.paths import KEYCHAIN_SERVICE

ITEM_SESSION = "session"
ITEM_API_ID = "api_id"
ITEM_API_HASH = "api_hash"
ITEMS = (ITEM_SESSION, ITEM_API_ID, ITEM_API_HASH)


@dataclass(frozen=True)
class StoredSession:
    """The person's login. The secret fields never appear in a repr."""

    session: str = field(repr=False)
    api_id: int
    api_hash: str = field(repr=False)


class VaultOutcome(StrEnum):
    LOADED = "loaded"
    NO_SESSION = "no_session"
    KEYCHAIN_LOCKED = "keychain_locked"


@dataclass(frozen=True)
class VaultReadResult:
    outcome: VaultOutcome
    session: StoredSession | None = None
    cause: str | None = None


class VaultError(Exception):
    """A Keychain write failed. ``cause`` is "locked" or "access denied"; ``logged_out`` says
    that restoring the previous session failed too, so nothing is stored any more."""

    def __init__(self, cause: str, *, logged_out: bool = False) -> None:
        message = f"The macOS Keychain could not be used ({cause})"
        if logged_out:
            message += "; logged out, run `telegram-mcp login` again"
        super().__init__(message)
        self.cause = cause
        self.logged_out = logged_out


def _cause(error: KeyringError) -> str:
    return "locked" if isinstance(error, KeyringLocked) else "access denied"


class SessionVault:
    def __init__(self, service: str = KEYCHAIN_SERVICE) -> None:
        self._service = service

    def load(self) -> VaultReadResult:
        """Exactly one of loaded, no_session or keychain_locked (BR1.5)."""
        try:
            values = {item: keyring.get_password(self._service, item) for item in ITEMS}
        except KeyringError as error:
            return VaultReadResult(VaultOutcome.KEYCHAIN_LOCKED, cause=_cause(error))
        session, api_id, api_hash = (values[item] for item in ITEMS)
        if not session or not api_id or not api_hash or not api_id.isdigit():
            return VaultReadResult(VaultOutcome.NO_SESSION)
        stored = StoredSession(session=session, api_id=int(api_id), api_hash=api_hash)
        return VaultReadResult(VaultOutcome.LOADED, session=stored)

    def save(self, stored: StoredSession) -> None:
        """Write all three items; on any failure delete what this call wrote (BR1.3)."""
        written: list[str] = []
        try:
            for item, value in _values(stored):
                keyring.set_password(self._service, item, value)
                written.append(item)
        except KeyringError as error:
            self._delete(written)
            raise VaultError(_cause(error)) from error

    def replace(self, new: StoredSession, old: StoredSession) -> None:
        """Write the new session over the old one. If a write fails, the old three values are
        written back; if that fails too, all three are deleted (BR1.8). Never a mix."""
        try:
            for item, value in _values(new):
                keyring.set_password(self._service, item, value)
        except KeyringError as error:
            try:
                for item, value in _values(old):
                    keyring.set_password(self._service, item, value)
            except KeyringError:
                self._delete(list(ITEMS))
                raise VaultError(_cause(error), logged_out=True) from error
            raise VaultError(_cause(error)) from error

    def _delete(self, items: list[str]) -> None:
        for item in items:
            try:
                keyring.delete_password(self._service, item)
            except KeyringError:
                continue


def _values(stored: StoredSession) -> list[tuple[str, str]]:
    return [
        (ITEM_SESSION, stored.session),
        (ITEM_API_ID, str(stored.api_id)),
        (ITEM_API_HASH, stored.api_hash),
    ]
