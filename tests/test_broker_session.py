"""The broker's logout and status, written before the code (team Testing Posture: these
decide whether the session is ended and the Keychain cleared).

Over a real Unix socket with the fake gateway and the in-memory keyring."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from support.brokers import FakeClock
from support.memory_keyring import MemoryKeyring
from support.rpc import RawClient
from support.samples import ALICE, FAMILY, fake_secrets
from telegram_mcp.broker.server import (
    LOGOUT_BUDGET_SECONDS,
    STATUS_BUDGET_SECONDS,
    Broker,
)
from telegram_mcp.gateway.fake import FakeGateway
from telegram_mcp.gateway.models import (
    FloodWait,
    GatewayError,
    GatewayTimeout,
    NotAuthorized,
    TelegramError,
)
from telegram_mcp.paths import FixedPaths
from telegram_mcp.vault import SessionVault, StoredSession

SEND = "tg_messages_send"
DESCRIPTIONS = ["Send it", "Do not send"]
ACCOUNT = "Test Person (@tester, id 4242)"


@pytest.fixture
def paths(home: Path) -> FixedPaths:
    return FixedPaths.from_home(home)


@pytest.fixture
def logged_in() -> StoredSession:
    values = fake_secrets()
    stored = StoredSession(values.session, values.api_id, values.api_hash)
    SessionVault().save(stored)
    return stored


@pytest.fixture
async def start_broker(
    paths: FixedPaths, fake_gateway: FakeGateway
) -> AsyncIterator[Callable[..., Any]]:
    started: list[Broker] = []

    async def start(**options: Any) -> Broker:
        broker = Broker(
            paths,
            gateway_factory=lambda session, api_id, api_hash: fake_gateway,
            clock=FakeClock(),
            **options,
        )
        await broker.start()
        started.append(broker)
        return broker

    yield start
    for broker in started:
        await broker.stop()


async def cli(paths: FixedPaths) -> RawClient:
    return await RawClient.connect(paths, client="cli", client_name=None)


async def hook(paths: FixedPaths) -> RawClient:
    return await RawClient.connect(paths, client="approval-hook", client_name=None)


async def ask(client: RawClient, args: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = await client.result("approval.check", {"tool": SEND, "args": args})
    return result


async def relay(client: RawClient, question: str, pick: str = "Allow") -> str:
    result = await client.result(
        "approval.answer",
        {
            "question": question,
            "options": ["Allow", "Deny"],
            "option_descriptions": DESCRIPTIONS,
            "multi_select": False,
            "pick": pick,
        },
    )
    return str(result["outcome"])


async def confirm(paths: FixedPaths, args: dict[str, Any]) -> None:
    approval_hook = await hook(paths)
    asked = await ask(approval_hook, args)
    assert asked["decision"] == "ask"
    assert await relay(approval_hook, asked["question"]) == "confirmed"
    await approval_hook.close()


def log_entries(paths: FixedPaths) -> list[dict[str, Any]]:
    if not paths.log.exists():
        return []
    return [json.loads(line) for line in paths.log.read_text().splitlines()]


async def wait_for_call(gateway: FakeGateway, name: str) -> None:
    for _ in range(200):
        if name in gateway.call_names():
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"the broker never called {name}")


# session.logout ------------------------------------------------------------------------------


@pytest.mark.usefixtures("logged_in")
async def test_logout_ends_the_session_clears_the_keychain_logs_once_and_stops(
    start_broker: Callable[..., Any],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    memory_keyring: MemoryKeyring,
) -> None:
    broker = await start_broker()
    client = await cli(paths)
    result = await client.result("session.logout")
    assert result == {
        "ok": True,
        "account": ACCOUNT,
        "telegram_confirmed": True,
        "telegram_reason": None,
        "retry_after": None,
        "keychain_cleared": True,
        "keychain_cause": None,
    }
    assert fake_gateway.call_names().count("log_out") == 1
    assert memory_keyring.items == {}
    await asyncio.wait_for(broker.wait_stopped(), 2)
    assert not paths.socket.exists()
    assert not paths.secret.exists()
    entries = log_entries(paths)
    assert len(entries) == 1
    entry = entries[0]
    assert {k: v for k, v in entry.items() if k != "ts"} == {
        "client": "cli",
        "event": "result",
        "tool": "logout",
        "chat_id": None,
        "approval_id": None,
        "content": ACCOUNT,
        "decision": None,
        "result": "ok",
    }


@pytest.mark.parametrize(
    ("error", "confirmed_by_telegram", "reason", "retry_after"),
    [
        (None, False, "telegram_error", None),
        (TelegramError("INTERNAL"), True, "telegram_error", None),
        (GatewayTimeout(), True, "telegram_timeout", None),
        (FloodWait(77), True, "flood_wait", 77),
        (NotAuthorized(), True, "no_session", None),
    ],
    ids=["telegram said no", "rpc error", "timeout", "flood wait", "already ended"],
)
@pytest.mark.usefixtures("logged_in")
async def test_every_telegram_outcome_still_clears_the_keychain(
    start_broker: Callable[..., Any],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    memory_keyring: MemoryKeyring,
    error: GatewayError | None,
    confirmed_by_telegram: bool,
    reason: str,
    retry_after: int | None,
) -> None:
    fake_gateway.log_out_result = confirmed_by_telegram
    if error is not None:
        fake_gateway.script("log_out", error=error)
    broker = await start_broker()
    result = await (await cli(paths)).result("session.logout")
    assert result["telegram_confirmed"] is False
    assert result["telegram_reason"] == reason
    assert result["retry_after"] == retry_after
    assert result["keychain_cleared"] is True
    assert result["ok"] is True
    assert memory_keyring.items == {}
    await asyncio.wait_for(broker.wait_stopped(), 2)
    assert [e["result"] for e in log_entries(paths) if e["tool"] == "logout"] == [reason]


@pytest.mark.usefixtures("logged_in")
async def test_a_log_out_call_that_hangs_is_a_timeout_after_its_budget(
    start_broker: Callable[..., Any],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    memory_keyring: MemoryKeyring,
) -> None:
    fake_gateway.script("log_out", hang=True)
    await start_broker(logout_budget=0.1)
    result = await (await cli(paths)).result("session.logout")
    assert (result["telegram_confirmed"], result["telegram_reason"]) == (False, "telegram_timeout")
    assert memory_keyring.items == {}


@pytest.mark.usefixtures("logged_in")
async def test_a_session_ended_elsewhere_is_not_logged_out_again(
    start_broker: Callable[..., Any],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    memory_keyring: MemoryKeyring,
) -> None:
    fake_gateway.authorized = False
    await start_broker()
    result = await (await cli(paths)).result("session.logout")
    assert result["account"] is None
    assert (result["telegram_confirmed"], result["telegram_reason"]) == (False, "no_session")
    assert "log_out" not in fake_gateway.call_names()
    assert memory_keyring.items == {}
    assert [e["content"] for e in log_entries(paths)] == [None]


async def test_with_nothing_stored_logout_clears_leftovers_without_calling_telegram(
    start_broker: Callable[..., Any],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    memory_keyring: MemoryKeyring,
) -> None:
    memory_keyring.items[("telegram-mcp", "api_id")] = "12345"
    await start_broker()
    result = await (await cli(paths)).result("session.logout")
    assert result["telegram_reason"] == "no_session"
    assert result["keychain_cleared"] is True
    assert fake_gateway.call_names() == []
    assert memory_keyring.items == {}


@pytest.mark.usefixtures("logged_in")
async def test_a_keychain_that_cannot_delete_is_reported_and_logged_as_locked(
    start_broker: Callable[..., Any],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    memory_keyring: MemoryKeyring,
) -> None:
    memory_keyring.fail_delete_items = {"session"}
    broker = await start_broker()
    result = await (await cli(paths)).result("session.logout")
    assert result["telegram_confirmed"] is True
    assert result["keychain_cleared"] is False
    assert result["keychain_cause"] == "access denied"
    assert result["ok"] is False
    await asyncio.wait_for(broker.wait_stopped(), 2)
    assert [e["result"] for e in log_entries(paths)] == ["keychain_locked"]


@pytest.mark.usefixtures("logged_in")
async def test_telegram_and_the_keychain_failing_together_are_both_named(
    start_broker: Callable[..., Any],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    memory_keyring: MemoryKeyring,
    logged_in: StoredSession,
) -> None:
    fake_gateway.script("log_out", error=TelegramError("INTERNAL"))
    memory_keyring.fail_deletes = True
    broker = await start_broker()
    result = await (await cli(paths)).result("session.logout")
    assert (result["telegram_reason"], result["keychain_cleared"]) == ("telegram_error", False)
    await asyncio.wait_for(broker.wait_stopped(), 2)
    assert [e["result"] for e in log_entries(paths)] == ["keychain_locked"]
    diagnostics = paths.diagnostics.read_text()
    assert "logout" in diagnostics
    assert "TelegramError" in diagnostics
    assert "access denied" in diagnostics
    assert logged_in.session not in diagnostics


@pytest.mark.usefixtures("logged_in")
async def test_from_the_moment_logout_arrives_every_call_is_refused(
    start_broker: Callable[..., Any], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    fake_gateway.script("log_out", hang=True)
    await start_broker(logout_budget=1.0)
    server = await RawClient.connect(paths)
    approval_hook = await hook(paths)
    pending = await ask(approval_hook, {"chat": ALICE.id, "text": "asked before"})
    logout = asyncio.create_task((await cli(paths)).result("session.logout"))
    await wait_for_call(fake_gateway, "log_out")
    calls_before = len(fake_gateway.calls)

    listed = await server.result("tool.call", {"tool": "tg_chats_list", "args": {}})
    assert listed["ok"] is False
    assert listed["error"]["code"] == "no_session"
    checked = await ask(approval_hook, {"chat": FAMILY.id, "text": "asked during"})
    assert (checked["decision"], checked["code"]) == ("refuse", "no_session")
    assert await relay(approval_hook, pending["question"]) == "unknown"
    assert len(fake_gateway.calls) == calls_before

    result = await asyncio.wait_for(logout, 5)
    assert result["telegram_reason"] == "telegram_timeout"
    assert result["keychain_cleared"] is True


@pytest.mark.usefixtures("logged_in")
async def test_a_send_already_running_finishes_once_before_logout_starts(
    start_broker: Callable[..., Any], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    await start_broker()
    args = {"chat": ALICE.id, "text": "in flight"}
    await confirm(paths, args)
    release = asyncio.Event()
    entered = asyncio.Event()
    original = fake_gateway.send_text

    async def slow_send(chat_id: int, text: str, reply_to: int | None, topic_id: int | None) -> Any:
        entered.set()
        await release.wait()
        return await original(chat_id, text, reply_to, topic_id)

    fake_gateway.send_text = slow_send  # type: ignore[method-assign]
    server = await RawClient.connect(paths)
    send = asyncio.create_task(server.result("tool.call", {"tool": SEND, "args": args}))
    await asyncio.wait_for(entered.wait(), 2)
    logout = asyncio.create_task((await cli(paths)).result("session.logout"))
    await asyncio.sleep(0.1)
    assert "log_out" not in fake_gateway.call_names()
    assert not logout.done()

    release.set()
    sent = await asyncio.wait_for(send, 2)
    result = await asyncio.wait_for(logout, 2)
    assert sent["ok"] is True
    assert [s.text for s in fake_gateway.sent] == ["in flight"]
    assert result["telegram_confirmed"] is True
    tools = [(e["tool"], e["result"]) for e in log_entries(paths) if e["event"] == "result"]
    assert tools[-2:] == [(SEND, "ok"), ("logout", "ok")]


@pytest.mark.usefixtures("logged_in")
async def test_open_approvals_are_dropped_and_logged_as_expired(
    start_broker: Callable[..., Any], paths: FixedPaths
) -> None:
    await start_broker()
    await confirm(paths, {"chat": ALICE.id, "text": "confirmed but unused"})
    approval_hook = await hook(paths)
    pending = await ask(approval_hook, {"chat": FAMILY.id, "text": "never answered"})
    await (await cli(paths)).result("session.logout")
    entries = log_entries(paths)
    expired = [e for e in entries if e["result"] == "timed_out"]
    assert sorted((e["event"], e["decision"]) for e in expired) == [
        ("decision", "timed_out"),
        ("result", None),
    ]
    assert pending["approval_id"] in {e["approval_id"] for e in expired}
    assert entries[-1]["tool"] == "logout"


@pytest.mark.usefixtures("logged_in")
async def test_a_stop_during_logout_waits_until_logout_has_answered(
    start_broker: Callable[..., Any],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    memory_keyring: MemoryKeyring,
) -> None:
    fake_gateway.script("log_out", hang=True)
    broker = await start_broker(logout_budget=0.5)
    logout = asyncio.create_task((await cli(paths)).result("session.logout"))
    await wait_for_call(fake_gateway, "log_out")
    stopper = await cli(paths)
    stop = asyncio.create_task(
        stopper.send_line(b'{"jsonrpc":"2.0","id":9,"method":"broker.stop","params":{}}\n')
    )
    await asyncio.sleep(0.2)
    assert not stop.done()
    assert len(memory_keyring.items) == 3

    result = await asyncio.wait_for(logout, 5)
    assert result["keychain_cleared"] is True
    assert memory_keyring.items == {}
    reply = await asyncio.wait_for(stop, 5)
    assert reply is None or reply["result"] == {"ok": True}
    await asyncio.wait_for(broker.wait_stopped(), 2)


@pytest.mark.usefixtures("logged_in")
async def test_a_second_logout_during_one_waits_and_gets_the_same_answer(
    start_broker: Callable[..., Any],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    memory_keyring: MemoryKeyring,
) -> None:
    fake_gateway.script("log_out", hang=True)
    broker = await start_broker(logout_budget=0.5)
    first = asyncio.create_task((await cli(paths)).result("session.logout"))
    await wait_for_call(fake_gateway, "log_out")
    second = asyncio.create_task((await cli(paths)).result("session.logout"))
    await asyncio.sleep(0.1)
    assert not second.done()

    answer = await asyncio.wait_for(first, 5)
    assert await asyncio.wait_for(second, 5) == answer
    assert fake_gateway.call_names().count("log_out") == 1
    assert [e["tool"] for e in log_entries(paths)].count("logout") == 1
    assert memory_keyring.items == {}
    await asyncio.wait_for(broker.wait_stopped(), 2)


@pytest.mark.usefixtures("logged_in")
async def test_a_failed_account_lookup_still_logs_out(
    start_broker: Callable[..., Any],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    memory_keyring: MemoryKeyring,
) -> None:
    fake_gateway.script("me", error=TelegramError("INTERNAL"))
    await start_broker()
    result = await (await cli(paths)).result("session.logout")
    assert result["account"] is None
    assert result["telegram_confirmed"] is True
    assert memory_keyring.items == {}
    assert "account lookup failed: TelegramError" in paths.diagnostics.read_text()


@pytest.mark.usefixtures("logged_in")
async def test_a_keychain_locked_at_logout_calls_no_telegram_and_clears_nothing(
    start_broker: Callable[..., Any],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    memory_keyring: MemoryKeyring,
) -> None:
    await start_broker()
    memory_keyring.locked = True
    result = await (await cli(paths)).result("session.logout")
    assert (result["telegram_confirmed"], result["telegram_reason"]) == (False, "telegram_error")
    assert (result["keychain_cleared"], result["keychain_cause"]) == (False, "locked")
    assert "log_out" not in fake_gateway.call_names()
    assert [e["result"] for e in log_entries(paths)] == ["keychain_locked"]


@pytest.mark.usefixtures("logged_in")
async def test_a_send_still_queued_when_logout_arrives_is_refused_not_sent(
    start_broker: Callable[..., Any], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    await start_broker()
    first = {"chat": ALICE.id, "text": "running"}
    queued = {"chat": FAMILY.id, "text": "queued"}
    await confirm(paths, first)
    await confirm(paths, queued)
    release = asyncio.Event()
    entered = asyncio.Event()
    original = fake_gateway.send_text

    async def slow_send(chat_id: int, text: str, reply_to: int | None, topic_id: int | None) -> Any:
        if text == "running":
            entered.set()
            await release.wait()
        return await original(chat_id, text, reply_to, topic_id)

    fake_gateway.send_text = slow_send  # type: ignore[method-assign]
    servers = [await RawClient.connect(paths) for _ in range(2)]
    running = asyncio.create_task(servers[0].result("tool.call", {"tool": SEND, "args": first}))
    await asyncio.wait_for(entered.wait(), 2)
    waiting = asyncio.create_task(servers[1].result("tool.call", {"tool": SEND, "args": queued}))
    await asyncio.sleep(0.05)  # the second send has used its approval and waits for the lock
    logout = asyncio.create_task((await cli(paths)).result("session.logout"))
    await asyncio.sleep(0.05)
    release.set()

    assert (await asyncio.wait_for(running, 2))["ok"] is True
    refused = await asyncio.wait_for(waiting, 2)
    assert refused["ok"] is False
    assert refused["error"]["code"] == "no_session"
    assert (await asyncio.wait_for(logout, 2))["telegram_confirmed"] is True
    assert [sent.text for sent in fake_gateway.sent] == ["running"]


# broker.status -------------------------------------------------------------------------------


@pytest.mark.usefixtures("logged_in")
async def test_status_checks_the_session_with_telegram_and_names_the_account(
    start_broker: Callable[..., Any], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    await start_broker()
    status = await (await cli(paths)).result("broker.status")
    assert status["session_state"] == "logged_in"
    assert status["logged_in"] is True
    assert status["account"] == ACCOUNT
    assert status["cause"] is None
    assert status["pid"] == os.getpid()
    assert status["clients"] == 0  # the status command is not a Claude Code session
    assert status["mcp_clients"] == 0
    assert "is_authorized" in fake_gateway.call_names()
    assert log_entries(paths) == []


async def test_status_with_nothing_stored_is_no_session(
    start_broker: Callable[..., Any], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    await start_broker()
    status = await (await cli(paths)).result("broker.status")
    assert (status["session_state"], status["logged_in"], status["account"]) == (
        "no_session",
        False,
        None,
    )
    assert fake_gateway.call_names() == []


@pytest.mark.usefixtures("logged_in")
async def test_a_session_ended_elsewhere_is_reported_and_forgotten(
    start_broker: Callable[..., Any], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    broker = await start_broker()
    fake_gateway.authorized = False
    status = await (await cli(paths)).result("broker.status")
    assert (status["session_state"], status["logged_in"], status["account"]) == (
        "ended",
        False,
        None,
    )
    assert broker.link.state.value == "closed"
    assert log_entries(paths) == []


@pytest.mark.usefixtures("logged_in")
async def test_a_session_that_ends_during_the_check_is_reported_ended(
    start_broker: Callable[..., Any], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    broker = await start_broker()
    fake_gateway.script("me", error=NotAuthorized())
    status = await (await cli(paths)).result("broker.status")
    assert status["session_state"] == "ended"
    assert broker.link.state.value == "closed"


@pytest.mark.usefixtures("logged_in")
async def test_a_locked_keychain_is_reported_with_its_cause(
    start_broker: Callable[..., Any], paths: FixedPaths, memory_keyring: MemoryKeyring
) -> None:
    await start_broker()
    memory_keyring.locked = True
    status = await (await cli(paths)).result("broker.status")
    assert (status["session_state"], status["cause"]) == ("keychain_locked", "locked")


@pytest.mark.parametrize(
    ("script", "budget"),
    [({"error": TelegramError("INTERNAL")}, 15.0), ({"hang": True}, 0.1)],
    ids=["telegram error", "past the budget"],
)
@pytest.mark.usefixtures("logged_in")
async def test_telegram_unreachable_is_reported_and_only_its_type_is_diagnosed(
    start_broker: Callable[..., Any],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    script: dict[str, Any],
    budget: float,
) -> None:
    fake_gateway.script("is_authorized", **script)
    await start_broker(status_budget=budget)
    status = await asyncio.wait_for((await cli(paths)).result("broker.status"), 3)
    assert (status["session_state"], status["logged_in"], status["account"]) == (
        "unreachable",
        False,
        None,
    )
    assert "status" in paths.diagnostics.read_text()
    assert "INTERNAL" not in paths.diagnostics.read_text()
    assert log_entries(paths) == []


@pytest.mark.usefixtures("logged_in")
async def test_status_counts_the_claude_sessions_apart_from_other_connections(
    start_broker: Callable[..., Any], paths: FixedPaths
) -> None:
    await start_broker()
    servers = [await RawClient.connect(paths) for _ in range(3)]
    approval_hook = await hook(paths)
    command = await cli(paths)
    status = await command.result("broker.status")
    assert (status["mcp_clients"], status["clients"]) == (3, 3)
    await servers[0].close()
    await asyncio.sleep(0.05)
    status = await command.result("broker.status")
    assert (status["mcp_clients"], status["clients"]) == (2, 2)
    await approval_hook.close()


def test_the_default_budgets_are_fifteen_and_thirty_seconds(paths: FixedPaths) -> None:
    assert STATUS_BUDGET_SECONDS == 15.0
    assert LOGOUT_BUDGET_SECONDS == 30.0
    broker = Broker(paths, gateway_factory=lambda s, i, h: FakeGateway())
    assert (broker.status_budget, broker.logout_budget) == (15.0, 30.0)
