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
from telegram_mcp.gateway.models import CodeExpired, SentCodeInfo, TelegramError
from telegram_mcp.login import Terminal, clean_hidden_paste, login, where_the_code_went
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
        method="phone",
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


@pytest.mark.parametrize(
    "typed",
    [
        "{hash} ",
        "{hash}\n",
        "\x1b[200~{hash}\x1b[201~",
        "\x1b[200~{hash}\n\x1b[201~",
        "{hash}​",
        "​{hash}⁠",
    ],
    ids=["trailing space", "newline", "paste markers", "paste with newline", "zero width", "both"],
)
async def test_a_pasted_api_hash_is_cleaned_before_it_is_checked(
    paths: FixedPaths, typed: str
) -> None:
    secrets = fake_secrets()
    gateway = FakeGateway(authorized=False, session_after_login=secrets.session)
    script = ScriptedTerminal(
        [f" {secrets.api_id} ", f" {PHONE}\t", f" {gateway.login_code} "],
        [typed.format(hash=secrets.api_hash)],
    )
    assert await run(paths, script, gateway) == 0
    assert "api_hash must be 32 hexadecimal characters." not in script.output
    loaded = SessionVault().load().session
    assert loaded == StoredSession(secrets.session, secrets.api_id, secrets.api_hash)


async def test_a_31_character_api_hash_is_still_refused(paths: FixedPaths) -> None:
    secrets = fake_secrets()
    gateway = FakeGateway(authorized=False)
    script = ScriptedTerminal(
        [str(secrets.api_id), PHONE, gateway.login_code],
        ["\x1b[200~" + secrets.api_hash[:31] + "\x1b[201~", secrets.api_hash],
    )
    assert await run(paths, script, gateway) == 0
    assert script.output.count("api_hash must be 32 hexadecimal characters.") == 1


def test_clean_hidden_paste_keeps_only_what_can_be_seen() -> None:
    assert clean_hidden_paste("\x1b[200~ ab​cd \r\n\x1b[201~") == "abcd"
    assert clean_hidden_paste("\x1b[2ab") == "[2ab"


NO_OTHER_WAY = " Telegram offers no other way to send it for this login."


@pytest.mark.parametrize(
    ("sent", "line"),
    [
        (
            SentCodeInfo("app"),
            "Telegram sent the code to your Telegram app: look for a message from Telegram "
            "(blue tick) on a device where this account is logged in.",
        ),
        (SentCodeInfo("sms"), "Telegram sent the code by SMS to your phone."),
        (SentCodeInfo("call"), "Telegram is calling your phone to say the code."),
        (
            SentCodeInfo("flash_call"),
            "Telegram is making a short call to your phone: the code is in the calling number.",
        ),
        (
            SentCodeInfo("missed_call"),
            "Telegram is calling your phone and hanging up: the code is the last digits of the "
            "calling number.",
        ),
        (
            SentCodeInfo("email", email_pattern="a***@e***.com"),
            "Telegram sent the code by email to a***@e***.com.",
        ),
        (SentCodeInfo("email"), "Telegram sent the code by email to your login email address."),
        (
            SentCodeInfo("fragment"),
            "Telegram sent the code to your anonymous number on Fragment (fragment.com).",
        ),
        (SentCodeInfo("other"), "Telegram sent the code in a way this version cannot name."),
    ],
)
def test_login_says_where_the_code_went_and_that_there_is_no_other_way(
    sent: SentCodeInfo, line: str
) -> None:
    assert where_the_code_went(sent) == line + NO_OTHER_WAY


@pytest.mark.parametrize(
    ("sent", "ending"),
    [
        (
            SentCodeInfo("app", next_delivery="sms", timeout_seconds=60),
            " If it does not arrive within 60 seconds, press Enter with no code to ask for it "
            "by SMS.",
        ),
        (
            SentCodeInfo("sms", next_delivery="call"),
            " If it does not arrive, press Enter with no code to ask for it by a phone call.",
        ),
    ],
)
def test_login_names_the_next_way_and_how_long_to_wait(sent: SentCodeInfo, ending: str) -> None:
    assert where_the_code_went(sent).endswith(ending)


async def test_enter_with_no_code_asks_for_the_next_way_and_uses_no_try(paths: FixedPaths) -> None:
    secrets = fake_secrets()
    gateway = FakeGateway(
        authorized=False,
        sent_code=SentCodeInfo("app", next_delivery="sms", timeout_seconds=60),
        resent_code=SentCodeInfo("sms"),
    )
    script = ScriptedTerminal(
        [str(secrets.api_id), PHONE, "", "11111", "22222", gateway.login_code],
        [secrets.api_hash],
    )
    assert await run(paths, script, gateway) == 0
    assert gateway.call_names().count("resend_code") == 1
    assert ("resend_code", {"phone": "+15550001234"}) in gateway.calls
    first = script.output.index(where_the_code_went(gateway.sent_code))
    assert script.output[first + 1] == where_the_code_went(gateway.resent_code)
    assert script.output.count("The code was wrong.") == 2


async def test_enter_with_no_other_way_asks_again_and_three_wrong_codes_still_stop(
    paths: FixedPaths, memory_keyring: MemoryKeyring
) -> None:
    secrets = fake_secrets()
    gateway = FakeGateway(authorized=False)
    script = ScriptedTerminal(
        [str(secrets.api_id), PHONE, "", "", "1", "2", "3"], [secrets.api_hash]
    )
    assert await run(paths, script, gateway) == 1
    assert "resend_code" not in gateway.call_names()
    assert script.prompts.count("The code Telegram sent you: ") == 5
    assert script.output[-1] == "The code was wrong 3 times; nothing was saved."
    assert memory_keyring.items == {}


async def test_a_refused_resend_keeps_the_code_already_sent(paths: FixedPaths) -> None:
    secrets = fake_secrets()
    gateway = FakeGateway(authorized=False, sent_code=SentCodeInfo("app", next_delivery="sms"))
    gateway.script("resend_code", error=TelegramError("SEND_CODE_UNAVAILABLE"))
    script = ScriptedTerminal(
        [str(secrets.api_id), PHONE, "", "", gateway.login_code], [secrets.api_hash]
    )
    assert await run(paths, script, gateway) == 0
    assert gateway.call_names().count("resend_code") == 1
    assert (
        "Telegram did not send the code another way (TelegramError); type the code you have."
        in script.output
    )


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
    assert script.output.count(where_the_code_went(gateway.sent_code)) == 2


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
