"""The install script `install.sh` (FR2.2, AC2.5 to AC2.7, NFR10, the guard warm-up).

Each test runs the script as a real process with PATH set to a folder of stub programs plus
/usr/bin:/bin, so `osascript`, `sed` and `grep` are the system's own. The `claude` stub keeps
the marketplace and plugin state in a JSON file, answers the two `--json` lists from it, applies
install, uninstall and marketplace remove to it, and records every call. Nothing here runs the
real `claude`, `uv` or `uvx`.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "install.sh"
SYSTEM_PATH = "/usr/bin:/bin"
REPO = "maksymskuibida/telegram-mcp"
PLUGIN_ID = "telegram-mcp@telegram-mcp"

CLAUDE_STUB = """\
import json
import sys
from pathlib import Path

STATE = Path({state!r})
CALLS = Path({calls!r})
CACHE = Path({cache!r})

args = sys.argv[1:]
with CALLS.open("a") as calls:
    calls.write(json.dumps(args) + "\\n")
state = json.loads(STATE.read_text())


def failing(step):
    message = state.get("fail", {{}}).get(step)
    if message is not None:
        print(message, file=sys.stderr)
        raise SystemExit(1)


def save():
    STATE.write_text(json.dumps(state))


if args == ["plugin", "marketplace", "list", "--json"]:
    failing("marketplace list")
    print(state.get("raw_markets") or json.dumps(state["markets"]))
elif args == ["plugin", "list", "--json"]:
    failing("plugin list")
    print(json.dumps(state["plugins"]))
elif args[:3] == ["plugin", "install", "telegram-mcp"]:
    failing("install")
    if not state.get("install_does_nothing"):
        source = args[args.index("--marketplace") + 1]
        repo, ref = source.split("#")
        if not any(m["name"] == "telegram-mcp" for m in state["markets"]):
            state["markets"].append(
                {{"name": "telegram-mcp", "source": "github", "repo": repo, "ref": ref,
                  "installLocation": "/m"}}
            )
        version = ref.lstrip("v")
        state["plugins"].append(
            {{"id": "telegram-mcp@telegram-mcp", "version": version, "scope": "user",
              "enabled": True, "installPath": str(CACHE / version)}}
        )
        save()
    print("Installed telegram-mcp")
elif args[:2] == ["plugin", "uninstall"]:
    failing("uninstall")
    state["plugins"] = [p for p in state["plugins"] if p["id"] != args[2]]
    save()
elif args[:3] == ["plugin", "marketplace", "remove"]:
    failing("marketplace remove")
    state["markets"] = [m for m in state["markets"] if m["name"] != args[3]]
    save()
else:
    print("unexpected call: " + " ".join(args), file=sys.stderr)
    raise SystemExit(3)
