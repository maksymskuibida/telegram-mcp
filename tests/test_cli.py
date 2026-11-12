"""The `telegram-mcp` command (C7, NFR7.1, NFR10.1)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from telegram_mcp import cli


@pytest.fixture(autouse=True)
def restore_umask() -> object:
    previous = os.umask(0o022)
    os.umask(previous)
    yield
    os.umask(previous)


def test_every_command_checks_the_platform_first(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    for command in (["serve"], ["broker"], ["login"], ["hook", "approval-pre"]):
        with pytest.raises(SystemExit) as exited:
            cli.main(command)
        assert exited.value.code == 2
    assert capsys.readouterr().err.count("telegram-mcp runs on macOS only") == 4


def test_the_umask_is_private_after_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert cli.main(["hook", "approval-post"]) == 0
    current = os.umask(0o077)
    assert current == 0o077
    assert json.loads(capsys.readouterr().out) == {}


def test_unknown_commands_and_hook_names_are_refused(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (["logout"], ["hook", "nope"], ["hook", "Guard"], []):
        with pytest.raises(SystemExit) as exited:
            cli.main(argv)
        assert exited.value.code == 2
    with pytest.raises(SystemExit) as shown:
        cli.main(["--version"])
    assert shown.value.code == 0
    assert "telegram-mcp 0.1.0" in capsys.readouterr().out


def test_the_guard_hook_name_is_accepted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    call = {"tool_name": "Bash", "tool_input": {"command": "telegram-mcp log"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(call)))
    assert cli.main(["hook", "guard"]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["hookSpecificOutput"]["permissionDecision"] == "deny"
    monkeypatch.setattr(
        sys, "stdin", io.StringIO('{"tool_name": "Bash", "tool_input": {"command": "ls"}}')
    )
    assert cli.main(["hook", "guard"]) == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_the_module_runs_as_a_program(home: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "telegram_mcp.cli", "hook", "approval-pre"],
        input="not json",
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0
    answer = json.loads(result.stdout)
    assert answer["hookSpecificOutput"]["permissionDecision"] == "deny"
