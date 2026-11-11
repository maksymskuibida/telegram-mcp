"""The hook launcher `hooks/run` blocks the call (exit 2) whenever the hook cannot give a valid
answer (BR9.2, NFR2.4). Each test puts a stub `uvx` on PATH."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "hooks" / "run"
SYSTEM_PATH = "/usr/bin:/bin"
HOOK_INPUT = '{"tool_name": "mcp__plugin_telegram-mcp_telegram__tg_messages_send"}'


def stub_uvx(folder: Path, body: str) -> Path:
    folder.mkdir(exist_ok=True)
    stub = folder / "uvx"
    stub.write_text("#!/bin/sh\n" + body + "\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return folder


def run_launcher(
    path: str, home: Path, name: str = "approval-pre"
) -> subprocess.CompletedProcess[str]:
    env = {"PATH": path, "HOME": str(home), "CLAUDE_PLUGIN_ROOT": "/plugin root"}
    return subprocess.run(
        ["/bin/sh", str(LAUNCHER), name],
        input=HOOK_INPUT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=10,
    )


def test_a_valid_answer_is_passed_through_from_the_pinned_command(home: Path) -> None:
    record = home / "uvx-call.txt"
    answer = '{"hookSpecificOutput": {"permissionDecision": "deny"}}'
    body = f'printf "%s\\n" "$@" > "{record}"\ncat >> "{record}"\nprintf \'%s\\n\' \'{answer}\''
    folder = stub_uvx(home / "bin", body)
    result = run_launcher(f"{folder}:{SYSTEM_PATH}", home)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == answer
    arguments = record.read_text().splitlines()
    assert arguments[:8] == [
        "--quiet",
        "--constraints",
        "/plugin root/constraints.txt",
        "--from",
        "/plugin root",
        "telegram-mcp",
        "hook",
        "approval-pre",
    ]
    assert arguments[8] == HOOK_INPUT


def test_a_missing_uvx_blocks(home: Path) -> None:
    result = run_launcher(SYSTEM_PATH, home)
    assert result.returncode == 2
    assert "uvx was not found" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("exit 1", "the hook command failed"),
        ("kill -9 $$", "the hook command failed"),
        ("exit 0", "printed no JSON answer"),
        ("echo 'Traceback (most recent call last):'", "printed no JSON answer"),
        ("echo '{\"half\": '", "printed no JSON answer"),
    ],
)
def test_a_failing_crashing_or_silent_hook_blocks(home: Path, body: str, reason: str) -> None:
    folder = stub_uvx(home / "bin", body)
    result = run_launcher(f"{folder}:{SYSTEM_PATH}", home)
    assert result.returncode == 2
    assert reason in result.stderr
    assert result.stdout == ""


def test_no_hook_name_blocks(home: Path) -> None:
    folder = stub_uvx(home / "bin", "echo '{}'")
    result = run_launcher(f"{folder}:{SYSTEM_PATH}", home, name="")
    assert result.returncode == 2
    assert "no hook name" in result.stderr


def test_the_launcher_is_executable_posix_sh() -> None:
    assert LAUNCHER.read_text().startswith("#!/bin/sh\n")
    assert os.access(LAUNCHER, os.X_OK)
    subprocess.run(["/bin/sh", "-n", str(LAUNCHER)], check=True)