"""

GIT_STUB = """\
#!/bin/sh
# `git ls-remote --exit-code --tags <url> refs/tags/<tag>`: 0 found, 2 not found, 128 no network.
printf '%s\\n' "$*" >> "{root}/git-calls"
grep -qx UNREACHABLE "{root}/remote-tags" && exit 128
for arg in "$@"; do last=$arg; done
grep -qx "${{last#refs/tags/}}" "{root}/remote-tags" || exit 2
exit 0
"""

HOOK_RUN_STUB = """\
#!/bin/sh
printf '%s\\n' "$*" > "$CLAUDE_PLUGIN_ROOT/warmup-args"
cat > "$CLAUDE_PLUGIN_ROOT/warmup-stdin"
exit {status}
"""


def write_program(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def github_market(ref: str) -> dict[str, Any]:
    return {
        "name": "telegram-mcp",
        "source": "github",
        "repo": REPO,
        "ref": ref,
        "installLocation": "/m",
    }


@dataclass
class Rig:
    """A temporary machine: stub programs, the claude state and the recorded calls."""

    root: Path

    @property
    def bin(self) -> Path:
        return self.root / "bin"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def state_file(self) -> Path:
        return self.root / "claude-state.json"

    @property
    def calls_file(self) -> Path:
        return self.root / "claude-calls.txt"

    def setup(self, system: str = "Darwin", without: tuple[str, ...] = ()) -> None:
        programs = {
            "uname": f"#!/bin/sh\necho {system}\n",
            "uv": "#!/bin/sh\nexit 0\n",
            "uvx": "#!/bin/sh\nexit 0\n",
            "git": GIT_STUB.format(root=self.root),
            "claude": f"#!{sys.executable}\n"
            + CLAUDE_STUB.format(
                state=str(self.state_file), calls=str(self.calls_file), cache=str(self.cache)
            ),
        }
        for name, text in programs.items():
            if name in without:
                (self.bin / name).unlink(missing_ok=True)
            else:
                write_program(self.bin / name, text)
        for version in ("0.1.0", "0.2.0"):
            self.set_warmup_status(version, 0)
        self.set_remote_tags("v0.1.0", "v0.2.0")
        self.set_state(markets=[], plugins=[])

    def set_remote_tags(self, *tags: str, unreachable: bool = False) -> None:
        """The tags the stub `git ls-remote` finds on GitHub, or GitHub unreachable."""
        text = "UNREACHABLE\n" if unreachable else "".join(f"{tag}\n" for tag in tags)
        (self.root / "remote-tags").write_text(text)

    def git_calls(self) -> list[str]:
        calls = self.root / "git-calls"
        return calls.read_text().splitlines() if calls.exists() else []

    def set_warmup_status(self, version: str, status: int) -> None:
        write_program(self.cache / version / "hooks" / "run", HOOK_RUN_STUB.format(status=status))

    def set_state(self, **state: Any) -> None:
        self.state_file.write_text(json.dumps(state))

    def install_at(self, ref: str) -> None:
        version = ref.lstrip("v")
        self.set_state(
            markets=[github_market(ref)],
            plugins=[plugin_entry(version, self.cache / version)],
        )

    def update_state(self, change: Callable[[dict[str, Any]], None]) -> None:
        state = json.loads(self.state_file.read_text())
        change(state)
        self.state_file.write_text(json.dumps(state))

    def state(self) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(self.state_file.read_text())
        return result

    def calls(self) -> list[list[str]]:
        if not self.calls_file.exists():
            return []
        return [json.loads(line) for line in self.calls_file.read_text().splitlines()]

    def changing_calls(self) -> list[list[str]]:
        return [call for call in self.calls() if "--json" not in call]

    def run(
        self, *args: str, stdin: str | None = None, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        command = ["/bin/sh", str(SCRIPT), *args] if stdin is None else ["/bin/sh", "-s", *args]
        return subprocess.run(
            command,
            input=stdin,
            capture_output=True,
            text=True,
            env={"PATH": f"{self.bin}:{SYSTEM_PATH}", "HOME": str(self.root), **(env or {})},
            check=False,
            timeout=60,
        )


def plugin_entry(version: str, path: Path) -> dict[str, Any]:
    return {
        "id": PLUGIN_ID,
        "version": version,
        "scope": "user",
        "enabled": True,
        "installPath": str(path),
    }


def install_call(tag: str) -> list[str]:
    return [
        "plugin",
        "install",
        "telegram-mcp",
        "--marketplace",
        f"{REPO}#{tag}",
        "--scope",
        "user",
    ]


UNINSTALL_CALL = ["plugin", "uninstall", PLUGIN_ID, "--scope", "user"]
REMOVE_CALL = ["plugin", "marketplace", "remove", "telegram-mcp"]


@pytest.fixture
def rig(home: Path) -> Rig:
    machine = Rig(home)
    machine.setup()
    return machine


def test_a_machine_that_is_not_macos_is_refused_before_claude_is_asked(rig: Rig) -> None:
    rig.setup(system="Linux")
    result = rig.run()
    assert result.returncode == 2
    assert "macOS only" in result.stderr
    assert "Linux" in result.stderr
    assert rig.calls() == []


@pytest.mark.parametrize(
    ("missing", "named", "where"),
    [
        ("uv", "uv was not found", "https://docs.astral.sh/uv/"),
        ("uvx", "uvx was not found", "https://docs.astral.sh/uv/"),
        ("claude", "(claude) was not found", "https://code.claude.com/docs/en/setup"),
    ],
)
def test_a_missing_program_is_named_with_where_to_get_it_and_nothing_is_installed(
    rig: Rig, missing: str, named: str, where: str
) -> None:
    rig.setup(without=(missing,))
    result = rig.run()
    assert result.returncode == 2
    assert named in result.stderr
    assert where in result.stderr
    assert "nothing was installed" in result.stderr
    assert rig.calls() == []
    assert rig.state() == {"markets": [], "plugins": []}


@pytest.mark.parametrize("tag", ["0.1.0", "v1.2", "v01.2.3", "latest", "v1.2.3-rc1", "main"])
def test_a_tag_that_is_not_a_release_tag_is_refused(rig: Rig, tag: str) -> None:
    result = rig.run("--tag", tag)
    assert result.returncode == 2
    assert "is not a release tag" in result.stderr
    assert rig.calls() == []


def test_a_fresh_install_pins_the_default_tag_and_warms_the_guard_up(rig: Rig) -> None:
    result = rig.run()
    assert result.returncode == 0, result.stderr
    assert rig.changing_calls() == [install_call("v0.1.0")]
    assert "telegram-mcp v0.1.0 is installed" in result.stdout
    assert "The guard hook is ready." in result.stdout
    plugin_folder = rig.cache / "0.1.0"
    assert (plugin_folder / "warmup-args").read_text() == "guard\n"
    assert (plugin_folder / "warmup-stdin").read_text() == "{}"
    assert f'--from "{plugin_folder}" telegram-mcp' in result.stdout
    assert "telegram-mcp login" in result.stdout
    assert [m["ref"] for m in rig.state()["markets"]] == ["v0.1.0"]


def test_the_same_tag_again_changes_nothing_and_says_so(rig: Rig) -> None:
    rig.install_at("v0.1.0")
    before = rig.state()
    result = rig.run("--tag", "v0.1.0")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "telegram-mcp v0.1.0 is already installed; nothing changed."
    assert rig.changing_calls() == []
    assert rig.state() == before


def test_a_newer_tag_removes_the_old_copy_then_installs_the_new_one(rig: Rig) -> None:
    rig.install_at("v0.1.0")
    result = rig.run("--tag=v0.2.0")
    assert result.returncode == 0, result.stderr
    assert rig.changing_calls() == [UNINSTALL_CALL, REMOVE_CALL, install_call("v0.2.0")]
    assert "Moving telegram-mcp from v0.1.0 to v0.2.0." in result.stdout
    assert "telegram-mcp v0.2.0 is installed" in result.stdout
    state = rig.state()
    assert [m["ref"] for m in state["markets"]] == ["v0.2.0"]
    assert [p["version"] for p in state["plugins"]] == ["0.2.0"]
    assert (rig.cache / "0.2.0" / "warmup-args").exists()


def test_a_move_checks_the_tag_on_github_before_removing_anything(rig: Rig) -> None:
    rig.install_at("v0.1.0")
    rig.run("--tag", "v0.2.0")
    assert rig.git_calls() == [
        f"ls-remote --exit-code --tags https://github.com/{REPO}.git refs/tags/v0.2.0"
    ]


def test_a_move_to_a_tag_that_does_not_exist_changes_nothing(rig: Rig) -> None:
    rig.install_at("v0.1.0")
    before = rig.state()
    result = rig.run("--tag", "v9.9.9")
    assert result.returncode == 2
    assert "v9.9.9 does not exist" in result.stderr
    assert "Nothing was changed" in result.stderr
    assert rig.changing_calls() == []
    assert rig.state() == before


def test_a_move_with_github_unreachable_changes_nothing(rig: Rig) -> None:
    rig.install_at("v0.1.0")
    rig.set_remote_tags(unreachable=True)
    before = rig.state()
    result = rig.run("--tag", "v0.2.0")
    assert result.returncode == 2
    assert "could not reach" in result.stderr
    assert "git exit 128" in result.stderr
    assert rig.changing_calls() == []
    assert rig.state() == before


def test_a_fresh_install_does_not_need_to_ask_github_first(rig: Rig) -> None:
    rig.set_remote_tags()
    result = rig.run()
    assert result.returncode == 0, result.stderr
    assert rig.git_calls() == []


def test_a_warm_up_past_its_limit_is_ended_with_everything_it_started(rig: Rig) -> None:
    pid_file = rig.root / "slow-child.pid"
    write_program(
        rig.cache / "0.1.0" / "hooks" / "run",
        f"#!/bin/sh\nsleep 30 &\necho $! > '{pid_file}'\nwait\n",
    )
    started = time.monotonic()
    result = rig.run(env={"TELEGRAM_MCP_WARMUP_SECONDS": "2"})
    assert time.monotonic() - started < 20
    assert result.returncode == 0, result.stderr
    assert "warning: the guard hook did not finish its first run" in result.stderr
    assert "Terminated" not in result.stderr + result.stdout
    child = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and process_alive(child):
        time.sleep(0.1)
    assert not process_alive(child)


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_marketplace_at_the_tag_with_the_plugin_missing_installs_the_plugin_only(
    rig: Rig,
) -> None:
    rig.set_state(markets=[github_market("v0.1.0")], plugins=[])
    result = rig.run()
    assert result.returncode == 0, result.stderr
    assert "the plugin is missing" in result.stdout
    assert rig.changing_calls() == [install_call("v0.1.0")]


@pytest.mark.parametrize(
    "market",
    [
        {"name": "telegram-mcp", "source": "directory", "path": "/Users/me/src/telegram-mcp"},
        {
            "name": "telegram-mcp",
            "source": "github",
            "repo": "someone/telegram-mcp",
            "ref": "v0.1.0",
        },
    ],
)
def test_a_marketplace_from_another_source_is_refused_and_left_alone(
    rig: Rig, market: dict[str, str]
) -> None:
    rig.set_state(markets=[market], plugins=[plugin_entry("0.1.0", rig.cache / "0.1.0")])
    before = rig.state()
    result = rig.run()
    assert result.returncode == 2
    assert market.get("path", market.get("repo", "")) in result.stderr
    assert "--uninstall" in result.stderr
    assert rig.changing_calls() == []
    assert rig.state() == before


def test_a_failing_install_stops_with_exit_1_and_claudes_message(rig: Rig) -> None:
    rig.update_state(lambda s: s.update(fail={"install": "network unreachable"}))
    result = rig.run()
    assert result.returncode == 1
    assert "network unreachable" in result.stderr
    assert "'claude plugin install telegram-mcp" in result.stderr
    assert "previous version" not in result.stderr
    assert "is installed" not in result.stdout


def test_a_move_whose_new_install_fails_says_the_old_version_was_removed(rig: Rig) -> None:
    rig.install_at("v0.1.0")
    rig.update_state(lambda s: s.update(fail={"install": "tag not found"}))
    result = rig.run("--tag", "v0.2.0")
    assert result.returncode == 1
    assert "tag not found" in result.stderr
    assert "The previous version (v0.1.0) was removed and v0.2.0 is not installed" in result.stderr
    assert (
        f"curl -fsSL https://raw.githubusercontent.com/{REPO}/v0.1.0/install.sh | sh"
        in result.stderr
    )


def test_a_failing_uninstall_during_a_move_stops_before_anything_is_removed(rig: Rig) -> None:
    rig.install_at("v0.1.0")
    rig.update_state(lambda s: s.update(fail={"uninstall": "busy"}))
    result = rig.run("--tag", "v0.2.0")
    assert result.returncode == 1
    assert "nothing was moved" in result.stderr
    assert rig.changing_calls() == [UNINSTALL_CALL]


def test_an_install_reported_done_but_not_at_the_tag_fails(rig: Rig) -> None:
    rig.update_state(lambda s: s.update(install_does_nothing=True))
    result = rig.run()
    assert result.returncode == 1
    assert "not installed at v0.1.0" in result.stderr
    assert not (rig.cache / "0.1.0" / "warmup-args").exists()


def test_a_failing_guard_warm_up_only_warns(rig: Rig) -> None:
    rig.set_warmup_status("0.1.0", 2)
    result = rig.run()
    assert result.returncode == 0, result.stderr
    assert "telegram-mcp v0.1.0 is installed" in result.stdout
    assert "warning: the guard hook did not finish its first run (exit 2)" in result.stderr
    assert rig.changing_calls() == [install_call("v0.1.0")]


@pytest.mark.parametrize(
    ("failing", "command"),
    [
        ("marketplace list", "claude plugin marketplace list --json"),
        ("plugin list", "claude plugin list --json"),
    ],
)
def test_a_failing_list_stops_with_exit_1_before_any_change(
    rig: Rig, failing: str, command: str
) -> None:
    rig.update_state(lambda s: s.update(fail={failing: "not logged in"}))
    result = rig.run()
    assert result.returncode == 1
    assert "not logged in" in result.stderr
    assert f"'{command}' failed (exit 1)" in result.stderr
    assert rig.changing_calls() == []


def test_a_list_that_is_not_json_stops_with_exit_1(rig: Rig) -> None:
    rig.update_state(lambda s: s.update(raw_markets="Marketplaces: none"))
    result = rig.run()
    assert result.returncode == 1
    assert "could not read Claude Code's plugin lists" in result.stderr
    assert rig.changing_calls() == []


def test_uninstall_removes_the_plugin_and_the_marketplace_and_says_what_stays(rig: Rig) -> None:
    rig.install_at("v0.1.0")
    result = rig.run("--uninstall")
    assert result.returncode == 0, result.stderr
    assert rig.changing_calls() == [UNINSTALL_CALL, REMOVE_CALL]
    assert rig.state() == {"markets": [], "plugins": []}
    assert "Removed the telegram-mcp plugin" in result.stdout
    assert "stays in the macOS Keychain (service telegram-mcp)" in result.stdout
    assert "'telegram-mcp logout'" in result.stdout
    assert "security delete-generic-password -s telegram-mcp -a api_hash" in result.stdout
    assert f"{rig.root}/Library/Application Support/telegram-mcp/" in result.stdout


def test_uninstall_also_removes_a_copy_installed_from_a_local_folder(rig: Rig) -> None:
    rig.set_state(
        markets=[{"name": "telegram-mcp", "source": "directory", "path": "/src/telegram-mcp"}],
        plugins=[],
    )
    result = rig.run("--uninstall")
    assert result.returncode == 0, result.stderr
    assert rig.changing_calls() == [REMOVE_CALL]


def test_uninstall_with_nothing_installed_changes_nothing(rig: Rig) -> None:
    result = rig.run("--uninstall")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "telegram-mcp is not installed; nothing changed."
    assert rig.changing_calls() == []


def test_a_failing_uninstall_stops_with_exit_1(rig: Rig) -> None:
    rig.install_at("v0.1.0")
    rig.update_state(lambda s: s.update(fail={"marketplace remove": "settings locked"}))
    result = rig.run("--uninstall")
    assert result.returncode == 1
    assert "settings locked" in result.stderr
    assert "'claude plugin marketplace remove telegram-mcp' failed" in result.stderr


def test_uninstall_needs_only_claude_and_refuses_a_tag(rig: Rig) -> None:
    rig.setup(without=("uv", "uvx"))
    assert rig.run("--uninstall").returncode == 0
    result = rig.run("--uninstall", "--tag", "v0.1.0")
    assert result.returncode == 2
    assert "cannot be used together" in result.stderr


def test_help_prints_the_usage(rig: Rig) -> None:
    result = rig.run("--help")
    assert result.returncode == 0
    assert result.stdout.startswith("Usage: install.sh [--tag vX.Y.Z] [--uninstall] [--help]")
    assert rig.calls() == []


@pytest.mark.parametrize("args", [("--force",), ("v0.1.0",), ("--tag",)])
def test_an_unknown_or_incomplete_argument_is_refused(rig: Rig, args: tuple[str, ...]) -> None:
    result = rig.run(*args)
    assert result.returncode == 2
    assert result.stderr.startswith("telegram-mcp install: ")
    assert rig.calls() == []


def test_the_script_works_when_piped_into_sh(rig: Rig) -> None:
    """`curl … | sh -s -- --tag …`: the script arrives on standard input, which no step reads."""
    result = rig.run("--", "--tag", "v0.2.0", stdin=SCRIPT.read_text())
    assert result.returncode == 0, result.stderr
    assert rig.changing_calls() == [install_call("v0.2.0")]
    assert "telegram-mcp v0.2.0 is installed" in result.stdout


def test_the_script_is_executable_posix_sh() -> None:
    assert SCRIPT.read_text().startswith("#!/bin/sh\n")
    assert os.access(SCRIPT, os.X_OK)
    subprocess.run(["/bin/sh", "-n", str(SCRIPT)], check=True)
