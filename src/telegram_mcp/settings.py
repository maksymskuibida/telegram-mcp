"""The Settings reader: the two file folders from config.toml, checked before use (C4)."""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

from telegram_mcp.paths import FixedPaths

FOLDER_KEYS = ("outbox_dir", "downloads_dir")
DEFAULT_FOLDERS = {
    "outbox_dir": "~/Downloads/telegram-mcp/outbox",
    "downloads_dir": "~/Downloads/telegram-mcp",
}
CONFIG_MODE = 0o600
FOLDER_MODE = 0o700


class SettingsError(Exception):
    """A refused setting. The message names the file or the key and the reason."""

    def __init__(self, message: str, key: str | None = None) -> None:
        super().__init__(message)
        self.key = key


@dataclass(frozen=True)
class ToolSettings:
    config_path: Path
    outbox_dir: Path
    downloads_dir: Path


class Settings:
    """Reads and checks ``config.toml``. It never writes the file, except to tighten its mode."""

    @staticmethod
    def load(paths: FixedPaths) -> ToolSettings:
        data = _read_config(paths.config)
        folders: dict[str, Path] = {}
        for key in FOLDER_KEYS:
            value = data.get(key, DEFAULT_FOLDERS[key])
            if not isinstance(value, str):
                raise SettingsError(f"{key} in {paths.config} must be a text path", key)
            folder = _expand(value, paths.home, key)
            check_folder(folder, key, paths)
            folders[key] = folder
        return ToolSettings(
            config_path=paths.config,
            outbox_dir=folders["outbox_dir"],
            downloads_dir=folders["downloads_dir"],
        )

    @staticmethod
    def folder(paths: FixedPaths, key: str) -> Path:
        """One folder setting, checked on its own: a refused ``downloads_dir`` never stops a
        file send, and a refused ``outbox_dir`` never stops a download (NFR2.7)."""
        data = _read_config(paths.config)
        value = data.get(key, DEFAULT_FOLDERS[key])
        if not isinstance(value, str):
            raise SettingsError(f"{key} in {paths.config} must be a text path", key)
        folder = _expand(value, paths.home, key)
        check_folder(folder, key, paths)
        return folder

    @staticmethod
    def ensure_folder(folder: Path, key: str, paths: FixedPaths) -> Path:
        """Create a missing folder (and missing parents) with mode 0700, then check it again."""
        check_folder(folder, key, paths)
        missing = [p for p in [folder, *folder.parents] if not os.path.lexists(p)]
        try:
            for part in reversed(missing):
                part.mkdir(mode=FOLDER_MODE)
                os.chmod(part, FOLDER_MODE)
        except OSError as error:
            raise SettingsError(
                f"{key} ({folder}) could not be created: {error.strerror}", key
            ) from error
        check_folder(folder, key, paths)
        return folder


def check_folder(folder: Path, key: str, paths: FixedPaths) -> None:
    """Refuse a folder that is or contains a link, lies outside the home folder, or lies inside
    the data folder (BR7.3). A folder holding the data folder is refused too."""
    if not folder.is_absolute():
        raise SettingsError(f"{key} ({folder}) must be an absolute path or start with ~", key)
    for part in [folder, *folder.parents]:
        if os.path.lexists(part) and stat.S_ISLNK(os.lstat(part).st_mode):
            raise SettingsError(f"{key} ({folder}) is refused: {part} is a link", key)
    if not folder.is_relative_to(paths.home):
        raise SettingsError(f"{key} ({folder}) is refused: it is outside your home folder", key)
    if folder.is_relative_to(paths.data_dir) or paths.data_dir.is_relative_to(folder):
        raise SettingsError(f"{key} ({folder}) is refused: it overlaps the tool's data folder", key)


def _read_config(config: Path) -> dict[str, object]:
    try:
        info = os.lstat(config)
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise SettingsError(f"{config} could not be read: {error.strerror}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SettingsError(f"{config} is refused: it is not a regular file")
    try:
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.chmod(config, CONFIG_MODE)
        text = config.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise SettingsError(f"{config} could not be read: {error}") from error
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise SettingsError(f"{config} is not valid TOML: {error}") from error


def _expand(value: str, home: Path, key: str) -> Path:
    if value == "~":
        expanded = str(home)
    elif value.startswith("~/"):
        expanded = str(home / value[2:])
    else:
        expanded = value
    if not os.path.isabs(expanded):
        raise SettingsError(f"{key} ({value}) must be an absolute path or start with ~", key)
    return Path(os.path.normpath(expanded))
