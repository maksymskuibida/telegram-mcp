"""The fixed paths every component derives from the home folder, never from configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

KEYCHAIN_SERVICE = "telegram-mcp"
DATA_DIR_RELATIVE = Path("Library") / "Application Support" / "telegram-mcp"


@dataclass(frozen=True)
class FixedPaths:
    home: Path
    data_dir: Path
    config: Path
    log: Path
    socket: Path
    secret: Path
    lock: Path
    diagnostics: Path
    keychain_service: str = KEYCHAIN_SERVICE

    @classmethod
    def from_home(cls, home: Path | None = None) -> FixedPaths:
        """Build the paths for ``home`` (the current user's home folder by default)."""
        base = (home if home is not None else Path.home()).absolute()
        data_dir = base / DATA_DIR_RELATIVE
        return cls(
            home=base,
            data_dir=data_dir,
            config=data_dir / "config.toml",
            log=data_dir / "activity.log",
            socket=data_dir / "broker.sock",
            secret=data_dir / "broker.secret",
            lock=data_dir / "broker.lock",
            diagnostics=data_dir / "broker.err",
        )
