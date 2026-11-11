"""`telegram-mcp login` with a scripted terminal and the fake gateway (BR1.1 to BR1.4, BR1.8,
NFR1.3, NFR2.6)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from support.brokers import running_broker, save_fake_session
from support.memory_keyring import MemoryKeyring
from support.samples import fake_secrets
from telegram_mcp.broker.lock import BrokerLock, LockState, read_lock
from telegram_mcp.gateway.fake import FakeGateway
from telegram_mcp.gateway.models import CodeExpired
from telegram_mcp.login import Terminal, login
from telegram_mcp.paths import FixedPaths
from telegram_mcp.vault import SessionVault, StoredSession, VaultOutcome

PHONE = "+1 555 000 1234"


class ScriptedTerminal:
    """Answers prompts in order; secrets go through ``ask_secret`` only."""

    def __init__(self, answers: list[str], secrets: list[str]) -> None:
        self.answers: Iterator[str] = iter(answers)
        self.secrets: Iterator[str] = iter(secrets)
        self.prompts: list[str] = []
        self.secret_prompts: list[str] = []
        self.output: list[str] = []

    def terminal(self) -> Terminal:
        return Terminal(ask=self._ask, ask_secret=self._ask_secret, say=self.output.append)

    def _ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return next(self.answers)

    def _ask_secret(self, prompt: str) -> str:
        self.secret_prompts.append(prompt)
        return next(self.secrets)

    @property
    def text(self) -> str:
        return "\n".join(self.output)


@pytest.fixture
def paths(home: Path) -> FixedPaths:
    return FixedPaths.from_home(home)


async def never_stop() -> None:
    pytest.fail("no broker runs, so none must be stopped")


async def run(
    paths: FixedPaths,
    script: ScriptedTerminal,
    gateway: FakeGateway,
    stop_broker: Callable[[], object] = never_stop,
) -> int:
    return await login(
        paths,
        script.terminal(),
        gateway_factory=lambda session, api_id, api_hash: gateway,
        stop_broker=stop_broker,  # type: ignore[arg-type]
    )


def assert_lock_released(paths: FixedPaths) -> None:
    assert (read_lock(paths.lock) or {}).get("state") == "released"


async def test_a_new_login_saves_the_session_after_re_asking_bad_values(
    paths: FixedPaths,
) -> None:
    secrets = fake_secrets()
    gateway = FakeGateway(authorized=False, session_after_login=secrets.session)
    script = ScriptedTerminal(
        ["zero", str(secrets.api_id), "not a phone", PHONE, gateway.login_code],
        ["tooshort", secrets.api_hash.upper()],
    )
    assert await run(paths, script, gateway) == 0
    loaded = SessionVault().load()
    assert loaded.session == StoredSession(secrets.session, secrets.api_id, secrets.api_hash)
    assert "api_id must be a positive whole number." in script.output
    assert "api_hash must be 32 hexadecimal characters." in script.output
    assert script.output[-1] == "Logged in as Test Person (@tester, id 4242)."
    assert ("sign_in_phone", {"phone": "+15550001234"}) in gateway.calls
    assert_lock_released(paths)
    assert secrets.api_hash not in script.text
    assert secrets.session not in script.text


async def test_a_wrong_and_an_expired_code_are_named_and_asked_again(paths: FixedPaths) -> None:
    secrets = fake_secrets()
    gateway = FakeGateway(authorized=False)
    gateway.script("sign_in_code", error=CodeExpired())
    script = ScriptedTerminal(
        [str(secrets.api_id), PHONE, "11111", "00000", gateway.login_code], [secrets.api_hash]
    )
    assert await run(paths, script, gateway) == 0
    assert "The code expired; a new one was sent." in script.output
    assert "The code was wrong." in script.output
    assert gateway.call_names().count("sign_in_phone") == 2


async def test_three_wrong_codes_stop_with_nothing_saved(
    paths: FixedPaths, memory_keyring: MemoryKeyring
) -> None:
    secrets = fake_secrets()
    gateway = FakeGateway(authorized=False)
    script = ScriptedTerminal([str(secrets.api_id), PHONE, "1", "2", "3"], [secrets.api_hash])
    assert await run(paths, script, gateway) == 1
    assert script.output[-1] == "The code was wrong 3 times; nothing was saved."
    assert memory_keyring.items == {}
    assert_lock_released(paths)


async def test_the_two_step_password_is_read_without_echo_and_retried(paths: FixedPaths) -> None:
    secrets = fake_secrets()
    gateway = FakeGateway(authorized=False, password="right password")
    script = ScriptedTerminal(
        [str(secrets.api_id), PHONE, gateway.login_code],
        [secrets.api_hash, "wrong", "right password"],
    )
    assert await run(paths, script, gateway) == 0
    assert script.output.count("The password was wrong.") == 1
    assert script.secret_prompts == [
        "api_hash (hidden): ",
        "Two-step verification password (hidden): ",
        "Two-step verification password (hidden): ",
    ]
    assert not any("password" in prompt.lower() for prompt in script.prompts)


async def test_three_wrong_passwords_stop(paths: FixedPaths, memory_keyring: MemoryKeyring) -> None:
    secrets = fake_secrets()
    gateway = FakeGateway(authorized=False, password="pw")
    script = ScriptedTerminal(
        [str(secrets.api_id), PHONE, gateway.login_code], [secrets.api_hash, "a", "b", "c"]
    )
    assert await run(paths, script, gateway) == 1
    assert memory_keyring.items == {}


@pytest.mark.parametrize(("answer", "replaced"), [("n", False), ("", False), ("Yes", True)])
async def test_an_existing_session_is_replaced_only_on_yes(
    paths: FixedPaths, answer: str, replaced: bool
) -> None:
    old = save_fake_session()
    new = fake_secrets()
    gateway = FakeGateway(authorized=True, session_after_login=new.session)
    answers = [answer] + ([str(new.api_id), PHONE, gateway.login_code] if replaced else [])
    script = ScriptedTerminal(answers, [new.api_hash])
    assert await run(paths, script, gateway) == 0
    assert script.output[0] == (
        "This Mac is already logged in to Telegram as Test Person (@tester, id 4242)."
    )
    expected = StoredSession(new.session, new.api_id, new.api_hash) if replaced else old
    assert SessionVault().load().session == expected
    assert_lock_released(paths)


async def test_a_keychain_failure_saves_nothing_and_exits_2(
    paths: FixedPaths, memory_keyring: MemoryKeyring
) -> None:
    secrets = fake_secrets()
    gateway = FakeGateway(authorized=False)
    memory_keyring.fail_on_write = {3}
    script = ScriptedTerminal([str(secrets.api_id), PHONE, gateway.login_code], [secrets.api_hash])
    assert await run(paths, script, gateway) == 2
    assert "The macOS Keychain could not be used (access denied)" in script.output[-1]
    assert memory_keyring.items == {}


async def test_a_locked_keychain_refuses_before_anything_else(
    paths: FixedPaths, memory_keyring: MemoryKeyring
) -> None:
    memory_keyring.locked = True
    script = ScriptedTerminal([], [])
    assert await run(paths, script, FakeGateway()) == 2
    assert "unlock it" in script.output[0]
    assert not paths.lock.exists()


async def test_a_running_broker_is_stopped_and_another_login_refused(
    paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    save_fake_session()
    async with running_broker(paths, fake_gateway) as broker:
        stopped: list[bool] = []

        async def stop() -> None:
            stopped.append(True)
            await broker.stop()

        script = ScriptedTerminal(["n"], [])
        assert await run(paths, script, FakeGateway(), stop) == 0
        assert stopped == [True]
        assert_lock_released(paths)

    other = BrokerLock(paths.lock)
    assert other.try_hold(LockState.LOGIN)
    try:
        script = ScriptedTerminal([], [])
        assert await run(paths, script, FakeGateway()) == 1
        assert script.output == ["Another login is already running in a terminal."]
    finally:
        other.release()


async def test_ctrl_c_releases_the_lock_and_saves_nothing(
    paths: FixedPaths, memory_keyring: MemoryKeyring
) -> None:
    def interrupt(prompt: str) -> str:
        raise KeyboardInterrupt

    terminal = Terminal(ask=interrupt, ask_secret=interrupt, say=lambda line: None)
    code = await login(
        paths,
        terminal,
        gateway_factory=lambda s, i, h: FakeGateway(authorized=False),
        stop_broker=never_stop,
    )
    assert code == 1
    assert memory_keyring.items == {}
    assert_lock_released(paths)
    assert SessionVault().load().outcome is VaultOutcome.NO_SESSION
