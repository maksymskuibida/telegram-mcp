"""The Claude Code plugin files (BR9.1, C6, NFR1.6)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from telegram_mcp.actions import default_registry
from telegram_mcp.consent import ActionKind
from telegram_mcp.paths import FixedPaths

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
    (pre, _guard) = hooks["PreToolUse"]
    (post,) = hooks["PostToolUse"]
    assert post["matcher"] == "AskUserQuestion"
    assert pre["hooks"] == [
        {
            "type": "command",
            "command": '"${CLAUDE_PLUGIN_ROOT}/hooks/run" approval-pre',
            "timeout": 20,
        }
    ]
    assert post["hooks"] == [
        {
            "type": "command",
            "command": '"${CLAUDE_PLUGIN_ROOT}/hooks/run" approval-post',
            "timeout": 15,
        }
    ]
    assert re.fullmatch(pre["matcher"], "mcp__plugin_telegram-mcp_telegram__tg_messages_send")
    assert not re.fullmatch(pre["matcher"], "mcp__plugin_telegram-mcp_telegram__tg_chats_list")


PREFIX = "mcp__plugin_telegram-mcp_telegram__"


def asking_tools() -> set[str]:
    """Every registered tool whose kind is write or mark, or that may mark (BR27.1)."""
    registry = default_registry(FixedPaths.from_home(ROOT))
    return {
        tool
        for tool, decl in registry.decls().items()
        if decl.kind is not ActionKind.READ or decl.marks_read_option
    }


def test_the_approval_matcher_covers_exactly_the_tools_that_can_ask() -> None:
    (pre, _guard) = load("hooks/hooks.json")["hooks"]["PreToolUse"]
    registry = default_registry(FixedPaths.from_home(ROOT))
    asking = asking_tools()
    assert len(asking) == 18  # 16 writes, the mark tool and the read tool
    for tool in registry.decls():
        matched = re.fullmatch(pre["matcher"], PREFIX + tool) is not None
        assert matched is (tool in asking), tool
        assert re.fullmatch(pre["matcher"], tool) is None, tool  # never without the prefix


def test_the_approval_matcher_matches_nothing_else() -> None:
    (pre, _guard) = load("hooks/hooks.json")["hooks"]["PreToolUse"]
    for name in [
        PREFIX + "tg_messages_sendx",
        PREFIX + "tg_messages_reader",
        "mcp__plugin_other_telegram__tg_messages_send",
        PREFIX + "tg_media_download",
        PREFIX + "tg_chats_resolve",
        "Bash",
    ]:
        assert re.fullmatch(pre["matcher"], name) is None, name


GUARDED_TOOLS = [
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
]


def test_the_guard_runs_before_claudes_own_tools_with_a_10_second_timeout() -> None:
    (_pre, guard) = load("hooks/hooks.json")["hooks"]["PreToolUse"]
    assert guard["hooks"] == [
        {
            "type": "command",
            "command": '"${CLAUDE_PLUGIN_ROOT}/hooks/run" guard',
            "timeout": 10,
        }
    ]
    for tool in GUARDED_TOOLS:
        assert re.fullmatch(guard["matcher"], tool), tool
    for tool in [
        "mcp__plugin_telegram-mcp_telegram__tg_messages_send",
        "mcp__plugin_telegram-mcp_telegram__tg_chats_list",
        "AskUserQuestion",
        "TodoWrite",
        "WebSearch",
        "BashOutput",
    ]:
        assert not re.fullmatch(guard["matcher"], tool), tool


def test_exactly_the_three_hooks_are_registered() -> None:
    hooks = load("hooks/hooks.json")["hooks"]
    assert set(hooks) == {"PreToolUse", "PostToolUse"}
    commands = [h["command"] for entries in hooks.values() for e in entries for h in e["hooks"]]
    # The plugin path is quoted, so a folder name with a space cannot split the command.
    assert all(command.startswith('"${CLAUDE_PLUGIN_ROOT}/hooks/run" ') for command in commands)
    assert sorted(command.rsplit(" ", 1)[1] for command in commands) == [
        "approval-post",
        "approval-pre",
        "guard",
    ]
