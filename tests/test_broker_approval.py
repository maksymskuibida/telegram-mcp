"""The broker's approval path, written before the broker (team Testing Posture).

Covers BR3.6, BR3.10, BR3.11, BR4.2's client rule, BR5.3, NFR2.2, NFR2.3, NFR2.5 and NFR11.6,
over a real Unix socket with the fake gateway."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from support.memory_keyring import MemoryKeyring
from support.rpc import RawClient
from support.samples import ALICE, fake_secrets
from telegram_mcp.broker.server import Broker
from telegram_mcp.gateway.fake import FakeGateway
from telegram_mcp.gateway.models import (
    ChatNotFound,
    NotAuthorized,
    OutcomeUnknown,
    TelegramError,
)
from telegram_mcp.paths import FixedPaths
from telegram_mcp.vault import SessionVault, StoredSession

SEND = "tg_messages_send"
SEND_ARGS = {"chat": ALICE.id, "text": "hello from the test"}
DESCRIPTIONS = ["Send it", "Do not send"]


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def paths(home: Path) -> FixedPaths:
    return FixedPaths.from_home(home)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def logged_in() -> StoredSession:
    values = fake_secrets()
    stored = StoredSession(values.session, values.api_id, values.api_hash)
    SessionVault().save(stored)
    return stored


BrokerFactory = Callable[..., Broker]


@pytest.fixture
async def start_broker(
    paths: FixedPaths, fake_gateway: FakeGateway, clock: FakeClock
) -> AsyncIterator[Callable[..., Any]]:
    started: list[Broker] = []

    async def start(**options: Any) -> Broker:
        broker = Broker(
            paths,
            gateway_factory=lambda session, api_id, api_hash: fake_gateway,
            clock=clock,
            **options,
        )
        await broker.start()
        started.append(broker)
        return broker

    yield start
    for broker in started:
        await broker.stop()


async def hook(paths: FixedPaths) -> RawClient:
    return await RawClient.connect(paths, client="approval-hook", client_name=None)


async def check(client: RawClient, args: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = await client.result(
        "approval.check", {"tool": SEND, "args": args if args is not None else SEND_ARGS}
    )
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


async def call(client: RawClient, args: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = await client.result(
        "tool.call", {"tool": SEND, "args": args if args is not None else SEND_ARGS}
    )
    return result


async def confirm(paths: FixedPaths) -> str:
    approval_hook = await hook(paths)
    asked = await check(approval_hook)
    assert asked["decision"] == "ask"
    assert await relay(approval_hook, asked["question"]) == "confirmed"
    await approval_hook.close()
    return str(asked["approval_id"])


# BR3.11 step 3: the vault first ------------------------------------------------------------


@pytest.mark.usefixtures("logged_in")
async def test_one_allow_runs_exactly_one_send(
    start_broker: Callable[..., Any], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    await start_broker()
    approval_hook = await hook(paths)
    asked = await check(approval_hook)
    assert asked["decision"] == "ask"
    assert asked["options"] == ["Allow", "Deny"]
    assert asked["option_descriptions"] == DESCRIPTIONS
    assert asked["question"].startswith(f"[tg-{asked['approval_id']}] Send this message to «Alice»")
    assert await relay(approval_hook, asked["question"]) == "confirmed"
    assert await check(approval_hook) == {"decision": "allow"}
    server = await RawClient.connect(paths)
    first = await call(server)
    assert first["ok"] is True
    assert first["data"]["chat_id"] == ALICE.id
    second = await call(server)
    assert second["ok"] is False
    assert second["error"]["code"] == "needs_confirmation"
    assert [sent.text for sent in fake_gateway.sent] == [SEND_ARGS["text"]]


@pytest.mark.parametrize(
    ("setup", "code"),
    [("nothing stored", "no_session"), ("locked", "keychain_locked")],
)
async def test_a_vault_failure_stops_before_any_telegram_call_or_approval(
    start_broker: Callable[..., Any],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    memory_keyring: MemoryKeyring,
    setup: str,
    code: str,
) -> None:
    broker = await start_broker()
    memory_keyring.locked = setup == "locked"
    approval_hook = await hook(paths)
    refused = await check(approval_hook)
    assert refused["decision"] == "refuse"
    assert refused["code"] == code
    server = await RawClient.connect(paths)
    result = await call(server)
    assert result["error"]["code"] == code
    listed = await server.result("tool.call", {"tool": "tg_chats_list", "args": {}})
    assert listed["error"]["code"] == code
    assert fake_gateway.calls == []
    assert broker.gate.open_count() == 0


async def test_a_confirmed_approval_survives_a_session_failure_unused(
    start_broker: Callable[..., Any],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    logged_in: StoredSession,
) -> None:
    await start_broker()
    await confirm(paths)
    server = await RawClient.connect(paths)
    fake_gateway.authorized = False
    assert (await call(server))["error"]["code"] == "no_session"
    fake_gateway.authorized = True
    bad = await call(server, {**SEND_ARGS, "approval_id": "x"})
    assert bad["error"]["code"] == "invalid_arguments"
    assert fake_gateway.sent == []
    assert (await call(server))["ok"] is True
    assert len(fake_gateway.sent) == 1


# BR3.6, BR4.2: only the hook confirms, only a confirmed approval runs -----------------------


@pytest.mark.usefixtures("logged_in")
async def test_a_write_with_no_confirmed_approval_needs_confirmation(
    start_broker: Callable[..., Any], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    await start_broker()
    server = await RawClient.connect(paths)
    result = await call(server)
    assert result == {
        "ok": False,
        "error": {
            "code": "needs_confirmation",
            "message": (
                "This action needs the person's confirmation; call it again so the "
                "confirmation hook can ask."
            ),
        },
    }
    assert fake_gateway.sent == []


@pytest.mark.usefixtures("logged_in")
async def test_an_answer_from_a_connection_that_is_not_the_hook_changes_nothing(
    start_broker: Callable[..., Any], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    await start_broker()
    approval_hook = await hook(paths)
    asked = await check(approval_hook)
    for kind in ("mcp-server", "cli"):
        other = await RawClient.connect(paths, client=kind)
        assert await relay(other, asked["question"]) == "unknown"
    server = await RawClient.connect(paths)
    assert (await call(server))["error"]["code"] == "needs_confirmation"
    assert await relay(approval_hook, asked["question"]) == "confirmed"


@pytest.mark.usefixtures("logged_in")
async def test_a_deny_never_sends(
    start_broker: Callable[..., Any], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    await start_broker()
    approval_hook = await hook(paths)
    asked = await check(approval_hook)
    assert await relay(approval_hook, asked["question"], "Deny") == "declined"
    server = await RawClient.connect(paths)
    assert (await call(server))["error"]["code"] == "needs_confirmation"
    again = await check(approval_hook)
    assert again["decision"] == "ask"
    assert again["approval_id"] != asked["approval_id"]
    assert fake_gateway.sent == []


# NFR2.2: failure injection at each step of approval.check ----------------------------------


@pytest.mark.parametrize(
    ("args", "script", "code"),
    [
        ({**SEND_ARGS, "chat": "@alice"}, None, "invalid_arguments"),
        ({**SEND_ARGS, "approval_id": "abc"}, None, "invalid_arguments"),
        ({**SEND_ARGS, "text": 1.5}, None, "invalid_arguments"),
        ({**SEND_ARGS, "chat": 99999}, None, "not_found"),
        (SEND_ARGS, ChatNotFound(), "not_found"),
        (SEND_ARGS, NotAuthorized(), "no_session"),
        (SEND_ARGS, TelegramError("RPC_FAILED"), "telegram_error"),
    ],
)
@pytest.mark.usefixtures("logged_in")
async def test_any_failure_while_checking_refuses_and_prepares_nothing(
    start_broker: Callable[..., Any],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    args: dict[str, Any],
    script: Exception | None,
    code: str,
) -> None:
    broker = await start_broker()
    if script is not None:
        fake_gateway.script("resolve_chat", error=script)  # type: ignore[arg-type]
    approval_hook = await hook(paths)
    refused = await check(approval_hook, args)
    assert refused["decision"] == "refuse"
    assert refused["code"] == code
    assert refused["message"]
    assert broker.gate.open_count() == 0
    assert fake_gateway.sent == []


@pytest.mark.usefixtures("logged_in")
async def test_an_unknown_tool_or_a_malformed_check_is_refused(
    start_broker: Callable[..., Any], paths: FixedPaths
) -> None:
    broker = await start_broker()
    approval_hook = await hook(paths)
    unknown = await approval_hook.result("approval.check", {"tool": "tg_mystery", "args": {}})
    assert (unknown["decision"], unknown["code"]) == ("refuse", "not_found")
    malformed = await approval_hook.result("approval.check", {"tool": SEND, "args": "chat=1"})
    assert (malformed["decision"], malformed["code"]) == ("refuse", "invalid_arguments")
    listed = await approval_hook.result("approval.check", {"tool": "tg_chats_list", "args": {}})
    assert listed == {"decision": "allow"}
    assert broker.gate.open_count() == 0


@pytest.mark.usefixtures("logged_in")
async def test_check_shares_one_ten_second_budget_for_telegram(
    start_broker: Callable[..., Any], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    broker = await start_broker(check_budget=0.3)
    fake_gateway.script("resolve_chat", hang=True)
    approval_hook = await hook(paths)
    started = time.monotonic()
    refused = await check(approval_hook)
    assert time.monotonic() - started < 2
    assert (refused["decision"], refused["code"]) == ("refuse", "telegram_timeout")
    assert broker.gate.open_count() == 0


def test_the_default_check_budget_is_ten_seconds(paths: FixedPaths) -> None:
    broker = Broker(paths, gateway_factory=lambda s, i, h: FakeGateway())
    assert broker.check_budget == 10.0


# NFR2.3: a failure before Telegram never uses the approval; after, nothing is resent -------


async def test_a_write_that_cannot_be_logged_is_refused_and_the_approval_kept(
    start_broker: Callable[..., Any],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    logged_in: StoredSession,
) -> None:
    await start_broker()
    await confirm(paths)
    paths.log.unlink()
    paths.log.mkdir()
    server = await RawClient.connect(paths)
    refused = await call(server)
    assert refused["error"]["code"] == "telegram_error"
    assert "activity log" in refused["error"]["message"]
    paths.log.rmdir()
    assert (await call(server))["ok"] is True
    assert len(fake_gateway.sent) == 1


@pytest.mark.parametrize(
    ("script", "code"),
    [
        ({"error": TelegramError("CHAT_WRITE_FORBIDDEN")}, "telegram_error"),
        ({"drop_after_send": True}, "outcome_unknown"),
        ({"error": OutcomeUnknown()}, "outcome_unknown"),
    ],
)
async def test_a_failure_after_the_send_started_is_never_resent(
    start_broker: Callable[..., Any],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    logged_in: StoredSession,
    script: dict[str, Any],
    code: str,
) -> None:
    await start_broker()
    await confirm(paths)
    fake_gateway.script("send_text", **script)
    server = await RawClient.connect(paths)
    failed = await call(server)
    assert failed["error"]["code"] == code
    assert (await call(server))["error"]["code"] == "needs_confirmation"
    assert fake_gateway.call_names().count("send_text") == 1


# BR3.9: the stored target ----------------------------------------------------------------


async def test_a_confirmed_send_goes_to_the_chat_stored_at_check_time(
    start_broker: Callable[..., Any],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    logged_in: StoredSession,
) -> None:
    await start_broker()
    await confirm(paths)
    fake_gateway.chats.clear()
    server = await RawClient.connect(paths)
    assert (await call(server))["ok"] is True
    assert fake_gateway.sent[0].message.chat_id == ALICE.id
    assert "resolve_chat" not in fake_gateway.call_names()[-2:]


# BR5.3: other clients --------------------------------------------------------------------


@pytest.mark.usefixtures("logged_in")
async def test_a_client_that_is_not_claude_code_cannot_write(
    start_broker: Callable[..., Any], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    await start_broker()
    await confirm(paths)
    other = await RawClient.connect(paths, client_name="some-other-client")
    refused = await call(other)
    assert refused["error"]["code"] == "unsupported_client"
    listed = await other.result("tool.call", {"tool": "tg_chats_list", "args": {}})
    assert listed["ok"] is True
    assert fake_gateway.sent == []


# NFR2.5: a restart forgets every approval ------------------------------------------------


async def test_a_restarted_broker_forgets_approvals(
    start_broker: Callable[..., Any],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    logged_in: StoredSession,
) -> None:
    first = await start_broker()
    approval_hook = await hook(paths)
    pending = await check(approval_hook)
    await approval_hook.close()
    await confirm(paths)
    await first.stop()
    await start_broker()
    approval_hook = await hook(paths)
    assert await relay(approval_hook, pending["question"]) == "unknown"
    server = await RawClient.connect(paths)
    assert (await call(server))["error"]["code"] == "needs_confirmation"
    assert fake_gateway.sent == []


# BR3.7: expiry, with the broker's sweep ---------------------------------------------------


async def test_an_unanswered_approval_expires_and_is_logged_timed_out(
    start_broker: Callable[..., Any],
    paths: FixedPaths,
    clock: FakeClock,
    logged_in: StoredSession,
) -> None:
    broker = await start_broker()
    approval_hook = await hook(paths)
    asked = await check(approval_hook)
    clock.advance(121)
    broker.sweep_expired()
    assert await relay(approval_hook, asked["question"]) == "unknown"
    entries = paths.log.read_text().splitlines()
    assert any('"decision":"timed_out"' in entry for entry in entries)
