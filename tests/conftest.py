"""Test bootstrap: a short temporary HOME, an in-memory keyring and a guard against the network."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import keyring
import pytest
from telethon.client.telegrambaseclient import TelegramBaseClient

from support.memory_keyring import MemoryKeyring
from support.samples import sample_chats
from telegram_mcp.gateway.fake import FakeGateway


@pytest.fixture(autouse=True)
def home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Every test gets its own HOME under /tmp so the broker socket path stays under
    macOS's 104-byte limit. The path is resolved, because /tmp itself is a link."""
    folder = Path(os.path.realpath(tempfile.mkdtemp(prefix="tgm", dir="/tmp")))
    monkeypatch.setenv("HOME", str(folder))
    monkeypatch.delenv("TELEGRAM_MCP_TEST_FAKE_GATEWAY", raising=False)
    # Any subprocess a test starts uses a file keyring in this HOME, never the real Keychain.
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "support.file_keyring.FileKeyring")
    tests_dir = str(Path(__file__).parent)
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join(filter(None, [tests_dir, os.environ.get("PYTHONPATH")]))
    )
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture(autouse=True)
def memory_keyring() -> Iterator[MemoryKeyring]:
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)


@pytest.fixture
def fake_gateway() -> FakeGateway:
    """A fake gateway with one chat of each kind; tests add hostile ones as they need."""
    return FakeGateway(chats=list(sample_chats()))


@pytest.fixture(autouse=True)
def no_telegram_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails any test that makes Telethon open a connection (NFR9.1)."""

    async def refuse(self: Any, *args: Any, **kwargs: Any) -> None:
        pytest.fail("a test tried to open a Telegram network connection")

    monkeypatch.setattr(TelegramBaseClient, "connect", refuse)
