"""`telegram-mcp login`: phone login into the Keychain (C7; rules BR1.1 to BR1.4, BR1.8).

Everything is asked in the terminal only; the api_hash and the two-step password are read
without echo. Nothing is saved until sign-in succeeds, and the broker lock is held in state
``login`` for the whole command, released on every way out, Ctrl-C included."""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace

from telegram_mcp.broker.client import BrokerClient
from telegram_mcp.broker.lock import BrokerLock, LockProbe, LockState, probe_lock
from telegram_mcp.broker.server import default_gateway_factory
from telegram_mcp.gateway.models import (
    BadCode,
    BadPassword,
    CodeDelivery,
    CodeExpired,
    FloodWait,
    GatewayError,
    GatewayTimeout,
    Me,
    PasswordNeeded,
    SentCodeInfo,
    TelegramError,
    account_label,
)
from telegram_mcp.gateway.protocol import GatewayFactory, TelegramGateway
from telegram_mcp.paths import FixedPaths
from telegram_mcp.vault import SessionVault, StoredSession, VaultError, VaultOutcome

MAX_TRIES = 3
STOP_WAIT_SECONDS = 5.0
ACCOUNT_LOOKUP_SECONDS = 15.0
EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2
_API_HASH = re.compile(r"^[0-9a-fA-F]{32}$")
_PHONE = re.compile(r"^\+?\d{6,15}$")
_PASTE_MARKERS = ("\x1b[200~", "\x1b[201~")
_WHERE: dict[CodeDelivery, str] = {
    "app": (
        "Telegram sent the code to your Telegram app: look for a message from Telegram "
        "(blue tick) on a device where this account is logged in."
    ),
    "sms": "Telegram sent the code by SMS to your phone.",
    "call": "Telegram is calling your phone to say the code.",
    "flash_call": (
        "Telegram is making a short call to your phone: the code is in the calling number."
    ),
    "missed_call": (
        "Telegram is calling your phone and hanging up: the code is the last digits of the "
        "calling number."
    ),
    "email": "Telegram sent the code by email to {email}.",
    "fragment": "Telegram sent the code to your anonymous number on Fragment (fragment.com).",
    "other": "Telegram sent the code in a way this version cannot name.",
}
_NEXT_WAY: dict[CodeDelivery, str] = {
    "app": "the Telegram app",
    "sms": "SMS",
    "call": "a phone call",
    "flash_call": "a short call",
    "missed_call": "a missed call",
    "email": "email",
    "fragment": "Fragment",
    "other": "another way",
}


@dataclass
class Terminal:
    ask: Callable[[str], str] = input
    ask_secret: Callable[[str], str] = getpass.getpass
    say: Callable[[str], None] = field(default=print)


class LoginStopped(Exception):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def describe(me: Me) -> str:
    return account_label(me)


async def login(
    paths: FixedPaths,
    terminal: Terminal,
    *,
    gateway_factory: GatewayFactory,
    stop_broker: Callable[[], Awaitable[None]],
    vault: SessionVault | None = None,
) -> int:
    vault = vault or SessionVault(paths.keychain_service)
    stored = vault.load()
    if stored.outcome is VaultOutcome.KEYCHAIN_LOCKED:
        terminal.say(
            f"The macOS Keychain could not be used ({stored.cause}); unlock it and run login again."
        )
        return EXIT_REFUSED
    lock = BrokerLock(paths.lock)
    try:
        await _hold_login_lock(paths, lock, stop_broker)
        if stored.session is not None and not await _confirm_replace(
            terminal, gateway_factory, stored.session
        ):
            terminal.say("Nothing changed.")
            return EXIT_OK
        return await _sign_in(terminal, gateway_factory, vault, stored.session)
    except LoginStopped as stopped:
        terminal.say(str(stopped))
        return stopped.code
    except (KeyboardInterrupt, EOFError, asyncio.CancelledError):
        terminal.say("Login cancelled; nothing was saved.")
        return EXIT_FAILED
    finally:
        lock.release()


