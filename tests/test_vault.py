"""The SessionVault (BR1.3, BR1.5, BR1.8; FR1.4, FR1.7, FR1.8)."""

from __future__ import annotations

import pytest

from support.memory_keyring import MemoryKeyring
from support.samples import fake_secrets
from telegram_mcp.vault import (
    ITEMS,
    SessionVault,
    StoredSession,
    VaultError,
    VaultOutcome,
)

SERVICE = "telegram-mcp"


def stored() -> StoredSession:
    values = fake_secrets()
    return StoredSession(session=values.session, api_id=values.api_id, api_hash=values.api_hash)


def test_nothing_stored_is_no_session() -> None:
    assert SessionVault().load().outcome is VaultOutcome.NO_SESSION


def test_only_some_items_is_no_session(memory_keyring: MemoryKeyring) -> None:
    memory_keyring.items[(SERVICE, "session")] = "partial"
    memory_keyring.items[(SERVICE, "api_id")] = "12"
    result = SessionVault().load()
    assert result.outcome is VaultOutcome.NO_SESSION
    assert result.session is None


def test_a_locked_keychain_is_reported_with_its_cause(memory_keyring: MemoryKeyring) -> None:
    memory_keyring.locked = True
    result = SessionVault().load()
    assert result.outcome is VaultOutcome.KEYCHAIN_LOCKED
    assert result.cause == "locked"


def test_save_writes_all_three_and_load_returns_them(memory_keyring: MemoryKeyring) -> None:
    session = stored()
    SessionVault().save(session)
    assert {name for _, name in memory_keyring.items} == set(ITEMS)
    result = SessionVault().load()
    assert result.outcome is VaultOutcome.LOADED
    assert result.session == session


def test_a_failed_save_leaves_nothing_behind(memory_keyring: MemoryKeyring) -> None:
    memory_keyring.fail_on_write = {2}
    with pytest.raises(VaultError) as failed:
        SessionVault().save(stored())
    assert failed.value.cause == "access denied"
    assert memory_keyring.items == {}
    assert SessionVault().load().outcome is VaultOutcome.NO_SESSION


def test_replace_writes_the_new_session(memory_keyring: MemoryKeyring) -> None:
    old, new = stored(), stored()
    vault = SessionVault()
    vault.save(old)
    vault.replace(new, old)
    assert vault.load().session == new


def test_a_failed_replace_restores_the_old_session(memory_keyring: MemoryKeyring) -> None:
    old, new = stored(), stored()
    vault = SessionVault()
    vault.save(old)
    memory_keyring.fail_on_write = {memory_keyring.writes + 2}
    with pytest.raises(VaultError) as failed:
        vault.replace(new, old)
    assert not failed.value.logged_out
    assert vault.load().session == old


def test_a_failed_restore_deletes_everything_and_says_so(memory_keyring: MemoryKeyring) -> None:
    old, new = stored(), stored()
    vault = SessionVault()
    vault.save(old)
    first = memory_keyring.writes
    memory_keyring.fail_on_write = {first + 2, first + 3}
    with pytest.raises(VaultError, match="logged out") as failed:
        vault.replace(new, old)
    assert failed.value.logged_out
    assert memory_keyring.items == {}


def test_secrets_never_appear_in_a_repr() -> None:
    session = stored()
    shown = repr(session)
    assert session.session not in shown
    assert session.api_hash not in shown
