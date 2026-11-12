"""The one Telegram connection: a shared connect, back-off after failures, reconnect after a
drop, and a session Telegram no longer accepts."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest

from support.brokers import FakeClock, broker_starter, save_fake_session
from support.rpc import RawClient
from support.samples import fake_secrets
from support.writes import check, hook, list_chats
from telegram_mcp.broker.link import (
    LinkState,
    SessionRefused,
    TelegramLink,
    Unreachable,
    backoff_seconds,
)
from telegram_mcp.broker.server import Broker
from telegram_mcp.gateway.fake import FakeGateway
from telegram_mcp.gateway.models import GatewayTimeout, NotAuthorized
from telegram_mcp.paths import FixedPaths
from telegram_mcp.vault import StoredSession


@pytest.fixture
def paths(home: Path) -> FixedPaths:
    return FixedPaths.from_home(home)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def stored() -> StoredSession:
    values = fake_secrets()
    return StoredSession(values.session, values.api_id, values.api_hash)


@pytest.fixture
async def start_broker(
    paths: FixedPaths, fake_gateway: FakeGateway, clock: FakeClock
) -> AsyncIterator[Callable[..., Awaitable[Broker]]]:
    save_fake_session()
    async with broker_starter(paths, fake_gateway, clock) as start:
        yield start


def link_for(fake: FakeGateway, clock: FakeClock, connect_limit: float = 30.0) -> TelegramLink:
    return TelegramLink(
        lambda session, api_id, api_hash: fake, clock=clock, connect_limit=connect_limit
    )


def state_of(link: TelegramLink) -> LinkState:
    """Read the state through a call, so the type checker does not narrow it across awaits."""
    return link.state


async def test_calls_that_arrive_together_share_one_connect(
    fake_gateway: FakeGateway, clock: FakeClock, stored: StoredSession
) -> None:
    fake_gateway.script("connect", delay=0.1)
    link = link_for(fake_gateway, clock)
    gateways = await asyncio.gather(*(link.open(stored) for _ in range(5)))
    assert all(gateway is fake_gateway for gateway in gateways)
    assert fake_gateway.call_names().count("connect") == 1
    assert link.connects_started == 1
    assert link.state is LinkState.OPEN


async def test_a_caller_that_gives_up_never_cancels_the_connect_for_the_others(
    fake_gateway: FakeGateway, clock: FakeClock, stored: StoredSession
) -> None:
    fake_gateway.script("connect", delay=0.3)
    link = link_for(fake_gateway, clock)
    impatient = asyncio.create_task(asyncio.wait_for(link.open(stored), 0.05))
    patient = asyncio.create_task(link.open(stored))
    with pytest.raises(TimeoutError):
        await impatient
    assert await patient is fake_gateway
    assert fake_gateway.call_names().count("connect") == 1


async def test_failed_connects_back_off_1_2_4_8_16_then_30_seconds(
    fake_gateway: FakeGateway, clock: FakeClock, stored: StoredSession
) -> None:
    fake_gateway.script("connect", error=GatewayTimeout(), times=7)
    link = link_for(fake_gateway, clock)
    for attempt, wait in enumerate([1, 2, 4, 8, 16, 30, 30], start=1):
        with pytest.raises(Unreachable) as failed:
            await link.open(stored)
        assert failed.value.retry_after == wait
        clock.advance(wait - 0.5)
        with pytest.raises(Unreachable) as at_once:
            await link.open(stored)
        assert at_once.value.retry_after == 1
        assert fake_gateway.call_names().count("connect") == attempt
        clock.advance(0.5)
    assert await link.open(stored) is fake_gateway
    assert [backoff_seconds(n) for n in range(1, 9)] == [1, 2, 4, 8, 16, 30, 30, 30]


async def test_a_successful_connect_resets_the_back_off(
    fake_gateway: FakeGateway, clock: FakeClock, stored: StoredSession
) -> None:
    fake_gateway.script("connect", error=GatewayTimeout(), times=2)
    link = link_for(fake_gateway, clock)
    for wait in (1, 2):
        with pytest.raises(Unreachable):
            await link.open(stored)
        clock.advance(wait)
    await link.open(stored)
    assert (link.failures_in_a_row, link.retry_at) == (0, None)
    link.mark_dropped()
    fake_gateway.script("connect", error=GatewayTimeout())
    with pytest.raises(Unreachable) as failed:
        await link.open(stored)
    assert failed.value.retry_after == 1


async def test_a_connect_over_its_limit_is_a_failure(
    fake_gateway: FakeGateway, clock: FakeClock, stored: StoredSession
) -> None:
    fake_gateway.script("connect", hang=True)
    link = link_for(fake_gateway, clock, connect_limit=0.1)
    started = time.monotonic()
    with pytest.raises(Unreachable) as failed:
        await link.open(stored)
    assert time.monotonic() - started < 1
    assert failed.value.retry_after == 1
    assert link.state is LinkState.BACKING_OFF


async def test_a_refused_session_closes_the_gateway_and_the_next_call_builds_a_new_one(
    clock: FakeClock, stored: StoredSession
) -> None:
    built: list[FakeGateway] = []

    def factory(session: str, api_id: int, api_hash: str) -> FakeGateway:
        built.append(FakeGateway(authorized=len(built) > 0))
        return built[-1]

    link = TelegramLink(factory, clock=clock)
    with pytest.raises(SessionRefused):
        await link.open(stored)
    assert built[0].call_names() == ["connect", "is_authorized", "disconnect"]
    assert link.state is LinkState.CLOSED
    assert await link.open(stored) is built[1]
    assert len(built) == 2


async def test_a_failed_session_check_on_an_open_link_reconnects_or_refuses(
    fake_gateway: FakeGateway, clock: FakeClock, stored: StoredSession
) -> None:
    link = link_for(fake_gateway, clock)
    await link.open(stored)
    fake_gateway.script("is_authorized", error=GatewayTimeout())
    assert await link.open(stored) is fake_gateway  # dropped, then opened again
    assert fake_gateway.call_names().count("connect") == 2
    fake_gateway.script("is_authorized", error=NotAuthorized())
    with pytest.raises(SessionRefused):
        await link.open(stored)
    fake_gateway.script("connect", error=NotAuthorized())
    with pytest.raises(SessionRefused):
        await link.open(stored)
    assert link.state is LinkState.CLOSED


async def test_closing_while_connecting_cancels_the_attempt(
    fake_gateway: FakeGateway, clock: FakeClock, stored: StoredSession
) -> None:
    fake_gateway.script("connect", hang=True)
    link = link_for(fake_gateway, clock)
    waiting = asyncio.create_task(link.open(stored))
    await asyncio.sleep(0.05)
    assert state_of(link) is LinkState.CONNECTING
    await link.close(0.1)
    assert state_of(link) is LinkState.CLOSED
    with pytest.raises(asyncio.CancelledError):
        await waiting


async def test_a_dropped_connection_is_opened_again_by_the_same_broker(
    start_broker: Callable[..., Awaitable[Broker]],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
) -> None:
    broker = await start_broker()
    server = await RawClient.connect(paths)
    secret = paths.secret.read_text()
    assert (await list_chats(server))["ok"] is True
    fake_gateway.script("list_chats", error=GatewayTimeout())
    assert (await list_chats(server))["error"]["code"] == "telegram_timeout"
    assert state_of(broker.link) is LinkState.CLOSED
    assert (await list_chats(server))["ok"] is True
    assert fake_gateway.call_names().count("connect") == 2
    assert state_of(broker.link) is LinkState.OPEN
    # Same broker process, same secret: the broker was never restarted.
    raw = await RawClient.open(paths)
    greeting = await raw.call(
        "hello",
        {"protocol": 1, "secret": secret, "client": "cli", "package_version": "test"},
    )
    assert greeting["result"]["pid"] == os.getpid()
    assert paths.secret.read_text() == secret


async def test_during_a_back_off_checks_refuse_at_once_and_the_change_is_reported(
    start_broker: Callable[..., Awaitable[Broker]],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    clock: FakeClock,
) -> None:
    broker = await start_broker()
    server = await RawClient.connect(paths)
    fake_gateway.script("connect", error=GatewayTimeout())
    failed = await list_chats(server)
    assert failed["error"] == {
        "code": "telegram_error",
        "message": "Telegram could not be reached; trying again in 1 s.",
        "retry_after": 1,
    }
    started = time.monotonic()
    refused = await check(await hook(paths), "during the back-off")
    assert time.monotonic() - started < 0.5
    assert (refused["decision"], refused["code"]) == ("refuse", "telegram_error")
    assert broker.gate.open_count() == 0
    clock.advance(1)
    assert (await list_chats(server))["ok"] is True
    diagnostics = paths.diagnostics.read_text()
    assert "connect to Telegram failed (GatewayTimeout, 1 in a row)" in diagnostics
    assert "calls fail at once for 1 s" in diagnostics
    assert "Telegram reachable again after 1 failed connects" in diagnostics
