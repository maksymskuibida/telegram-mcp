"""The Claude Code plugin files (BR9.1, C6, NFR1.6)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PINNED_RUN = [
    "--quiet",
    "--constraints",
    "${CLAUDE_PLUGIN_ROOT}/constraints.txt",
    "--from",
    "${CLAUDE_PLUGIN_ROOT}",
    "telegram-mcp",
]


def load(relative: str) -> Any:
    return json.loads((ROOT / relative).read_text())


@pytest.mark.parametrize(
    "relative",
    [
        ".claude-plugin/plugin.json",
        ".claude-plugin/marketplace.json",
        ".mcp.json",
        "hooks/hooks.json",
    ],
)
def test_every_plugin_file_is_valid_json(relative: str) -> None:
    assert isinstance(load(relative), dict)


def test_the_plugin_and_marketplace_name_one_plugin_from_this_folder() -> None:
    plugin = load(".claude-plugin/plugin.json")
    marketplace = load(".claude-plugin/marketplace.json")
    assert plugin["name"] == "telegram-mcp"
    assert [(p["name"], p["source"]) for p in marketplace["plugins"]] == [("telegram-mcp", "./")]
    version = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M)
    assert version is not None
    assert plugin["version"] == version[1]


def test_the_server_runs_the_pinned_versions() -> None:
    servers = load(".mcp.json")["mcpServers"]
    assert list(servers) == ["telegram"]
    assert servers["telegram"]["command"] == "uvx"
    assert servers["telegram"]["args"] == [*PINNED_RUN, "serve"]
    assert (ROOT / "constraints.txt").is_file()


def test_the_two_approval_hooks_run_through_the_launcher_with_their_timeouts() -> None:
    hooks = load("hooks/hooks.json")["hooks"]
    (pre,) = hooks["PreToolUse"]
    (post,) = hooks["PostToolUse"]
    assert pre["matcher"] == "mcp__plugin_telegram-mcp_telegram__tg_messages_send"
    assert post["matcher"] == "AskUserQuestion"
    assert pre["hooks"] == [
        {
            "type": "command",
            "command": "${CLAUDE_PLUGIN_ROOT}/hooks/run approval-pre",
            "timeout": 20,
        }
    ]
    assert post["hooks"] == [
        {
            "type": "command",
            "command": "${CLAUDE_PLUGIN_ROOT}/hooks/run approval-post",
            "timeout": 15,
        }
    ]
    assert re.fullmatch(pre["matcher"], "mcp__plugin_telegram-mcp_telegram__tg_messages_send")
    assert not re.fullmatch(pre["matcher"], "mcp__plugin_telegram-mcp_telegram__tg_chats_list")


def test_no_other_hook_is_registered_yet() -> None:
    hooks = load("hooks/hooks.json")["hooks"]
    assert set(hooks) == {"PreToolUse", "PostToolUse"}
    commands = [h["command"] for entries in hooks.values() for e in entries for h in e["hooks"]]
    assert all(command.startswith("${CLAUDE_PLUGIN_ROOT}/hooks/run ") for command in commands)
    assert not any(command.endswith(" guard") for command in commands)
