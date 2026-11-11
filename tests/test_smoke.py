"""Proves the test runner works before the first test-first step."""

from pathlib import Path

import telegram_mcp


def test_package_imports_and_has_a_version() -> None:
    assert telegram_mcp.__version__ == "0.1.0"


def test_home_is_short_and_private_to_the_test(home: Path) -> None:
    assert Path.home() == home
    socket_path = home / "Library/Application Support/telegram-mcp/broker.sock"
    assert len(str(socket_path).encode()) < 104
