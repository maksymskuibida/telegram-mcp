"""The `telegram-mcp` command (C7, NFR7.1, NFR10.1)."""

from __future__ import annotations

import asyncio
import io
import json
import os
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

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
    commands = (
        ["serve"],
        ["broker"],
        ["login"],
        ["login", "--qr"],
        ["logout"],
        ["status"],
        ["log"],
        ["hook", "approval-pre"],
    )
    for command in commands:
        with pytest.raises(SystemExit) as exited:
            cli.main(command)
        assert exited.value.code == 2
    assert capsys.readouterr().err.count("telegram-mcp runs on macOS only") == len(commands)


def test_the_umask_is_private_after_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert cli.main(["hook", "approval-post"]) == 0
    current = os.umask(0o077)
    assert current == 0o077
    assert json.loads(capsys.readouterr().out) == {}


def test_unknown_commands_and_hook_names_are_refused(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (["logoff"], ["logout", "--force"], ["hook", "nope"], ["hook", "Guard"], []):
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


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["login"], {"qr": False}),
        (["login", "--qr"], {"qr": True}),
        (["logout"], {"yes": False}),
        (["logout", "--yes"], {"yes": True}),
        (["log"], {"lines": None, "follow": False, "json": False}),
        (["log", "--lines", "5", "--follow", "--json"], {"lines": 5, "follow": True, "json": True}),
    ],
)
def test_the_person_commands_parse_their_options(argv: list[str], expected: dict[str, Any]) -> None:
    args = vars(cli.build_parser().parse_args(argv))
    assert {key: args[key] for key in expected} == expected


@pytest.mark.parametrize("bad", ["0", "x", "-3", "1.5"])
def test_a_bad_lines_value_fails_with_the_usage_and_exit_1(
    bad: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exited:
        cli.main(["log", "--lines", bad])
    assert exited.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("usage: telegram-mcp log")
    assert "--lines" in err


def test_each_person_command_runs_its_own_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    from telegram_mcp import log_reader, login, session_commands

    calls: list[tuple[str, Any]] = []

    def recorder(name: str, code: int, pick: Callable[..., Any]) -> Callable[..., int]:
        def record(*args: Any, **kwargs: Any) -> int:
            calls.append((name, pick(*args, **kwargs)))
            return code

        return record

    monkeypatch.setattr(login, "run_login_command", recorder("login", 0, lambda p, qr: qr))
    monkeypatch.setattr(session_commands, "run_logout", recorder("logout", 0, lambda p, t, y: y))
    monkeypatch.setattr(session_commands, "run_status", recorder("status", 2, lambda p, t: None))
    monkeypatch.setattr(log_reader, "run_log", recorder("log", 0, lambda p, options: options))
    assert cli.main(["login", "--qr"]) == 0
    assert cli.main(["logout", "--yes"]) == 0
    assert cli.main(["status"]) == 2
    assert cli.main(["log", "--lines", "3"]) == 0
    assert calls == [
        ("login", True),
        ("logout", True),
        ("status", None),
        ("log", log_reader.LogOptions(lines=3)),
    ]


def test_log_and_help_open_no_socket(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], home: Path
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("no socket may be opened")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(asyncio, "open_unix_connection", refuse)
    assert cli.main(["log"]) == 0
    assert "Nothing has been logged yet" in capsys.readouterr().out
    for argv in (["--help"], ["log", "--help"], ["status", "--help"]):
        with pytest.raises(SystemExit) as shown:
            cli.main(argv)
        assert shown.value.code == 0
    assert not (home / "Library").exists()
