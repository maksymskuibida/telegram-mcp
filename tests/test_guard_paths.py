"""The file, search and fetch tools (BR1.1 to BR1.5, BR5.1, BR5.3 to BR5.5, NFR7.1).

Every case runs on a temporary home folder, once with the default outbox and downloads folders
and once with configured ones."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from support.guard_home import GuardHome, make_guard_home
from telegram_mcp.guard import Route

FILE_TOOLS = ("Read", "Write", "Edit", "MultiEdit")


@pytest.fixture(params=["default folders", "configured folders"])
def gh(home: Path, request: pytest.FixtureRequest) -> GuardHome:
    return make_guard_home(home, configured=request.param == "configured folders")


def file_call(gh: GuardHome, tool: str, path: str, cwd: str | None = None) -> dict[str, Any]:
    if tool == "NotebookEdit":
        return gh.call(tool, cwd, notebook_path=path, new_source="")
    if tool == "Write":
        return gh.call(tool, cwd, file_path=path, content="x")
    if tool == "MultiEdit":
        return gh.call(tool, cwd, file_path=path, edits=[])
    if tool == "Edit":
        return gh.call(tool, cwd, file_path=path, old_string="a", new_string="b")
    return gh.call(tool, cwd, file_path=path)


# BR1.1, BR1.4: file tools

REFUSED_FILE_PATHS = [
    # (path, cwd)
    ("{data}/activity.log", None),
    ("{data}/config.toml", None),
    ("{data}", None),
    ("{data}/", None),
    ("{data}/new-file.txt", None),
    ("{data}/sub/folder/new.txt", None),
    ("~/Library/Application Support/telegram-mcp/config.toml", None),
    ("~/Library/Application Support/telegram-mcp", None),
    ("telegram-mcp/activity.log", "{home}/Library/Application Support"),
    ("activity.log", "{data}"),
    ("./broker.secret", "{data}"),
    ("../telegram-mcp/broker.err", "{data}"),
    ("../Library/Application Support/telegram-mcp/activity.log", "{project}"),
    ("{home}/Library/Application Support/telegram-mcp/../telegram-mcp/activity.log", None),
    ("{project}/../Library/./Application Support//telegram-mcp/activity.log", None),
    ("{home}/link/activity.log", None),
    ("{home}/link", None),
    ("~/link/config.toml", None),
    ("{home}/loglink", None),
    ("link/broker.sock", "{home}"),
    ("{home}/link/../telegram-mcp/activity.log", None),
    ("~/library/application support/TELEGRAM-MCP/activity.log", None),
    ("{home}/LIBRARY/Application Support/Telegram-Mcp", None),
]


@pytest.mark.parametrize("tool", [*FILE_TOOLS, "NotebookEdit"])
@pytest.mark.parametrize(("path", "cwd"), REFUSED_FILE_PATHS)
def test_a_file_tool_reaching_the_data_folder_is_refused(
    gh: GuardHome, tool: str, path: str, cwd: str | None
) -> None:
    call = file_call(gh, tool, gh.fill(path), None if cwd is None else gh.fill(cwd))
    decision = gh.decide(call)
    assert decision.verdict == "refuse"
    assert decision.route == Route.DATA_PATH


PASSED_FILE_PATHS = [
    ("{project}/README.md", None),
    ("README.md", None),
    ("src/new_module.py", None),
    ("~/notes.md", None),
    ("{home}/Library/Application Support/telegram-mcp-old/activity.log", None),
    ("{home}/Library/Application Support/Claude/settings.json", None),
    ("~/telegram-mcp-notes/telegram-mcp/README.md", None),
    ("~/.claude/plugins/cache/telegram-mcp/hooks/run", None),
    ("~/.claude/plugins/marketplaces/telegram-mcp/src/telegram_mcp/guard/rules.py", None),
    ("~/.cache/uv/archive-v0/x/telegram_mcp/hooks.py", None),
    ("/etc/hosts", None),
    ("activity.log", "{project}"),
    ("../project/README.md", "{project}/src"),
]


@pytest.mark.parametrize("tool", [*FILE_TOOLS, "NotebookEdit"])
@pytest.mark.parametrize(("path", "cwd"), PASSED_FILE_PATHS)
def test_a_file_tool_elsewhere_passes(
    gh: GuardHome, tool: str, path: str, cwd: str | None
) -> None:
    call = file_call(gh, tool, gh.fill(path), None if cwd is None else gh.fill(cwd))
    assert gh.decide(call).verdict == "pass"


@pytest.mark.parametrize("tool", FILE_TOOLS)
def test_the_outbox_and_downloads_folders_pass(gh: GuardHome, tool: str) -> None:
    for path in (gh.outbox / "photo.jpg", gh.outbox / "new.txt", gh.downloads / "file.pdf"):
        assert gh.decide(file_call(gh, tool, str(path))).verdict == "pass"


@pytest.mark.parametrize("tool", ["Write", "Edit"])
def test_file_contents_naming_the_data_folder_pass(gh: GuardHome, tool: str) -> None:
    text = f"cat '{gh.paths.log}'\nimport telegram_mcp\ntelegram-mcp log\nbroker.secret"
    if tool == "Write":
        call = gh.call(tool, file_path=str(gh.project / "script.sh"), content=text)
    else:
        call = gh.call(
            tool, file_path=str(gh.project / "README.md"), old_string="readme", new_string=text
        )
    assert gh.decide(call).verdict == "pass"


def test_a_link_loop_cannot_be_decided_and_is_refused(gh: GuardHome) -> None:
    decision = gh.decide(gh.call("Read", file_path=str(gh.home / "loop" / "x")))
    assert decision.verdict == "refuse"
    assert decision.route == Route.DATA_PATH


@pytest.fixture
def locked_folder(gh: GuardHome) -> Iterator[Path]:
    folder = gh.home / "locked"
    (folder / "inner").mkdir(parents=True)
    os.chmod(folder, 0)
    try:
        yield folder
    finally:
        os.chmod(folder, 0o700)


def test_a_path_that_cannot_be_resolved_for_another_reason_is_refused(
    gh: GuardHome, locked_folder: Path
) -> None:
    decision = gh.decide(gh.call("Read", file_path=str(locked_folder / "inner" / "x")))
    assert decision.verdict == "refuse"
    assert decision.route == Route.DATA_PATH


def test_a_path_that_does_not_exist_yet_is_decided_by_its_text(gh: GuardHome) -> None:
    missing = gh.home / "new" / "folder" / "file.txt"
    assert gh.decide(gh.call("Write", file_path=str(missing), content="x")).verdict == "pass"


# BR1.2, BR1.5, BR5.3: searches

REFUSED_SEARCHES: list[tuple[str, dict[str, Any], str | None]] = [
    ("Grep", {"pattern": "token", "path": "{data}"}, None),
    ("Grep", {"pattern": "token", "path": "{data}/activity.log"}, None),
    ("Grep", {"pattern": "token", "path": "~/Library/Application Support/telegram-mcp"}, None),
    ("Grep", {"pattern": "token", "path": "{home}/link"}, None),
    ("Grep", {"pattern": "token"}, "{data}"),
    ("Grep", {"pattern": "token", "path": "telegram-mcp"}, "{home}/Library/Application Support"),
    ("Grep", {"pattern": "x", "path": "{home}", "glob": "**/Application Support/telegram-mcp/*"}, None),
    ("Grep", {"pattern": "x", "path": "{home}", "glob": "Library/Application Support/telegram-mcp/*"}, None),
    ("Grep", {"pattern": "x", "path": "{home}", "glob": "**/Application*/telegram-mcp/activity.log"}, None),
    ("Glob", {"pattern": "{data}/*"}, None),
    ("Glob", {"pattern": "~/Library/Application Support/telegram-mcp/*"}, None),
    ("Glob", {"pattern": "**/Application Support/telegram-mcp/*"}, None),
    ("Glob", {"pattern": "**/application support/telegram-mcp/*", "path": "{home}"}, None),
    ("Glob", {"pattern": "**/Application*/telegram-mcp/activity.log", "path": "/"}, None),
    ("Glob", {"pattern": "**/tele*/activity.log", "path": "{home}"}, None),
    ("Glob", {"pattern": "**/telegram-mcp/README.md", "path": "{home}"}, None),
    ("Glob", {"pattern": "~/Library/App*/tele*/activity.log"}, None),
    ("Glob", {"pattern": "~/Library/Application Support/*"}, None),
    ("Glob", {"pattern": "{home}/Library/*/telegram-mcp"}, None),
    ("Glob", {"pattern": "~/Library/Application Support/tele[g]ram-mcp/**"}, None),
    ("Glob", {"pattern": "~/Library/Application Support/{Claude,telegram-mcp}/*"}, None),
    ("Glob", {"pattern": "*", "path": "{data}"}, None),
    ("Glob", {"pattern": "*.log"}, "{data}"),
    ("Glob", {"pattern": "telegram-mcp/*"}, "{home}/Library/Application Support"),
    ("Glob", {"pattern": "*", "path": "{home}/link"}, None),
    ("Glob", {"pattern": "{home}/link/*"}, None),
]


@pytest.mark.parametrize(("tool", "tool_input", "cwd"), REFUSED_SEARCHES)
def test_a_search_into_the_data_folder_is_refused(
    gh: GuardHome, tool: str, tool_input: dict[str, Any], cwd: str | None
) -> None:
    filled = {k: gh.fill(v) for k, v in tool_input.items()}
    call = gh.call(tool, None if cwd is None else gh.fill(cwd), **filled)
    decision = gh.decide(call)
    assert decision.verdict == "refuse"
    assert decision.route == Route.DATA_PATH


PASSED_SEARCHES: list[tuple[str, dict[str, Any], str | None]] = [
    ("Grep", {"pattern": "token", "path": "~"}, None),
    ("Grep", {"pattern": "token", "path": "{home}/Library"}, None),
    ("Grep", {"pattern": "token", "path": "{home}/Library/Application Support"}, None),
    ("Grep", {"pattern": "telegram-mcp/activity.log"}, None),
    ("Grep", {"pattern": "Library/Application Support/telegram-mcp", "path": "{project}"}, None),
    ("Grep", {"pattern": "x", "path": "{home}", "glob": "*.log"}, None),
    ("Grep", {"pattern": "x", "path": "{home}", "glob": "**/*.log"}, None),
    ("Grep", {"pattern": "x", "glob": "src/**/*.py"}, None),
    ("Grep", {"pattern": "x", "path": "{home}/Downloads"}, None),
    ("Glob", {"pattern": "~/Library/**"}, None),
    ("Glob", {"pattern": "**/*.log", "path": "{home}"}, None),
    ("Glob", {"pattern": "**/*", "path": "{home}"}, None),
    ("Glob", {"pattern": "**/*.py"}, None),
    ("Glob", {"pattern": "src/**/*"}, None),
    ("Glob", {"pattern": "~/Library/Application Support/Claude/*"}, None),
    ("Glob", {"pattern": "~/Library/Application Support/telegram-mcp-old/*"}, None),
    ("Glob", {"pattern": "*", "path": "{home}"}, None),
    ("Glob", {"pattern": "~/Downloads/telegram-mcp/**"}, None),
    ("Glob", {"pattern": "**/telegram-mcp/README.md", "path": "{project}"}, None),
    ("Glob", {"pattern": "*.log"}, "{home}/Library/Application Support"),
]


@pytest.mark.parametrize(("tool", "tool_input", "cwd"), PASSED_SEARCHES)
def test_a_search_elsewhere_or_above_the_data_folder_passes(
    gh: GuardHome, tool: str, tool_input: dict[str, Any], cwd: str | None
) -> None:
    filled = {k: gh.fill(v) for k, v in tool_input.items()}
    call = gh.call(tool, None if cwd is None else gh.fill(cwd), **filled)
    assert gh.decide(call).verdict == "pass"


def test_searches_in_the_outbox_and_downloads_folders_pass(gh: GuardHome) -> None:
    for folder in (gh.outbox, gh.downloads):
        assert gh.decide(gh.call("Grep", pattern="x", path=str(folder))).verdict == "pass"
        assert gh.decide(gh.call("Glob", pattern=f"{folder}/**/*.jpg")).verdict == "pass"


# BR1.3: fetches

REFUSED_URLS = [
    "file://{data}/activity.log",
    "file://{home}/Library/Application%20Support/telegram-mcp/config.toml",
    "FILE://{data}/broker.secret",
    "file:{data}/config.toml",
    "file://localhost{data}/activity.log",
    "file://{home}/link/activity.log",
    "file://{home}/library/application%20support/TELEGRAM-MCP",
]


@pytest.mark.parametrize("url", REFUSED_URLS)
def test_a_file_url_into_the_data_folder_is_refused(gh: GuardHome, url: str) -> None:
    decision = gh.decide(gh.call("WebFetch", url=gh.fill(url), prompt="read it"))
    assert decision.verdict == "refuse"
    assert decision.route == Route.DATA_PATH


PASSED_URLS = [
    "https://example.com/",
    "https://example.com/Library/Application%20Support/telegram-mcp/activity.log",
    "http://localhost:8080/telegram-mcp/log",
    "file://{project}/README.md",
    "file://{home}/Downloads/telegram-mcp/outbox/photo.jpg",
]


@pytest.mark.parametrize("url", PASSED_URLS)
def test_other_fetches_pass(gh: GuardHome, url: str) -> None:
    assert gh.decide(gh.call("WebFetch", url=gh.fill(url), prompt="read it")).verdict == "pass"
