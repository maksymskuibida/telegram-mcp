"""`telegram-mcp logout` and `telegram-mcp status`, written before the code (team Testing
Posture: they decide whether the session is ended and the Keychain cleared).

In-process brokers on the fake gateway, the in-memory keyring and a scripted terminal; a
scripted socket broker stands in where a connection must drop or never answer."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from support.brokers import make_broker, save_fake_session
from support.memory_keyring import MemoryKeyring
from support.rpc import RawClient
from telegram_mcp.broker.client import BrokerClient, Timeouts
from telegram_mcp.broker.lock import BrokerLock, LockState
from telegram_mcp.broker.server import Broker
from telegram_mcp.gateway.fake import FakeGateway
from telegram_mcp.gateway.models import FloodWait, GatewayError, GatewayTimeout, TelegramError
from telegram_mcp.login import Terminal
from telegram_mcp.paths import FixedPaths
from telegram_mcp.session_commands import logout, status

ACCOUNT = "Test Person (@tester, id 4242)"
UNREAD = "an account whose name could not be read"
FAST = Timeouts(hello=1, answer=1, check=1, call=1, start=1, status=1, logout=1)
LOGIN_RUNNING_LOGOUT = "A login is running in another terminal; finish or cancel it first."
LOGIN_RUNNING_STATUS = "A login is running in another terminal."
UNKNOWN = "The logout was sent but its result is unknown; run `telegram-mcp status` to check."
NOT_CONFIRMED = (
    "This Mac is logged out: the session was deleted from the Keychain. Telegram did not "
    "confirm the logout ({reason}), so the session may still be listed on Telegram's side. "
    "To end it, open Telegram on another device → Settings → Devices and terminate this "
    "session."
)


class ScriptedTerminal:
    """Answers prompts in order; an exception in the answers is raised from ``ask``."""

    def __init__(self, answers: list[str | BaseException] | None = None) -> None:
        self.answers: Iterator[str | BaseException] = iter(answers or [])
        self.prompts: list[str] = []
        self.output: list[str] = []

    def terminal(self) -> Terminal:
        return Terminal(ask=self._ask, ask_secret=self._ask, say=self.output.append)

    def _ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        answer = next(self.answers)
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture
def paths(home: Path) -> FixedPaths:
    return FixedPaths.from_home(home)


@pytest.fixture
async def brokers() -> AsyncIterator[list[Broker]]:
    started: list[Broker] = []
    yield started
    for broker in started:
        await broker.stop()


def in_process(
    paths: FixedPaths, gateway: FakeGateway, started: list[Broker], **options: Any
) -> BrokerClient:
    """A `cli` client that starts an in-process broker when none runs."""

    def spawn() -> None:
        broker = make_broker(paths, gateway, **options)
        started.append(broker)
        asyncio.get_running_loop().create_task(broker.start())

    return BrokerClient(paths, "cli", spawn=spawn, timeouts=FAST)


def no_broker(paths: FixedPaths) -> BrokerClient:
    def spawn() -> None:
        pytest.fail("no broker may be started here")

    return BrokerClient(paths, "cli", spawn=spawn, timeouts=FAST)


@contextlib.asynccontextmanager
async def scripted_broker(
    paths: FixedPaths, replies: dict[str, list[Any]]
) -> AsyncIterator[list[str]]:
    """A socket that answers hello, then each method with its next scripted reply: a result,
    "drop" (close the connection) or "hang" (never answer). Yields the methods received."""
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.secret.write_text("0" * 64)
    received: list[str] = []
    done = asyncio.Event()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(ConnectionError, OSError):
            while line := await reader.readline():
                message = json.loads(line)
                method = message["method"]
                received.append(method)
                if method == "hello":
                    reply: Any = {"broker_version": "test", "pid": 4321}
                else:
                    queue = replies.get(method, [])
                    reply = queue.pop(0) if queue else "hang"
                if reply == "drop":
                    writer.close()
                    return
                if reply == "hang":
                    await done.wait()
                    return
                answer = {"jsonrpc": "2.0", "id": message["id"], "result": reply}
                writer.write(json.dumps(answer).encode() + b"\n")
                await writer.drain()

    server = await asyncio.start_unix_server(serve, path=str(paths.socket))
    try:
        yield received
    finally:
        done.set()
        server.close()


def status_reply(state: str = "logged_in", **fields: Any) -> dict[str, Any]:
    return {
        "logged_in": state == "logged_in",
        "account": ACCOUNT if state == "logged_in" else None,
        "clients": 1,
        "idle_seconds": 0,
        "session_state": state,
        "cause": None,
        "pid": 4321,
        "mcp_clients": 0,
        **fields,
    }


async def run_logout(
    paths: FixedPaths, script: ScriptedTerminal, client: BrokerClient, *, yes: bool = False
) -> int:
    try:
        return await logout(paths, script.terminal(), yes=yes, client=client)
    finally:
        await client.close()


async def run_status(paths: FixedPaths, script: ScriptedTerminal, client: BrokerClient) -> int:
    try:
        return await status(paths, script.terminal(), client=client)
    finally:
        await client.close()


# Before anything is asked ----------------------------------------------------------------


async def test_a_running_login_refuses_both_commands_without_a_broker(
    paths: FixedPaths, memory_keyring: MemoryKeyring
) -> None:
    save_fake_session()
    paths.data_dir.mkdir(parents=True)
    login_lock = BrokerLock(paths.lock)
    assert login_lock.try_hold(LockState.LOGIN)
    try:
        script = ScriptedTerminal()
        assert await run_logout(paths, script, no_broker(paths), yes=True) == 1
        assert script.output == [LOGIN_RUNNING_LOGOUT]
        script = ScriptedTerminal()
        assert await run_status(paths, script, no_broker(paths)) == 1
        assert script.output == [LOGIN_RUNNING_STATUS]
    finally:
        login_lock.release()
    assert len(memory_keyring.items) == 3
    assert not paths.socket.exists()


async def test_a_locked_keychain_stops_logout_before_anything_changes(
    paths: FixedPaths, memory_keyring: MemoryKeyring
) -> None:
    save_fake_session()
    memory_keyring.locked = True
    script = ScriptedTerminal()
    assert await run_logout(paths, script, no_broker(paths)) == 2
    assert script.output == [
        "The macOS Keychain could not be used (locked); unlock it and run logout again."
    ]
    assert script.prompts == []


async def test_with_nothing_stored_logout_deletes_leftovers_and_asks_nothing(
    paths: FixedPaths, memory_keyring: MemoryKeyring
) -> None:
    memory_keyring.items[("telegram-mcp", "session")] = "half-written"
    script = ScriptedTerminal()
    assert await run_logout(paths, script, no_broker(paths)) == 0
    assert script.output == ["Not logged in; nothing to log out."]
    assert script.prompts == []
    assert memory_keyring.items == {}


# The question ------------------------------------------------------------------------------


@pytest.mark.parametrize("answer", ["y", "yes", "YES", " Yes "])
async def test_yes_in_any_case_logs_out_and_names_the_account(
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    brokers: list[Broker],
    memory_keyring: MemoryKeyring,
    answer: str,
) -> None:
    save_fake_session()
    script = ScriptedTerminal([answer])
    assert await run_logout(paths, script, in_process(paths, fake_gateway, brokers)) == 0
    assert script.prompts == [f"Log out of Telegram as {ACCOUNT}? [y/N] "]
    assert script.output == [
        f"Logged out of Telegram as {ACCOUNT}; the session was deleted from this Mac."
    ]
    assert memory_keyring.items == {}
    assert fake_gateway.call_names().count("log_out") == 1


@pytest.mark.parametrize(
    "answer",
    ["n", "", "maybe", "yess", EOFError(), KeyboardInterrupt()],
    ids=["n", "empty", "maybe", "yess", "end of input", "ctrl-c"],
)
async def test_anything_else_changes_nothing(
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    brokers: list[Broker],
    memory_keyring: MemoryKeyring,
    answer: str | BaseException,
) -> None:
    save_fake_session()
    script = ScriptedTerminal([answer])
    assert await run_logout(paths, script, in_process(paths, fake_gateway, brokers)) == 0
    assert script.output[-1] == "Nothing changed."
    assert len(memory_keyring.items) == 3
    assert "log_out" not in fake_gateway.call_names()


async def test_yes_flag_skips_the_question(
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    brokers: list[Broker],
    memory_keyring: MemoryKeyring,
) -> None:
    save_fake_session()
    script = ScriptedTerminal()
    client = in_process(paths, fake_gateway, brokers)
    assert await run_logout(paths, script, client, yes=True) == 0
    assert script.prompts == []
    assert "is_authorized" not in fake_gateway.call_names()
    assert memory_keyring.items == {}


async def test_a_session_ended_elsewhere_is_asked_about_and_said_so(
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    brokers: list[Broker],
    memory_keyring: MemoryKeyring,
) -> None:
    save_fake_session()
    fake_gateway.authorized = False
    script = ScriptedTerminal(["y"])
    assert await run_logout(paths, script, in_process(paths, fake_gateway, brokers)) == 0
    assert script.prompts == [f"Log out of Telegram as {UNREAD}? [y/N] "]
    assert script.output == [
        "The session had already been ended from another device; it was deleted from this Mac."
    ]
    assert memory_keyring.items == {}


# The outcomes ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (TelegramError("INTERNAL"), "a Telegram error"),
        (GatewayTimeout(), "Telegram did not answer in time"),
        (FloodWait(77), "Telegram asked to wait 77 s"),
    ],
)
async def test_telegram_not_confirming_still_logs_this_mac_out(
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    brokers: list[Broker],
    memory_keyring: MemoryKeyring,
    error: GatewayError,
    reason: str,
) -> None:
    save_fake_session()
    fake_gateway.script("log_out", error=error)
    script = ScriptedTerminal()
    client = in_process(paths, fake_gateway, brokers)
    assert await run_logout(paths, script, client, yes=True) == 0
    assert script.output == [NOT_CONFIRMED.format(reason=reason)]
    assert memory_keyring.items == {}


@pytest.mark.parametrize(
    ("error", "said"), [(None, "ended"), (TelegramError("INTERNAL"), "did not confirm ending")]
)
async def test_a_keychain_that_cannot_delete_exits_2_and_names_the_cause(
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    brokers: list[Broker],
    memory_keyring: MemoryKeyring,
    error: GatewayError | None,
    said: str,
) -> None:
    save_fake_session()
    if error is not None:
        fake_gateway.script("log_out", error=error)
    memory_keyring.fail_delete_items = {"api_hash"}
    script = ScriptedTerminal()
    client = in_process(paths, fake_gateway, brokers)
    assert await run_logout(paths, script, client, yes=True) == 2
    assert script.output == [
        f"Telegram {said} the session, but the macOS Keychain could not delete it "
        "(access denied); unlock the Keychain and run logout again."
    ]


@pytest.mark.parametrize(
    ("reply", "hint"),
    [("drop", ""), ("hang", " A macOS Keychain prompt may be open.")],
    ids=["dropped", "120 s passed"],
)
async def test_a_lost_logout_answer_is_unknown_and_never_resent(
    paths: FixedPaths, memory_keyring: MemoryKeyring, reply: str, hint: str
) -> None:
    save_fake_session()
    async with scripted_broker(paths, {"session.logout": [reply]}) as received:
        script = ScriptedTerminal()
        client = BrokerClient(paths, "cli", spawn=lambda: None, timeouts=FAST)
        assert await run_logout(paths, script, client, yes=True) == 1
    assert script.output == [UNKNOWN + hint]
    assert received.count("session.logout") == 1


async def test_logout_that_cannot_reach_a_broker_says_so(
    paths: FixedPaths, memory_keyring: MemoryKeyring
) -> None:
    save_fake_session()
    script = ScriptedTerminal()
    client = BrokerClient(paths, "cli", spawn=lambda: None, timeouts=FAST)
    assert await run_logout(paths, script, client, yes=True) == 1
    assert script.output[0].startswith("The background connection could not be started (")
    assert len(memory_keyring.items) == 3


# Status ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prepare", "line", "code"),
    [
        (None, f"Logged in to Telegram as {ACCOUNT}.", 0),
        ("nothing", "Not logged in. Run `telegram-mcp login` in your terminal.", 2),
        (
            "ended",
            "Not logged in: the session was ended from another device. Run `telegram-mcp "
            "login` in your terminal.",
            2,
        ),
        ("locked", "The macOS Keychain could not be used (locked); unlock it.", 2),
        ("unreachable", "A session is stored, but Telegram could not be reached to check it.", 1),
    ],
)
async def test_status_prints_three_lines_and_exits_by_state(
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    brokers: list[Broker],
    memory_keyring: MemoryKeyring,
    prepare: str | None,
    line: str,
    code: int,
) -> None:
    if prepare != "nothing":
        save_fake_session()
    if prepare == "ended":
        fake_gateway.authorized = False
    if prepare == "unreachable":
        fake_gateway.script("is_authorized", error=TelegramError("INTERNAL"))
    client = in_process(paths, fake_gateway, brokers)
    if prepare == "locked":
        await client.status()  # the broker starts and reads the vault before the lock
        memory_keyring.locked = True
    script = ScriptedTerminal()
    assert await run_status(paths, script, client) == code
    assert script.output[0] == line
    assert len(script.output) == 3
    assert script.output[2] == "Claude sessions using it: 0."
    assert log_lines(paths) == []


def log_lines(paths: FixedPaths) -> list[str]:
    return paths.log.read_text().splitlines() if paths.log.exists() else []


async def test_status_tells_a_started_broker_from_a_running_one_and_counts_sessions(
    paths: FixedPaths, fake_gateway: FakeGateway, brokers: list[Broker]
) -> None:
    save_fake_session()
    script = ScriptedTerminal()
    assert await run_status(paths, script, in_process(paths, fake_gateway, brokers)) == 0
    pid = os.getpid()  # the in-process broker runs in this process
    assert script.output[1] == f"Background connection: started now (pid {pid})."

    sessions = [await RawClient.connect(paths) for _ in range(2)]
    hook = await RawClient.connect(paths, client="approval-hook", client_name=None)
    script = ScriptedTerminal()
    assert await run_status(paths, script, no_broker(paths)) == 0
    assert script.output[1:] == [
        f"Background connection: running (pid {pid}).",
        "Claude sessions using it: 2.",
    ]
    for client in [*sessions, hook]:
        await client.close()


async def test_status_retries_once_when_the_connection_drops(paths: FixedPaths) -> None:
    async with scripted_broker(paths, {"broker.status": ["drop", status_reply()]}) as received:
        script = ScriptedTerminal()
        client = BrokerClient(paths, "cli", spawn=lambda: None, timeouts=FAST)
        assert await run_status(paths, script, client) == 0
    assert received.count("broker.status") == 2
    assert script.output[0] == f"Logged in to Telegram as {ACCOUNT}."


async def test_status_drops_twice_is_a_failure(paths: FixedPaths) -> None:
    async with scripted_broker(paths, {"broker.status": ["drop", "drop"]}) as received:
        script = ScriptedTerminal()
        client = BrokerClient(paths, "cli", spawn=lambda: None, timeouts=FAST)
        assert await run_status(paths, script, client) == 1
    assert received.count("broker.status") == 2
    assert script.output[0].startswith("The background connection could not be started (")


async def test_status_waits_a_bounded_time_for_the_answer(paths: FixedPaths) -> None:
    async with scripted_broker(paths, {"broker.status": ["hang"]}):
        script = ScriptedTerminal()
        client = BrokerClient(paths, "cli", spawn=lambda: None, timeouts=FAST)
        assert await run_status(paths, script, client) == 1
    assert script.output == ["The background connection did not answer in time."]


async def test_status_that_cannot_start_a_broker_says_so(paths: FixedPaths) -> None:
    script = ScriptedTerminal()
    client = BrokerClient(paths, "cli", spawn=lambda: None, timeouts=FAST)
    assert await run_status(paths, script, client) == 1
    assert script.output[0].startswith("The background connection could not be started (")


async def test_logout_asks_status_with_its_own_bounded_wait(paths: FixedPaths) -> None:
    save_fake_session()
    async with scripted_broker(paths, {"broker.status": ["hang"]}) as received:
        script = ScriptedTerminal(["y"])
        client = BrokerClient(paths, "cli", spawn=lambda: None, timeouts=FAST)
        assert await run_logout(paths, script, client) == 1
    assert script.output == ["The background connection did not answer in time."]
    assert "session.logout" not in received


def test_the_command_waits_match_the_design() -> None:
    assert (Timeouts().status, Timeouts().logout) == (20.0, 120.0)
