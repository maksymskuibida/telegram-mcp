"""BrokerClient: finding or starting the broker, the single retry, the time limits
(BR2.2, BR2.4, BR2.5, NFR11.7, NFR11.9, the ten-callers case of FR2.3)."""

from __future__ import annotations

import asyncio
import os
import statistics
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from support.brokers import make_broker, running_broker, save_fake_session, subprocess_brokers
from support.rpc import RawClient
from support.samples import ALICE
from telegram_mcp.broker import jsonrpc
from telegram_mcp.broker.client import (
    BrokerClient,
    BrokerUnavailable,
    RequestLost,
    Timeouts,
    spawn_broker,
)
from telegram_mcp.broker.lock import BrokerLock, LockState
from telegram_mcp.broker.server import Broker
from telegram_mcp.gateway.fake import FakeGateway
from telegram_mcp.paths import FixedPaths

__all__ = ["subprocess_brokers"]

FAST = Timeouts(hello=1, answer=1, check=1, call=0.3, start=0.5)
SEND = "tg_messages_send"


@pytest.fixture
def paths(home: Path) -> FixedPaths:
    return FixedPaths.from_home(home)


def never_spawn() -> None:
    pytest.fail("the client must not start a broker here")


async def test_a_free_lock_makes_the_client_start_a_broker(
    paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    save_fake_session()
    started: list[Broker] = []

    def spawn() -> None:
        broker = make_broker(paths, fake_gateway)
        started.append(broker)
        asyncio.get_running_loop().create_task(broker.start())

    client = BrokerClient(paths, "mcp-server", client_name="claude-code", spawn=spawn)
    try:
        result = await client.tool_call("tg_chats_list", {})
        assert result["ok"] is True
        assert len(started) == 1
        assert client.broker_pid == os.getpid()
    finally:
        await client.close()
        for broker in started:
            await broker.stop()


async def test_a_running_lock_makes_the_client_wait_for_the_socket(
    paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    paths.data_dir.mkdir(parents=True, mode=0o700)
    starting = BrokerLock(paths.lock)
    assert starting.try_hold(LockState.RUNNING)
    broker = make_broker(paths, fake_gateway)

    async def finish_starting() -> None:
        await asyncio.sleep(0.2)
        starting.release()
        await broker.start()

    task = asyncio.create_task(finish_starting())
    client = BrokerClient(paths, "approval-hook", spawn=never_spawn)
    try:
        assert await client.approval_check("tg_chats_list", {}) == {
            "decision": "refuse",
            "code": "no_session",
            "message": "Not logged in: run `telegram-mcp login` in a terminal.",
        }
    finally:
        await task
        await client.close()
        await broker.stop()


async def test_a_login_in_progress_is_reported_and_nothing_starts(paths: FixedPaths) -> None:
    paths.data_dir.mkdir(parents=True, mode=0o700)
    login = BrokerLock(paths.lock)
    assert login.try_hold(LockState.LOGIN)
    client = BrokerClient(paths, "mcp-server", client_name="claude-code", spawn=never_spawn)
    try:
        result = await client.tool_call(SEND, {"chat": 1, "text": "x"})
        assert result["error"]["code"] == "login_in_progress"
        with pytest.raises(BrokerUnavailable) as refused:
            await client.approval_check(SEND, {})
        assert refused.value.code == "login_in_progress"
    finally:
        login.release()


async def test_the_client_starts_a_broker_once_then_gives_up(paths: FixedPaths) -> None:
    spawned: list[int] = []
    client = BrokerClient(paths, "approval-hook", spawn=lambda: spawned.append(1), timeouts=FAST)
    started = time.monotonic()
    with pytest.raises(BrokerUnavailable, match="did not start"):
        await client.approval_check(SEND, {})
    assert spawned == [1]
    assert time.monotonic() - started < 2


async def fake_broker(
    paths: FixedPaths, on_request: Callable[[str], bytes | None]
) -> tuple[asyncio.Server, list[str]]:
    """A stand-in broker: accepts hello, then answers each request with ``on_request``."""
    paths.data_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    paths.secret.write_text("s" * 64)
    seen: list[str] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while line := await reader.readline():
            request = jsonrpc.parse_request(line)
            seen.append(request.method)
            if request.method == "hello":
                writer.write(jsonrpc.result_message(request.id, {"pid": 1}))
                continue
            reply = on_request(request.method)
            if reply is None:
                writer.close()
                return
            writer.write(reply)

    server = await asyncio.start_unix_server(serve, path=str(paths.socket))
    return server, seen


@pytest.mark.parametrize(
    ("tool", "code"), [(SEND, "outcome_unknown"), ("tg_chats_list", "telegram_error")]
)
async def test_a_request_lost_after_writing_is_never_retried(
    paths: FixedPaths, tool: str, code: str
) -> None:
    server, seen = await fake_broker(paths, lambda method: None)
    client = BrokerClient(paths, "mcp-server", client_name="claude-code", spawn=never_spawn)
    try:
        result = await client.tool_call(tool, {"chat": 1, "text": "x"})
        assert result["error"]["code"] == code
        assert seen.count("tool.call") == 1
    finally:
        server.close()
        await client.close()


@pytest.mark.parametrize(
    ("tool", "code"), [(SEND, "outcome_unknown"), ("tg_chats_list", "telegram_timeout")]
)
async def test_callers_bound_their_waits(paths: FixedPaths, tool: str, code: str) -> None:
    server, _ = await fake_broker(paths, lambda method: b"")
    client = BrokerClient(
        paths, "mcp-server", client_name="claude-code", spawn=never_spawn, timeouts=FAST
    )
    try:
        started = time.monotonic()
        result = await client.tool_call(tool, {"chat": 1, "text": "x"})
        assert result["error"]["code"] == code
        assert time.monotonic() - started < 1
        with pytest.raises(RequestLost):
            await client.approval_check(tool, {})
    finally:
        server.close()
        await client.close()


def test_the_default_time_limits_match_the_contract() -> None:
    assert Timeouts() == Timeouts(hello=5, answer=5, check=12, call=75, start=5)


async def test_a_kept_connection_reconnects_after_the_broker_restarts(
    paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    save_fake_session()
    client = BrokerClient(paths, "mcp-server", client_name="claude-code", spawn=never_spawn)
    try:
        async with running_broker(paths, fake_gateway):
            assert (await client.tool_call("tg_chats_list", {}))["ok"] is True
        async with running_broker(paths, fake_gateway):
            assert (await client.tool_call("tg_chats_list", {}))["ok"] is True
    finally:
        await client.close()


async def test_a_pick_is_recorded_while_a_send_waits_on_telegram(
    paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    """NFR11.9: approval.answer on one connection completes while tool.call waits."""
    save_fake_session()
    async with running_broker(paths, fake_gateway):
        hook = await RawClient.connect(paths, client="approval-hook", client_name=None)

        async def confirm(text: str) -> dict[str, Any]:
            args = {"chat": ALICE.id, "text": text}
            asked = await hook.result("approval.check", {"tool": SEND, "args": args})
            answered = await hook.result(
                "approval.answer",
                {
                    "question": asked["question"],
                    "options": ["Allow", "Deny"],
                    "option_descriptions": ["Send it", "Do not send"],
                    "multi_select": False,
                    "pick": "Allow",
                },
            )
            assert answered == {"outcome": "confirmed"}
            return args

        first = await confirm("first")
        fake_gateway.script("send_text", hang=True)
        server = await RawClient.connect(paths)
        waiting = asyncio.create_task(server.call("tool.call", {"tool": SEND, "args": first}))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        await confirm("second")
        assert time.monotonic() - started < 1
        assert not waiting.done()
        waiting.cancel()


async def test_per_call_overhead_over_one_kept_connection(
    paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    """NFR11.7: reported, not gated (the bound here only catches a broken connection reuse)."""
    save_fake_session()
    async with running_broker(paths, fake_gateway):
        client = BrokerClient(paths, "mcp-server", client_name="claude-code", spawn=never_spawn)
        await client.tool_call("tg_chats_list", {})
        timings = []
        for _ in range(30):
            started = time.perf_counter()
            await client.tool_call("tg_chats_list", {"limit": 1})
            timings.append(time.perf_counter() - started)
        await client.close()
    median_ms = statistics.median(timings) * 1000
    print(f"NFR11.7 per-call overhead: median {median_ms:.2f} ms over 30 calls")
    assert median_ms < 50


@pytest.mark.usefixtures("subprocess_brokers")
async def test_ten_callers_at_once_yield_one_broker(paths: FixedPaths) -> None:
    clients = [BrokerClient(paths, "cli", spawn=spawn_broker) for _ in range(10)]
    try:
        results = await asyncio.gather(
            *(client.request("broker.status", {}, 5) for client in clients)
        )
        assert all(result["clients"] >= 1 for result in results)
        pids = {client.broker_pid for client in clients}
        assert len(pids) == 1
        assert pids != {os.getpid()}
    finally:
        for client in clients:
            await client.close()
