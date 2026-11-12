"""`telegram-mcp log`: rotated files oldest first, readable and JSON views, escaped control
characters, unreadable lines, `--follow` across rotations, streaming and read-only access."""

from __future__ import annotations

import json
import os
import stat
import unicodedata
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from telegram_mcp.log_reader import (
    POLL_SECONDS,
    LogOptions,
    log_files,
    run_log,
)
from telegram_mcp.paths import FixedPaths

TS = "2026-11-12T10:41:07.123Z"


def local(ts: str = TS) -> str:
    return datetime.fromisoformat(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def entry(tool: str = "tg_chats_list", **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ts": TS,
        "client": "mcp-server:claude-code",
        "event": "result",
        "tool": tool,
        "chat_id": None,
        "approval_id": None,
        "content": None,
        "decision": None,
        "result": "ok",
    }
    return {**base, **fields}


def line_of(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


@pytest.fixture
def paths(home: Path) -> FixedPaths:
    folder = FixedPaths.from_home(home)
    folder.data_dir.mkdir(parents=True, mode=0o700)
    return folder


def write(path: Path, *values: dict[str, Any] | str, mode: str = "w") -> None:
    with path.open(mode, encoding="utf-8") as handle:
        for value in values:
            handle.write(value if isinstance(value, str) else line_of(value))


def rotated(paths: FixedPaths, number: int) -> Path:
    return paths.log.with_name(f"activity.log.{number}")


class Run:
    def __init__(self) -> None:
        self.out: list[str] = []
        self.err: list[str] = []
        self.sleeps: list[float] = []

    def __call__(
        self,
        paths: FixedPaths,
        options: LogOptions | None = None,
        *,
        on_poll: list[Callable[[], None]] | None = None,
        opener: Callable[[Path], BinaryIO] | None = None,
    ) -> int:
        steps = list(on_poll or [])

        def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)
            if not steps:
                raise KeyboardInterrupt
            steps.pop(0)()

        extra: dict[str, Any] = {"opener": opener} if opener is not None else {}
        return run_log(
            paths,
            options or LogOptions(),
            say=self.out.append,
            complain=self.err.append,
            sleep=sleep,
            **extra,
        )

    def tools(self) -> list[str]:
        return [line.split("  ")[2] for line in self.out]


# One-shot reading ----------------------------------------------------------------------------


def test_with_no_log_it_says_nothing_was_logged(paths: FixedPaths) -> None:
    run = Run()
    assert run(paths) == 0
    assert run.out == [f"Nothing has been logged yet ({paths.log})."]


def test_rotated_files_are_read_oldest_first(paths: FixedPaths) -> None:
    write(rotated(paths, 4), entry("a"))
    write(rotated(paths, 2), entry("b"))
    write(rotated(paths, 1), entry("c"))
    write(paths.log, entry("d"), entry("e"))
    run = Run()
    assert run(paths) == 0
    assert run.tools() == ["a", "b", "c", "d", "e"]


def test_lines_keeps_the_last_n_across_files_oldest_first(paths: FixedPaths) -> None:
    write(rotated(paths, 1), entry("a"), entry("b"))
    write(paths.log, entry("c"))
    run = Run()
    assert run(paths, LogOptions(lines=2)) == 0
    assert run.tools() == ["b", "c"]


def test_the_command_reads_only_the_fixed_files(paths: FixedPaths) -> None:
    names = [path.name for path in log_files(paths)]
    assert names == [f"activity.log{suffix}" for suffix in ("", ".1", ".2", ".3", ".4")]
    assert all(path.parent == paths.data_dir for path in log_files(paths))


def test_an_entry_prints_its_present_fields_and_content_in_local_time(paths: FixedPaths) -> None:
    prompt = entry(
        SEND := "tg_messages_send",
        event="prompt",
        chat_id=123456,
        approval_id="7f3a",
        content='Send "hello" to Family?',
    )
    decision = entry(SEND, event="decision", chat_id=123456, approval_id="7f3a", decision="allowed")
    result = entry(SEND, chat_id=123456, client="mcp-server", content="hello")
    bare = entry(client="broker", result="timed_out")
    write(paths.log, prompt, decision, result, bare)
    run = Run()
    assert run(paths) == 0
    when = local()
    assert run.out == [
        f"{when}  prompt  {SEND}  chat 123456  approval 7f3a  client mcp-server:claude-code"
        ' | Send "hello" to Family?',
        f"{when}  decision  {SEND}  chat 123456  approval 7f3a  allowed  "
        "client mcp-server:claude-code",
        f"{when}  result  {SEND}  chat 123456  → ok  client mcp-server | hello",
        f"{when}  result  tg_chats_list  → timed_out  client broker",
    ]


def test_multi_line_content_continues_on_indented_lines(paths: FixedPaths) -> None:
    write(paths.log, entry(content="first\nsecond\n"))
    run = Run()
    run(paths)
    assert run.out == [
        f"{local()}  result  tg_chats_list  → ok  client mcp-server:claude-code\n"
        "    first\n    second\n    "
    ]


def test_every_hidden_character_and_the_backslash_are_shown_as_escapes(
    paths: FixedPaths,
) -> None:
    hostile = "a\\b \x1b[31mred \x7f \x9b2J \u202eevil \u200d \rback \u2028 tab\tend"
    write(paths.log, entry("tool\x1b]0;title\x07", content=hostile))
    run = Run()
    run(paths)
    shown = run.out[0]
    assert shown.endswith(
        "| a\\\\b \\x1b[31mred \\x7f \\x9b2J \\u202eevil \\u200d \\x0dback \\u2028 tab\tend"
    )
    assert "tool\\x1b]0;title\\x07" in shown
    assert not any(
        unicodedata.category(char) in ("Cc", "Cf") and char not in "\t" for char in shown
    )


def test_json_prints_each_entry_as_valid_json_with_no_raw_hidden_character(
    paths: FixedPaths,
) -> None:
    stored = [
        entry(content="a\\b \x1b[2J \x7f \x9b \u202e \u2028 \u200d\nnext"),
        entry("t", event="something_new", extra_field={"x": 1}),
    ]
    write(paths.log, *stored)
    run = Run()
    assert run(paths, LogOptions(as_json=True)) == 0
    assert [json.loads(line) for line in run.out] == stored
    for line in run.out:
        assert not any(unicodedata.category(char) in ("Cc", "Cf") for char in line)
        line.encode("utf-8")


def test_a_lone_surrogate_in_a_line_still_prints_valid_utf8(paths: FixedPaths) -> None:
    write(paths.log, '{"ts":"' + TS + '","event":"call","tool":"t","content":"\\ud800"}\n')
    for as_json in (False, True):
        run = Run()
        run(paths, LogOptions(as_json=as_json))
        run.out[0].encode("utf-8")
        assert "\\ud800" in run.out[0]


@pytest.mark.parametrize(
    "bad",
    [
        "not json at all \x1b[0m",
        "[1, 2]",
        '{"ts": "yesterday", "event": "call", "tool": "t"}',
        '{"ts": "' + TS + '", "event": "call"}',
    ],
)
def test_an_unreadable_line_is_shown_in_both_views_and_reading_goes_on(
    paths: FixedPaths, bad: str
) -> None:
    write(paths.log, bad + "\n", entry("after"))
    run = Run()
    assert run(paths) == 0
    assert run.out[0].startswith("(unreadable entry) ")
    assert "\x1b" not in run.out[0]
    assert run.out[1].split("  ")[2] == "after"
    run = Run()
    run(paths, LogOptions(as_json=True))
    assert json.loads(run.out[0]) == {"unreadable": bad}
    assert json.loads(run.out[1])["tool"] == "after"


def test_an_unknown_event_and_extra_fields_print_generically(paths: FixedPaths) -> None:
    write(paths.log, entry("logout", event="rotated", client="cli", surprise="ignored"))
    run = Run()
    run(paths)
    assert run.out == [f"{local()}  rotated  logout  → ok  client cli"]


def test_a_rotation_while_opening_neither_repeats_nor_drops_an_entry(paths: FixedPaths) -> None:
    write(rotated(paths, 1), entry("older"))
    write(paths.log, entry("current"))

    def rotating_open(path: Path) -> BinaryIO:
        handle = path.open("rb")
        if path == paths.log:  # the logger rotates right after the live file was opened
            rotated(paths, 1).rename(rotated(paths, 2))
            paths.log.rename(rotated(paths, 1))
            write(paths.log, entry("newer"))
        return handle

    run = Run()
    assert run(paths, opener=rotating_open) == 0
    assert run.tools() == ["older", "current"]


def test_an_unreadable_file_is_an_error_on_standard_error(paths: FixedPaths) -> None:
    write(rotated(paths, 1), entry("a"))
    rotated(paths, 1).chmod(0)
    run = Run()
    assert run(paths) == 1
    assert run.out == []
    assert run.err == [f"The log could not be read ({rotated(paths, 1)}: Permission denied)."]


class NoWholeReads:
    """A file that can only be read a line at a time."""

    def __init__(self, path: Path) -> None:
        self._handle = path.open("rb")
        self.lines_read = 0

    def readline(self, size: int = -1) -> bytes:
        self.lines_read += 1
        return self._handle.readline(size)

    def read(self, size: int = -1) -> bytes:
        raise AssertionError("the log must never be read whole")

    def readlines(self, hint: int = -1) -> list[bytes]:
        raise AssertionError("the log must never be read whole")

    def fileno(self) -> int:
        return self._handle.fileno()

    def close(self) -> None:
        self._handle.close()


def test_two_megabytes_are_streamed_a_line_at_a_time(paths: FixedPaths) -> None:
    filler = "x" * 900
    count = 2_400
    write(paths.log, *(entry(f"t{n}", content=filler) for n in range(count)))
    assert paths.log.stat().st_size > 2_000_000
    opened: list[NoWholeReads] = []

    def opener(path: Path) -> BinaryIO:
        handle = NoWholeReads(path)
        opened.append(handle)
        return handle  # type: ignore[return-value]

    run = Run()
    assert run(paths, LogOptions(lines=3), opener=opener) == 0
    assert run.tools() == [f"t{n}" for n in range(count - 3, count)]
    assert opened[0].lines_read == count + 1


def test_the_command_never_writes_to_the_data_folder(paths: FixedPaths) -> None:
    write(paths.log, entry("a"))
    write(rotated(paths, 1), entry("b"))
    paths.log.chmod(0o600)

    def state() -> dict[str, tuple[int, int, float]]:
        return {
            p.name: (stat.S_IMODE(p.stat().st_mode), p.stat().st_size, p.stat().st_mtime)
            for p in paths.data_dir.iterdir()
        }

    before = state()
    Run()(paths, LogOptions(follow=True), on_poll=[lambda: None])
    Run()(paths, LogOptions(as_json=True, lines=1))
    assert state() == before


# --follow ------------------------------------------------------------------------------------


def test_follow_prints_appended_entries_every_half_second(paths: FixedPaths) -> None:
    write(paths.log, entry("start"))
    run = Run()
    code = run(
        paths,
        LogOptions(follow=True),
        on_poll=[
            lambda: write(paths.log, entry("one"), mode="a"),
            lambda: write(paths.log, entry("two"), entry("three"), mode="a"),
        ],
    )
    assert code == 0
    assert run.tools() == ["start", "one", "two", "three"]
    assert run.sleeps == [POLL_SECONDS] * 3


def test_follow_finishes_the_old_file_before_the_new_one_after_rotations(
    paths: FixedPaths,
) -> None:
    write(paths.log, entry("start"))

    def append_then_rotate(tool: str, fresh: str) -> Callable[[], None]:
        def step() -> None:
            write(paths.log, entry(tool), mode="a")
            for number in (3, 2, 1):
                if rotated(paths, number).exists():
                    rotated(paths, number).rename(rotated(paths, number + 1))
            paths.log.rename(rotated(paths, 1))
            write(paths.log, entry(fresh))

        return step

    run = Run()
    run(
        paths,
        LogOptions(follow=True),
        on_poll=[
            append_then_rotate("before-first", "after-first"),
            append_then_rotate("before-second", "after-second"),
            lambda: write(paths.log, entry("last"), mode="a"),
        ],
    )
    assert run.tools() == [
        "start",
        "before-first",
        "after-first",
        "before-second",
        "after-second",
        "last",
    ]


def test_follow_holds_a_partial_line_until_it_is_complete(paths: FixedPaths) -> None:
    whole = line_of(entry("split"))
    write(paths.log, entry("start"), whole[:20])
    run = Run()
    seen: list[int] = []
    run(
        paths,
        LogOptions(follow=True),
        on_poll=[
            lambda: seen.append(len(run.out)),
            lambda: write(paths.log, whole[20:], mode="a"),
        ],
    )
    assert seen == [1]
    assert run.tools() == ["start", "split"]


def test_follow_waits_for_a_log_that_does_not_exist_yet(paths: FixedPaths) -> None:
    run = Run()
    run(
        paths,
        LogOptions(follow=True, as_json=True),
        on_poll=[lambda: None, lambda: write(paths.log, entry("late"))],
    )
    assert run.out[0] == f"Nothing has been logged yet ({paths.log})."
    assert json.loads(run.out[1])["tool"] == "late"


def test_follow_with_lines_starts_from_the_last_entries(paths: FixedPaths) -> None:
    write(paths.log, entry("a"), entry("b"), entry("c"))
    run = Run()
    run(
        paths,
        LogOptions(lines=1, follow=True),
        on_poll=[lambda: write(paths.log, entry("d"), mode="a")],
    )
    assert run.tools() == ["c", "d"]
    assert os.access(paths.log, os.R_OK)
