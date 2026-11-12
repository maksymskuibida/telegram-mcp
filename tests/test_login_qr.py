"""`telegram-mcp login` choosing QR, with the fake gateway and a fake clock: fresh codes for
three minutes, the two-step password, refusals, and nothing saved on any failure."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from support.brokers import FakeClock, save_fake_session
from support.memory_keyring import MemoryKeyring
from support.samples import fake_secrets
from telegram_mcp.broker.lock import read_lock
from telegram_mcp.gateway.fake import FakeGateway
from telegram_mcp.gateway.models import FloodWait, GatewayTimeout, TelegramError
from telegram_mcp.login import (
    METHOD_QUESTION,
    QR_GAVE_UP,
    QR_NEW_CODE,
    LoginMethod,
    Terminal,
    login,
)
from telegram_mcp.paths import FixedPaths
from telegram_mcp.qr import SCAN_HINT
from telegram_mcp.vault import SessionVault, StoredSession

LOGGED_IN = "Logged in as Test Person (@tester, id 4242)."
FIRST_URL = "tg://login?token=QRFIRSTTOKEN"


class ScriptedTerminal:
    def __init__(self, answers: list[Any], secrets: list[Any]) -> None:
        self.answers: Iterator[Any] = iter(answers)
        self.secrets: Iterator[Any] = iter(secrets)
        self.prompts: list[str] = []
        self.output: list[str] = []

    def terminal(self) -> Terminal:
        return Terminal(ask=self._ask, ask_secret=self._secret, say=self.output.append)

    def _ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._next(self.answers)

    def _secret(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._next(self.secrets)

    @staticmethod
    def _next(answers: Iterator[Any]) -> str:
        answer = next(answers)
        if isinstance(answer, BaseException):
            raise answer
        return str(answer)


@pytest.fixture
def paths(home: Path) -> FixedPaths:
    return FixedPaths.from_home(home)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def qr_gateway(clock: FakeClock, **options: Any) -> FakeGateway:
    return FakeGateway(
        authorized=False,
        clock=clock,
        qr_time_passes=clock.advance,
        qr_tokens=[FIRST_URL],
        **options,
    )


async def never_stop() -> None:
    pytest.fail("no broker runs, so none must be stopped")


async def run(
    paths: FixedPaths,
    script: ScriptedTerminal,
    gateway: FakeGateway,
    clock: FakeClock,
    method: LoginMethod | None = "qr",
) -> int:
    return await login(
        paths,
        script.terminal(),
        gateway_factory=lambda session, api_id, api_hash: gateway,
        stop_broker=never_stop,
        method=method,
        clock=clock,
    )


def credentials() -> tuple[list[Any], list[Any], StoredSession]:
    values = fake_secrets()
    stored = StoredSession("fake-session", values.api_id, values.api_hash)
    return [str(values.api_id)], [values.api_hash], stored


def waits(gateway: FakeGateway) -> list[float]:
    return [float(args["timeout_seconds"]) for name, args in gateway.calls if name == "wait_qr"]


def assert_nothing_saved(paths: FixedPaths, keyring: MemoryKeyring, gateway: FakeGateway) -> None:
    assert keyring.items == {}
    assert (read_lock(paths.lock) or {}).get("state") == "released"
    assert gateway.call_names()[-1] == "disconnect"


# Choosing QR ---------------------------------------------------------------------------------


async def test_the_question_is_asked_until_1_or_2_and_2_shows_a_code(
    paths: FixedPaths, clock: FakeClock
) -> None:
    gateway = qr_gateway(clock, qr_scan_on_wait=1)
    answers, secrets, stored = credentials()
    script = ScriptedTerminal(["3", " x ", "2", *answers], secrets)
    assert await run(paths, script, gateway, clock, method=None) == 0
    assert script.prompts[:3] == [METHOD_QUESTION] * 3
    assert script.output.count("Type 1 or 2.") == 2
    assert FIRST_URL in script.output
    assert SCAN_HINT in script.output
    assert script.output[-1] == LOGGED_IN
    assert SessionVault().load().session == stored


async def test_1_chooses_the_phone_login(paths: FixedPaths, clock: FakeClock) -> None:
    gateway = qr_gateway(clock)
    answers, secrets, _ = credentials()
    script = ScriptedTerminal(["1", *answers, "+15550001234", gateway.login_code], secrets)
    assert await run(paths, script, gateway, clock, method=None) == 0
    assert "sign_in_qr" not in gateway.call_names()
    assert "Phone number, with country code: " in script.prompts


async def test_the_qr_flag_asks_no_question_and_a_first_scan_logs_in(
    paths: FixedPaths, clock: FakeClock
) -> None:
    gateway = qr_gateway(clock, qr_scan_on_wait=1)
    answers, secrets, stored = credentials()
    script = ScriptedTerminal(answers, secrets)
    assert await run(paths, script, gateway, clock) == 0
    assert METHOD_QUESTION not in script.prompts
    assert gateway.call_names().count("sign_in_qr") == 1
    assert waits(gateway) == [30.0]
    assert SessionVault().load().session == stored
    assert (read_lock(paths.lock) or {}).get("state") == "released"


# Fresh codes for three minutes ---------------------------------------------------------------


async def test_a_scan_after_two_expired_codes_logs_in(paths: FixedPaths, clock: FakeClock) -> None:
    gateway = qr_gateway(clock, qr_scan_on_wait=3)
    answers, secrets, _ = credentials()
    script = ScriptedTerminal(answers, secrets)
    assert await run(paths, script, gateway, clock) == 0
    assert gateway.call_names().count("sign_in_qr") == 3
    assert script.output.count(QR_NEW_CODE) == 2
    assert waits(gateway) == [30.0, 30.0, 30.0]
    assert script.output[-1] == LOGGED_IN


async def test_nobody_scanning_stops_exactly_at_three_minutes(
    paths: FixedPaths, clock: FakeClock, memory_keyring: MemoryKeyring
) -> None:
    started = clock()
    gateway = qr_gateway(clock)
    answers, secrets, _ = credentials()
    script = ScriptedTerminal(answers, secrets)
    assert await run(paths, script, gateway, clock) == 1
    assert script.output[-1] == QR_GAVE_UP
    assert waits(gateway) == [30.0] * 6
    assert clock() - started == timedelta(seconds=180)
    assert gateway.call_names().count("sign_in_qr") == 6
    assert_nothing_saved(paths, memory_keyring, gateway)


async def test_the_last_wait_ends_at_the_deadline_not_the_expiry(
    paths: FixedPaths, clock: FakeClock
) -> None:
    gateway = qr_gateway(clock, qr_lifetime_seconds=50)
    answers, secrets, _ = credentials()
    script = ScriptedTerminal(answers, secrets)
    assert await run(paths, script, gateway, clock) == 1
    assert waits(gateway) == [50.0, 50.0, 50.0, 30.0]


async def test_no_new_code_is_asked_for_with_under_five_seconds_left(
    paths: FixedPaths, clock: FakeClock
) -> None:
    gateway = qr_gateway(clock, qr_lifetime_seconds=59)
    answers, secrets, _ = credentials()
    script = ScriptedTerminal(answers, secrets)
    assert await run(paths, script, gateway, clock) == 1
    assert waits(gateway) == [59.0, 59.0, 59.0]
    assert gateway.call_names().count("sign_in_qr") == 3


# The two-step password -----------------------------------------------------------------------


async def test_a_password_after_the_scan_is_asked_hidden_and_retried(
    paths: FixedPaths, clock: FakeClock
) -> None:
    gateway = qr_gateway(clock, qr_scan_on_wait=1, password="right")
    answers, secrets, stored = credentials()
    script = ScriptedTerminal(answers, [*secrets, "wrong", "right"])
    assert await run(paths, script, gateway, clock) == 0
    assert script.output.count("The password was wrong.") == 1
    assert script.prompts.count("Two-step verification password (hidden): ") == 2
    assert SessionVault().load().session == stored


async def test_three_wrong_passwords_save_nothing(
    paths: FixedPaths, clock: FakeClock, memory_keyring: MemoryKeyring
) -> None:
    gateway = qr_gateway(clock, qr_scan_on_wait=1, password="right")
    answers, secrets, _ = credentials()
    script = ScriptedTerminal(answers, [*secrets, "a", "b", "c"])
    assert await run(paths, script, gateway, clock) == 1
    assert script.output[-1] == "The password was wrong 3 times; nothing was saved."
    assert_nothing_saved(paths, memory_keyring, gateway)


# Refusals and interruptions ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "error", "said"),
    [
        ("sign_in_qr", TelegramError("SECRET_REASON"), "TelegramError"),
        ("sign_in_qr", GatewayTimeout(), "GatewayTimeout"),
        ("wait_qr", FloodWait(42), "FloodWait, wait 42 s"),
        ("wait_qr", TelegramError("SECRET_REASON"), "TelegramError"),
    ],
)
async def test_a_telegram_refusal_stops_with_its_type_only(
    paths: FixedPaths,
    clock: FakeClock,
    memory_keyring: MemoryKeyring,
    call: str,
    error: Any,
    said: str,
) -> None:
    gateway = qr_gateway(clock, qr_scan_on_wait=1)
    gateway.script(call, error=error)
    answers, secrets, _ = credentials()
    script = ScriptedTerminal(answers, secrets)
    assert await run(paths, script, gateway, clock) == 1
    assert script.output[-1] == f"Telegram refused the login ({said}); nothing was saved."
    assert "SECRET_REASON" not in "\n".join(script.output)
    assert_nothing_saved(paths, memory_keyring, gateway)


@pytest.mark.parametrize("stop", [KeyboardInterrupt(), EOFError()], ids=["ctrl-c", "eof"])
async def test_stopping_at_the_choice_saves_nothing(
    paths: FixedPaths, clock: FakeClock, memory_keyring: MemoryKeyring, stop: BaseException
) -> None:
    gateway = qr_gateway(clock)
    script = ScriptedTerminal([stop], [])
    assert await run(paths, script, gateway, clock, method=None) == 1
    assert script.output == ["Login cancelled; nothing was saved."]
    assert memory_keyring.items == {}
    assert (read_lock(paths.lock) or {}).get("state") == "released"


@pytest.mark.parametrize(
    "stop", [KeyboardInterrupt(), asyncio.CancelledError()], ids=["ctrl-c", "cancelled"]
)
async def test_stopping_while_waiting_for_a_scan_disconnects_and_saves_nothing(
    paths: FixedPaths, clock: FakeClock, memory_keyring: MemoryKeyring, stop: BaseException
) -> None:
    gateway = qr_gateway(clock)

    async def interrupted(timeout_seconds: float) -> Any:
        gateway.calls.append(("wait_qr", {"timeout_seconds": timeout_seconds}))
        raise stop

    gateway.wait_qr = interrupted  # type: ignore[method-assign]
    answers, secrets, _ = credentials()
    script = ScriptedTerminal(answers, secrets)
    assert await run(paths, script, gateway, clock) == 1
    assert script.output[-1] == "Login cancelled; nothing was saved."
    assert_nothing_saved(paths, memory_keyring, gateway)


async def test_a_qr_login_replaces_a_stored_account_only_after_yes(
    paths: FixedPaths, clock: FakeClock
) -> None:
    save_fake_session()
    gateway = qr_gateway(clock, qr_scan_on_wait=1)
    gateway.authorized = True
    answers, secrets, stored = credentials()
    script = ScriptedTerminal(["y", "2", *answers], secrets)
    assert await run(paths, script, gateway, clock, method=None) == 0
    assert script.prompts[:2] == ["Replace it? [y/N] ", METHOD_QUESTION]
    assert SessionVault().load().session == stored
