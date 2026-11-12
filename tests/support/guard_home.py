"""A temporary home folder for the guard's tests: a real data folder holding fake secrets, links
into it, and the outbox and downloads folders, either the defaults or configured ones."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telegram_mcp.guard import GuardDecision, decide
from telegram_mcp.paths import FixedPaths

DATA_FILES = (
    "activity.log",
    "config.toml",
    "broker.sock",
    "broker.secret",
    "broker.lock",
    "broker.err",
)
WATCHED_TOOLS = (
    "Bash",
    "Monitor",
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Glob",
    "Grep",
    "Task",
    "Agent",
    "WebFetch",
)


@dataclass
class GuardHome:
    home: Path
    paths: FixedPaths
    outbox: Path
    downloads: Path
    project: Path
    secrets: dict[str, str] = field(default_factory=dict)

    @property
    def data(self) -> Path:
        return self.paths.data_dir

    @property
    def environ(self) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "PWD": str(self.project),
            "TMPDIR": str(self.home / "tmp") + "/",
            "USER": "tester",
            "LOGNAME": "tester",
            "PATH": "/usr/bin:/bin",
            "SHELL": "/bin/zsh",
        }

    def call(self, tool: str, cwd: str | None = None, **tool_input: Any) -> dict[str, Any]:
        call: dict[str, Any] = {
            "session_id": "test",
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": tool_input,
            "cwd": str(self.project) if cwd is None else cwd,
        }
        return call

    def decide(self, call: object) -> GuardDecision:
        return decide(call, self.paths, self.environ)

    def bash(self, command: str, cwd: str | Path | None = None) -> GuardDecision:
        return self.decide(self.call("Bash", None if cwd is None else str(cwd), command=command))

    def fill(self, text: str) -> str:
        """Replaces {home}, {data} and {project} in a test table entry."""
        for key, value in (("home", self.home), ("data", self.data), ("project", self.project)):
            text = text.replace("{" + key + "}", str(value))
        return text


def make_guard_home(home: Path, configured: bool) -> GuardHome:
    paths = FixedPaths.from_home(home)
    paths.data_dir.mkdir(parents=True, mode=0o700)
    values: dict[str, str] = {}
    for name in DATA_FILES:
        value = f"fake-secret-{name}-{secrets.token_hex(8)}"
        values[name] = value
        (paths.data_dir / name).write_text(value + "\n")
    os.chmod(paths.config, 0o644)
    if configured:
        outbox = home / "Documents" / "tg" / "outbox"
        downloads = home / "Documents" / "tg" / "in"
        paths.config.write_text(
            f'outbox_dir = "~/Documents/tg/outbox"\ndownloads_dir = "~/Documents/tg/in"\n'
            f"# {values['config.toml']}\n"
        )
        os.chmod(paths.config, 0o644)
    else:
        outbox = home / "Downloads" / "telegram-mcp" / "outbox"
        downloads = home / "Downloads" / "telegram-mcp"
    outbox.mkdir(parents=True)
    downloads.mkdir(parents=True, exist_ok=True)
    (outbox / "photo.jpg").write_text("jpeg")
    project = home / "project"
    (project / "src").mkdir(parents=True)
    (project / "README.md").write_text("readme")
    (home / "tmp").mkdir()
    (home / "link").symlink_to(paths.data_dir)
    (home / "loglink").symlink_to(paths.log)
    (home / "loop").symlink_to(home / "loop")
    return GuardHome(
        home=home,
        paths=paths,
        outbox=outbox,
        downloads=downloads,
        project=project,
        secrets=values,
    )
