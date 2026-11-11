"""Platform check, umask and the fixed paths (NFR10.1, NFR7.1, NFR7.2, BR7.1)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from telegram_mcp.paths import FixedPaths
from telegram_mcp.platform import MACOS_ONLY_MESSAGE, require_macos, secure_umask


def test_require_macos_passes_on_darwin() -> None:
    require_macos("darwin")


def test_require_macos_exits_2_with_the_fixed_message_elsewhere(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        require_macos("linux")
    assert exited.value.code == 2
    assert capsys.readouterr().err.strip() == MACOS_ONLY_MESSAGE


def test_secure_umask_makes_new_files_private(home: Path) -> None:
    previous = os.umask(0o022)
    try:
        secure_umask()
        target = home / "created"
        target.write_text("x")
        assert target.stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(previous)


def test_fixed_paths_come_from_the_home_folder(home: Path) -> None:
    paths = FixedPaths.from_home()
    data_dir = home / "Library" / "Application Support" / "telegram-mcp"
    assert paths.data_dir == data_dir
    assert paths.config == data_dir / "config.toml"
    assert paths.log == data_dir / "activity.log"
    assert paths.socket == data_dir / "broker.sock"
    assert paths.secret == data_dir / "broker.secret"
    assert paths.lock == data_dir / "broker.lock"
    assert paths.diagnostics == data_dir / "broker.err"
    assert paths.keychain_service == "telegram-mcp"


def test_fixed_paths_ignore_the_configuration_file(home: Path) -> None:
    paths = FixedPaths.from_home()
    paths.data_dir.mkdir(parents=True)
    paths.config.write_text('data_dir = "/elsewhere"\nlog = "/elsewhere/log"\n')
    again = FixedPaths.from_home(home)
    assert again == paths
    assert str(again.data_dir).startswith(str(home))