async def _hold_login_lock(
    paths: FixedPaths, lock: BrokerLock, stop_broker: Callable[[], Awaitable[None]]
) -> None:
    """BR1.4: take the lock as login, stopping a running broker first (up to 5 s)."""
    paths.data_dir.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileExistsError):
        paths.data_dir.mkdir(mode=0o700)
    if lock.try_hold(LockState.LOGIN):
        return
    if probe_lock(paths.lock) is LockProbe.LOGIN:
        raise LoginStopped("Another login is already running in a terminal.", EXIT_FAILED)
    with contextlib.suppress(Exception):
        await stop_broker()
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    while time.monotonic() < deadline:
        if lock.try_hold(LockState.LOGIN):
            return
        await asyncio.sleep(0.1)
    raise LoginStopped("The background connection did not stop; try again.", EXIT_FAILED)


async def _confirm_replace(
    terminal: Terminal, gateway_factory: GatewayFactory, old: StoredSession
) -> bool:
    """BR1.8: name the stored account and ask before replacing it. The lock is held, so no
    broker uses the old session at the same time."""
    account = "an account whose name could not be read"
    gateway = gateway_factory(old.session, old.api_id, old.api_hash)
    try:
        async with asyncio.timeout(ACCOUNT_LOOKUP_SECONDS):
            await gateway.connect()
            account = describe(await gateway.me())
    except (GatewayError, TimeoutError):
        pass
    finally:
        await _disconnect(gateway)
    terminal.say(f"This Mac is already logged in to Telegram as {account}.")
    answer = terminal.ask("Replace it? [y/N] ").strip().lower()
    return answer in ("y", "yes")


async def _sign_in(
    terminal: Terminal,
    gateway_factory: GatewayFactory,
    vault: SessionVault,
    old: StoredSession | None,
) -> int:
    api_id = _ask_until(
        terminal.ask,
        "api_id (from my.telegram.org): ",
        _parse_api_id,
        terminal,
        "api_id must be a positive whole number.",
    )
    api_hash = _ask_until(
        terminal.ask_secret,
        "api_hash (hidden): ",
        _parse_api_hash,
        terminal,
        "api_hash must be 32 hexadecimal characters.",
    )
    phone = _ask_until(
        terminal.ask,
        "Phone number, with country code: ",
        _parse_phone,
        terminal,
        "Give the phone number with its country code, like +15551234567.",
    )
    gateway = gateway_factory("", api_id, api_hash)
    try:
        await gateway.connect()
        sent = await gateway.sign_in_phone(phone)
        terminal.say(where_the_code_went(sent))
        me = await _enter_code(terminal, gateway, phone, sent)
        stored = StoredSession(gateway.export_session(), api_id, api_hash)
    except GatewayError as error:
        raise LoginStopped(
            f"Telegram refused the login ({type(error).__name__}); nothing was saved.", EXIT_FAILED
        ) from error
    finally:
        await _disconnect(gateway)
    try:
        if old is None:
            vault.save(stored)
        else:
            vault.replace(stored, old)
    except VaultError as error:
        raise LoginStopped(f"{error}.", EXIT_REFUSED) from error
    terminal.say(f"Logged in as {describe(me)}.")
    return EXIT_OK


def where_the_code_went(sent: SentCodeInfo) -> str:
    """The line printed before the code is asked for (BR1.10): where Telegram sent it, and
    either the next way it offers or that it offers none."""
    line = _WHERE[sent.delivery].format(email=sent.email_pattern or "your login email address")
    if sent.next_delivery is None:
        return f"{line} Telegram offers no other way to send it for this login."
    way = _NEXT_WAY[sent.next_delivery]
    if sent.timeout_seconds:
        wait = f"If it does not arrive within {sent.timeout_seconds} seconds"
    else:
        wait = "If it does not arrive"
    return f"{line} {wait}, press Enter with no code to ask for it by {way}."


