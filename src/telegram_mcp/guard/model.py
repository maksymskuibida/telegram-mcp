"""The values one guard decision works with: the call, what is protected, and the answer."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from telegram_mcp.paths import FixedPaths

COMMAND_NAME = "telegram-mcp"
PACKAGE_MODULE = "telegram_mcp"
DATA_FOLDER_TEXT = "library/application support/telegram-mcp"
DATA_FILE_NAMES = (
    "activity.log",
    "config.toml",
    "broker.lock",
    "broker.err",
    "broker.sock",
    "broker.secret",
)
UNIQUE_FILE_NAMES = ("broker.sock", "broker.secret")
ALLOWED_SUBCOMMANDS = ("status", "--help", "-h", "--version")
REFUSED_SUBCOMMAND_WORDS = ("login", "logout", "log", "serve", "broker", "hook")


class Route(StrEnum):
    """Why a call was refused."""

    DATA_PATH = "data_path"
    DATA_TEXT = "data_text"
    UNDECIDABLE = "undecidable"
    KEYCHAIN = "keychain"
    OWN_COMMAND = "own_command"
    UNPARSABLE = "unparsable"
    UNREADABLE_CALL = "unreadable_call"
    GUARD_ERROR = "guard_error"


class Refusal(Exception):
    """Raised by a rule to refuse the call; the first refusal ends the check."""

    def __init__(self, route: Route, matched: str) -> None:
        super().__init__(route.value)
        self.route = route
        self.matched = matched


@dataclass(frozen=True)
class GuardCall:
    """One PreToolUse call as Claude Code hands it to the hook. Nothing in it is trusted, and
    a subagent's id, when present, is never read."""

    tool_name: str
    tool_input: dict[str, Any]
    cwd: str | None


@dataclass(frozen=True)
class ProtectedSet:
    """The data folder, fixed from the home folder and never from configuration (C4)."""

    home: str
    data_dir: str
    keychain_service: str

    @classmethod
    def from_paths(cls, paths: FixedPaths) -> ProtectedSet:
        return cls(
            home=str(paths.home),
            data_dir=str(paths.data_dir),
            keychain_service=paths.keychain_service,
        )


@dataclass(frozen=True)
class GuardDecision:
    """Pass, or refuse with a route and the reason Claude reads."""

    verdict: Literal["pass", "refuse"]
    route: Route | None = None
    matched: str | None = None
    reason: str | None = None

    def answer(self) -> dict[str, Any]:
        """The hook's JSON answer. A pass is the empty object, so Claude Code's own permission
        flow goes on; the guard never answers anything that skips it (BR4.5)."""
        if self.verdict == "pass":
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": self.reason or "",
            }
        }


PASS = GuardDecision("pass")
