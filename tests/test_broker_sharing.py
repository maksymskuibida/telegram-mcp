"""Two Claude Code sessions sharing one broker and one Telegram connection: reads beside
writes, the read places, several clients at once, a whole and ordered log, and the timers'
cost on the event loop."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest

from support.brokers import FakeClock, broker_starter, save_fake_session, wait_until
from support.rpc import RawClient
from support.samples import ALICE
from support.writes import DESCRIPTIONS, SEND, check, confirm, hook, list_chats, send
from telegram_mcp.broker.client import BrokerClient
from telegram_mcp.broker.server import Broker
from telegram_mcp.gateway.fake import FakeGateway
from telegram_mcp.paths import FixedPaths


@pytest.fixture
def paths(home: Path) -> FixedPaths:
    return FixedPaths.from_home(home)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def start_broker(
    paths: FixedPaths, fake_gateway: FakeGateway, clock: FakeClock
) -> AsyncIterator[Callable[..., Awaitable[Broker]]]:
    save_fake_session()
    async with broker_starter(paths, fake_gateway, clock) as start:
        yield start


def never_spawn() -> None:
    pytest.fail("the broker is already running; the client must not start one")


def session(paths: FixedPaths) -> BrokerClient:
    return BrokerClient(paths, "mcp-server", client_name="claude-code", spawn=never_spawn)


async def hanging_send(paths: FixedPaths, fake: FakeGateway, text: str) -> asyncio.Task[object]:
    await confirm(paths, text)
    fake.script("send_text", hang=True)
    sender = await RawClient.connect(paths)
    task: asyncio.Task[object] = asyncio.create_task(send(sender, text, 30))
    await wait_until(lambda: "send_text" in fake.call_names())
    return task


# Part 1: reads and checks never wait for a write ---------------------------------------------


async def test_a_read_and_a_check_finish_on_time_while_a_send_hangs(
    start_broker: Callable[..., Awaitable[Broker]], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    await start_broker()
    sending = await hanging_send(paths, fake_gateway, "hanging")
    reader, approval_hook = await RawClient.connect(paths), await hook(paths)
    started = time.monotonic()
    assert (await list_chats(reader))["ok"] is True
    assert (await check(approval_hook, "another message"))["decision"] == "ask"
    assert time.monotonic() - started < 0.5
    assert not sending.done()
    sending.cancel()


async def test_an_answer_and_status_are_not_held_by_a_hanging_send(
    start_broker: Callable[..., Awaitable[Broker]], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    await start_broker()
    sending = await hanging_send(paths, fake_gateway, "hanging")
    approval_hook = await hook(paths)
    asked = await check(approval_hook, "second")
    started = time.monotonic()
    answered = await approval_hook.result(
        "approval.answer",
        {
            "question": asked["question"],
            "options": ["Allow", "Deny"],
            "option_descriptions": DESCRIPTIONS,
            "multi_select": False,
            "pick": "Allow",
        },
    )
    status = await (await RawClient.connect(paths, client="cli")).result("broker.status")
    assert answered == {"outcome": "confirmed"}
    assert status["logged_in"] is True
    assert time.monotonic() - started < 0.5
    sending.cancel()


async def test_two_reads_run_side_by_side(
    start_broker: Callable[..., Awaitable[Broker]], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    await start_broker()
    first, second = await RawClient.connect(paths), await RawClient.connect(paths)
    await list_chats(first)  # connect first, so only the reads are timed
    fake_gateway.script("list_chats", delay=1, times=2)
    started = time.monotonic()
    results = await asyncio.gather(list_chats(first), list_chats(second))
    assert all(result["ok"] for result in results)
    assert time.monotonic() - started < 1.5


async def test_at_most_ten_reads_reach_telegram_and_the_wait_counts_in_their_limit(
    start_broker: Callable[..., Awaitable[Broker]], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    broker = await start_broker(call_limit=1.0)
    readers = [await RawClient.connect(paths) for _ in range(12)]
    await list_chats(readers[0])
    fake_gateway.script("list_chats", hang=True, times=12)
    tasks = [asyncio.create_task(list_chats(reader)) for reader in readers]
    started = time.monotonic()
    await wait_until(lambda: broker.reads.in_use == 10)
    await asyncio.sleep(0.1)
    assert fake_gateway.call_names().count("list_chats") == 1 + 10
    results = await asyncio.gather(*tasks)
    assert [result["error"]["code"] for result in results] == ["telegram_timeout"] * 12
    # The two that waited for a place were answered inside the same 1 s limit, not after
    # a second limit of their own.
    assert time.monotonic() - started < 1.5


# Part 2: two sessions end to end -------------------------------------------------------------


async def test_two_sessions_list_chats_at_once_over_one_connect(
    start_broker: Callable[..., Awaitable[Broker]], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    await start_broker()
    terminal, desktop = session(paths), session(paths)
    try:
        results = await asyncio.gather(
            terminal.tool_call("tg_chats_list", {}), desktop.tool_call("tg_chats_list", {})
        )
        assert all(result["ok"] for result in results)
        assert fake_gateway.call_names().count("connect") == 1
        assert terminal.broker_pid == desktop.broker_pid
    finally:
        await terminal.close()
        await desktop.close()


async def test_one_session_sends_while_the_other_reads(
    start_broker: Callable[..., Awaitable[Broker]], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    await start_broker()
    await confirm(paths, "from the terminal")
    terminal, desktop = session(paths), session(paths)
    fake_gateway.script("send_text", delay=0.5)
    try:
        sending = asyncio.create_task(
            terminal.tool_call(SEND, {"chat": ALICE.id, "text": "from the terminal"})
        )
        await wait_until(lambda: "send_text" in fake_gateway.call_names())
        assert (await desktop.tool_call("tg_chats_list", {}))["ok"] is True
        assert not sending.done()
        assert (await sending)["ok"] is True
        assert [sent.text for sent in fake_gateway.sent] == ["from the terminal"]
    finally:
        await terminal.close()
        await desktop.close()


async def test_eight_clients_with_mixed_calls_are_each_served(
    start_broker: Callable[..., Awaitable[Broker]], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    broker = await start_broker()
    sessions = [await RawClient.connect(paths) for _ in range(5)]
    hooks = [await hook(paths) for _ in range(2)]
    status_client = await RawClient.connect(paths, client="cli")
    await confirm(paths, "mixed send")
    calls = [
        list_chats(sessions[0]),
        list_chats(sessions[1]),
        list_chats(sessions[2]),
        send(sessions[3], "mixed send"),
        list_chats(sessions[4]),
        check(hooks[0], "mixed one"),
        check(hooks[1], "mixed two"),
        status_client.result("broker.status"),
    ]
    results = await asyncio.gather(*calls)
    assert all(result["ok"] for result in results[:5])
    assert [result["decision"] for result in results[5:7]] == ["ask", "ask"]
    assert results[7]["clients"] == 5
    assert broker.session_count() == 5
    assert fake_gateway.call_names().count("connect") == 1


async def test_log_entries_stay_whole_and_in_order_with_two_sessions(
    start_broker: Callable[..., Awaitable[Broker]], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    await start_broker()
    terminal, desktop = await RawClient.connect(paths), await RawClient.connect(paths)
    for text in ("a", "b", "c"):
        await confirm(paths, text)
    calls = []
    for text in ("a", "b", "c"):
        calls += [list_chats(terminal), send(desktop, text)]
    for call in calls:
        assert (await call)["ok"] is True
    lines = paths.log.read_text(encoding="utf-8").splitlines()
    entries = [json.loads(line) for line in lines]
    assert len(entries) == len(lines)
    stamps = [entry["ts"] for entry in entries]
    assert stamps == sorted(stamps)
    assert sum(1 for entry in entries if entry["event"] == "result") == 6


async def test_the_timers_never_hold_the_event_loop_for_50_ms(
    start_broker: Callable[..., Awaitable[Broker]], paths: FixedPaths, clock: FakeClock
) -> None:
    broker = await start_broker()
    for text in ("x", "y"):
        await check(await hook(paths), text)
    clock.advance(3600)
    for step in (broker.sweep_expired, broker.rotate_secret_if_due, broker.writes.drop_overdue):
        started = time.perf_counter()
        step()
        assert time.perf_counter() - started < 0.05
    started = time.perf_counter()
    broker.stop_if_idle()  # schedules the stop; does not run it on the timer's turn
    assert time.perf_counter() - started < 0.05
    await asyncio.wait_for(broker.wait_stopped(), 3)