async def _enter_code(
    terminal: Terminal, gateway: TelegramGateway, phone: str, sent: SentCodeInfo
) -> Me:
    """BR1.2: up to 3 codes, then up to 3 passwords when the account has one. An empty code
    asks for the code by the next way Telegram offers, or is asked again when it offers none;
    neither counts as a try (BR1.10)."""
    tries = 0
    while tries < MAX_TRIES:
        code = terminal.ask("The code Telegram sent you: ").strip()
        if not code:
            if sent.next_delivery is not None:
                sent = await _resend(terminal, gateway, phone, sent)
            continue
        tries += 1
        try:
            return await gateway.sign_in_code(code)
        except BadCode:
            terminal.say("The code was wrong.")
        except CodeExpired:
            terminal.say("The code expired; a new one was sent.")
            sent = await gateway.sign_in_phone(phone)
            terminal.say(where_the_code_went(sent))
        except PasswordNeeded:
            return await _enter_password(terminal, gateway)
    raise LoginStopped("The code was wrong 3 times; nothing was saved.", EXIT_FAILED)


async def _resend(
    terminal: Terminal, gateway: TelegramGateway, phone: str, sent: SentCodeInfo
) -> SentCodeInfo:
    """Ask for the code by the next way. When Telegram refuses, say so and keep the code
    already sent: the person can still type it."""
    try:
        resent = await gateway.resend_code(phone)
    except (TelegramError, FloodWait, GatewayTimeout) as error:
        terminal.say(
            f"Telegram did not send the code another way ({type(error).__name__}); "
            "type the code you have."
        )
        return replace(sent, next_delivery=None, timeout_seconds=None)
    terminal.say(where_the_code_went(resent))
    return resent


async def _enter_password(terminal: Terminal, gateway: TelegramGateway) -> Me:
    for _ in range(MAX_TRIES):
        password = terminal.ask_secret("Two-step verification password (hidden): ")
        try:
            return await gateway.sign_in_password(password)
        except BadPassword:
            terminal.say("The password was wrong.")
    raise LoginStopped("The password was wrong 3 times; nothing was saved.", EXIT_FAILED)


def _ask_until[T](
    ask: Callable[[str], str],
    prompt: str,
    parse: Callable[[str], T | None],
    terminal: Terminal,
    complaint: str,
) -> T:
    """Ask again until the value is well formed (BR1.1)."""
    while True:
        value = parse(ask(prompt).strip())
        if value is not None:
            return value
        terminal.say(complaint)


def _parse_api_id(text: str) -> int | None:
    return int(text) if text.isdigit() and int(text) > 0 else None


def clean_hidden_paste(text: str) -> str:
    """A value typed at a hidden prompt, cleaned of what the person cannot see there (BR1.1):
    the terminal's bracketed-paste markers, every other control or format character (a
    newline, a zero-width space), and surrounding whitespace."""
    for marker in _PASTE_MARKERS:
        text = text.replace(marker, "")
    shown = "".join(char for char in text if unicodedata.category(char) not in ("Cc", "Cf"))
    return shown.strip()


def _parse_api_hash(text: str) -> str | None:
    cleaned = clean_hidden_paste(text)
    return cleaned.lower() if _API_HASH.match(cleaned) else None


def _parse_phone(text: str) -> str | None:
    compact = re.sub(r"[\s\-()]", "", text)
    return compact if _PHONE.match(compact) else None


async def _disconnect(gateway: TelegramGateway) -> None:
    with contextlib.suppress(Exception):
        await gateway.disconnect()


def run_login_command(paths: FixedPaths) -> int:
    async def stop_broker() -> None:
        client = BrokerClient(paths, "cli", spawn=lambda: None)
        try:
            await client.stop_broker()
        finally:
            await client.close()

    try:
        return asyncio.run(
            login(
                paths,
                Terminal(),
                gateway_factory=default_gateway_factory(paths),
                stop_broker=stop_broker,
            )
        )
    except KeyboardInterrupt:
        print("Login cancelled; nothing was saved.")
        return EXIT_FAILED
