"""The Settings reader (BR7.2 to BR7.4)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from telegram_mcp.paths import FixedPaths
from telegram_mcp.settings import Settings, SettingsError


@pytest.fixture
def paths(home: Path) -> FixedPaths:
    fixed = FixedPaths.from_home(home)
    fixed.data_dir.mkdir(parents=True, mode=0o700)
    return fixed


def write_config(paths: FixedPaths, text: str, mode: int = 0o600) -> None:
    paths.config.write_text(text)
    os.chmod(paths.config, mode)


def test_missing_file_gives_the_defaults_and_creates_nothing(paths: FixedPaths) -> None:
    settings = Settings.load(paths)
    assert settings.outbox_dir == paths.home / "Downloads/telegram-mcp/outbox"
    assert settings.downloads_dir == paths.home / "Downloads/telegram-mcp"
    assert not paths.config.exists()
    assert not settings.outbox_dir.exists()


def test_tilde_is_expanded_and_unknown_keys_are_ignored(paths: FixedPaths) -> None:
    write_config(paths, 'outbox_dir = "~/Out"\ncolour = "blue"\n')
    settings = Settings.load(paths)
    assert settings.outbox_dir == paths.home / "Out"
    assert settings.downloads_dir == paths.home / "Downloads/telegram-mcp"


def test_a_wider_mode_is_tightened_to_0600(paths: FixedPaths) -> None:
    write_config(paths, 'outbox_dir = "~/Out"\n', mode=0o644)
    Settings.load(paths)
    assert paths.config.stat().st_mode & 0o777 == 0o600


def test_invalid_toml_is_refused_naming_the_file(paths: FixedPaths) -> None:
    write_config(paths, "outbox_dir = [unclosed\n")
    with pytest.raises(SettingsError, match=r"config\.toml is not valid TOML"):
        Settings.load(paths)


def test_a_folder_value_that_is_not_text_is_refused_naming_the_key(paths: FixedPaths) -> None:
    write_config(paths, "downloads_dir = 42\n")
    with pytest.raises(SettingsError) as refused:
        Settings.load(paths)
    assert refused.value.key == "downloads_dir"


def test_a_link_anywhere_in_the_path_is_refused(paths: FixedPaths) -> None:
    real = paths.home / "real"
    real.mkdir()
    (paths.home / "linked").symlink_to(real)
    write_config(paths, 'outbox_dir = "~/linked/outbox"\n')
    with pytest.raises(SettingsError, match="is a link") as refused:
        Settings.load(paths)
    assert refused.value.key == "outbox_dir"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("/usr/local/somewhere", "outside your home folder"),
        ("~/Library/Application Support/telegram-mcp/out", "overlaps the tool's data folder"),
        ("~/Library", "overlaps the tool's data folder"),
        ("relative/path", "must be an absolute path"),
    ],
)
def test_a_folder_outside_home_or_overlapping_the_data_folder_is_refused(
    paths: FixedPaths, value: str, reason: str
) -> None:
    write_config(paths, f'outbox_dir = "{value}"\n')
    with pytest.raises(SettingsError, match=reason) as refused:
        Settings.load(paths)
    assert refused.value.key == "outbox_dir"


def test_ensure_folder_creates_missing_parents_with_0700(paths: FixedPaths) -> None:
    folder = paths.home / "a" / "b" / "outbox"
    Settings.ensure_folder(folder, "outbox_dir", paths)
    for part in (paths.home / "a", paths.home / "a" / "b", folder):
        assert part.is_dir()
        assert part.stat().st_mode & 0o777 == 0o700


def test_ensure_folder_refuses_a_folder_it_cannot_create(paths: FixedPaths) -> None:
    blocker = paths.home / "blocker"
    blocker.write_text("a file, not a folder")
    with pytest.raises(SettingsError, match="could not be created"):
        Settings.ensure_folder(blocker / "outbox", "outbox_dir", paths)
