"""`telegram-mcp logout` and `telegram-mcp status`: what the person runs in their terminal to
end the session or see where it stands (C7).

Both go through the one broker, never around it: logout asks the broker to end the session
on Telegram and clear the Keychain; status asks the broker, which checks with Telegram. Neither
runs while a login holds the lock. Messages name the account only by name, @username and id."""

from __future__ import annotations

import asyncio
from typing import Any

from telegram_mcp.broker.client import (
    LOST,
    NOT_STARTED,
    BrokerClient,
    BrokerRefused,
    BrokerUnavailable,
    RequestLost,
)
from telegram_mcp.broker.lock import LockProbe, probe_lock
from telegram_mcp.login import Terminal
from telegram_mcp.paths import FixedPaths
from telegram_mcp.vault import SessionVault, VaultOutcome

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2
UNREAD_ACCOUNT = "an account whose name could not be read"
NOTHING_CHANGED = "Nothing changed."
NO_ANSWER = "The background connection did not answer in time."
LOGOUT_UNKNOWN = (
    "The logout was sent but its result is unknown; run `telegram-mcp status` to check."
)
KEYCHAIN_PROMPT = " A macOS Keychain prompt may be open."
LOGIN_RUNNING_LOGOUT = "A login is running in another terminal; finish or cancel it first."
LOGIN_RUNNING_STATUS = "A login is running in another terminal."
NOT_CONFIRMED = (
    "This Mac is logged out: the session was deleted from the Keychain. Telegram did not "
    "confirm the logout ({reason}), so the session may still be listed on Telegram's side. "
    "To end it, open Telegram on another device → Settings → Devices and terminate this "
    "session."
)
STATE_LINES = {
    "logged_in": ("Logged in to Telegram as {account}.", EXIT_OK),
    "no_session": ("Not logged in. Run `telegram-mcp login` in your terminal.", EXIT_REFUSED),
    "ended": (
        "Not logged in: the session was ended from another device. Run `telegram-mcp login` "
        "in your terminal.",
        EXIT_REFUSED,
    ),
    "keychain_locked": (
        "The macOS Keychain could not be used ({cause}); unlock it.",
        EXIT_REFUSED,
    ),
    "unreachable": (
        "A session is stored, but Telegram could not be reached to check it.",
        EXIT_FAILED,
    ),
}


class CommandStopped(Exception):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


# logout --------------------------------------------------------------------------------------


async def logout(
    paths: FixedPaths,
    terminal: Terminal,
    *,
    yes: bool,
    client: BrokerClient,
    vault: SessionVault | None = None,
) -> int:
    try:
        return await _logout(paths, terminal, yes, client, vault or SessionVault())
    except CommandStopped as stopped:
        terminal.say(str(stopped))
        return stopped.code


async def _logout(
    paths: FixedPaths, terminal: Terminal, yes: bool, client: BrokerClient, vault: SessionVault
) -> int:
    if probe_lock(paths.lock) is LockProbe.LOGIN:
        raise CommandStopped(LOGIN_RUNNING_LOGOUT, EXIT_FAILED)
    stored = await asyncio.to_thread(vault.load)
    if stored.outcome is VaultOutcome.KEYCHAIN_LOCKED:
        raise CommandStopped(
            f"The macOS Keychain could not be used ({stored.cause}); unlock it and run logout "
            "again.",
            EXIT_REFUSED,
        )
    if stored.outcome is VaultOutcome.NO_SESSION:
        await asyncio.to_thread(vault.delete)  # what a half-written login may have left
        terminal.say("Not logged in; nothing to log out.")
        return EXIT_OK
    account = None
    if not yes:
        account = await _account_for_question(client)
        if not _confirmed(terminal, account or UNREAD_ACCOUNT):
            terminal.say(NOTHING_CHANGED)
            return EXIT_OK
    outcome = await _send_logout(client)
    return _report_logout(terminal, outcome, account)


async def _account_for_question(client: BrokerClient) -> str | None:
    answer = await _ask_status(client, LOGIN_RUNNING_LOGOUT)
    return answer.get("account") if answer.get("session_state") == "logged_in" else None


def _confirmed(terminal: Terminal, account: str) -> bool:
    try:
        answer = terminal.ask(f"Log out of Telegram as {account}? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in ("y", "yes")


async def _send_logout(client: BrokerClient) -> dict[str, Any]:
    try:
        outcome = await client.logout()
    except BrokerUnavailable as error:
        raise _unavailable(error, LOGIN_RUNNING_LOGOUT) from error
    except RequestLost as lost:
        hint = KEYCHAIN_PROMPT if lost.timed_out else ""
        raise CommandStopped(LOGOUT_UNKNOWN + hint, EXIT_FAILED) from lost
    except BrokerRefused as refused:  # an older broker without session.logout
        raise CommandStopped(
            f"The background connection refused the logout ({refused}); nothing changed.",
            EXIT_FAILED,
        ) from refused
    if not isinstance(outcome, dict):
        raise CommandStopped(LOGOUT_UNKNOWN, EXIT_FAILED)
    return outcome


def _report_logout(terminal: Terminal, outcome: dict[str, Any], asked: str | None) -> int:
    confirmed = outcome.get("telegram_confirmed") is True
    reason = outcome.get("telegram_reason")
    if outcome.get("keychain_cleared") is not True:
        said = "ended" if confirmed else "did not confirm ending"
        terminal.say(
            f"Telegram {said} the session, but the macOS Keychain could not delete it "
            f"({outcome.get('keychain_cause') or 'access denied'}); unlock the Keychain and "
            "run logout again."
        )
        return EXIT_REFUSED
    if confirmed:
        account = outcome.get("account") or asked
        named = f" as {account}" if account else ""
        terminal.say(f"Logged out of Telegram{named}; the session was deleted from this Mac.")
    elif reason == "no_session":
        terminal.say(
            "The session had already been ended from another device; it was deleted from this Mac."
        )
    else:
        terminal.say(NOT_CONFIRMED.format(reason=_reason_text(reason, outcome.get("retry_after"))))
    return EXIT_OK


def _reason_text(reason: object, retry_after: object) -> str:
    if reason == "flood_wait" and isinstance(retry_after, int):
        return f"Telegram asked to wait {retry_after} s"
    if reason == "telegram_timeout":
        return "Telegram did not answer in time"
    return "a Telegram error"


# status --------------------------------------------------------------------------------------


async def status(paths: FixedPaths, terminal: Terminal, *, client: BrokerClient) -> int:
    probe = probe_lock(paths.lock)
    if probe is LockProbe.LOGIN:
        terminal.say(LOGIN_RUNNING_STATUS)
        return EXIT_FAILED
    try:
        answer = await _ask_status(client, LOGIN_RUNNING_STATUS)
    except CommandStopped as stopped:
        terminal.say(str(stopped))
        return stopped.code
    state = str(answer.get("session_state"))
    line, code = STATE_LINES.get(state, STATE_LINES["unreachable"])
    terminal.say(line.format(account=answer.get("account"), cause=answer.get("cause")))
    pid = answer.get("pid")
    # The idle exit (and its sentence on this line) arrives with the broker's idle stop.
    running = "running" if probe is LockProbe.RUNNING else "started now"
    terminal.say(f"Background connection: {running} (pid {pid}).")
    terminal.say(f"Claude sessions using it: {int(answer.get('mcp_clients') or 0)}.")
    return code


async def _ask_status(client: BrokerClient, login_running: str) -> dict[str, Any]:
    try:
        answer = await client.status()
    except BrokerUnavailable as error:
        raise _unavailable(error, login_running) from error
    except RequestLost as lost:
        if lost.timed_out:
            raise CommandStopped(NO_ANSWER, EXIT_FAILED) from lost
        raise CommandStopped(_not_started("the connection was lost"), EXIT_FAILED) from lost
    except BrokerRefused as refused:
        raise CommandStopped(_not_started(str(refused)), EXIT_FAILED) from refused
    if not isinstance(answer, dict):
        raise CommandStopped(_not_started("it answered nonsense"), EXIT_FAILED)
    return answer


def _unavailable(error: BrokerUnavailable, login_running: str) -> CommandStopped:
    if error.code == "login_in_progress":
        return CommandStopped(login_running, EXIT_FAILED)
    reasons = {
        NOT_STARTED: "it did not start within 5 seconds",
        LOST: "the connection was lost",
    }
    if error.code == "version_mismatch":
        return CommandStopped(_not_started("a different version is running"), EXIT_FAILED)
    return CommandStopped(_not_started(reasons.get(error.message, error.message)), EXIT_FAILED)


def _not_started(reason: str) -> str:
    return f"The background connection could not be started ({reason})."


# Entry points --------------------------------------------------------------------------------


def run_logout(paths: FixedPaths, terminal: Terminal, yes: bool) -> int:
    return asyncio.run(_with_client(paths, lambda c: logout(paths, terminal, yes=yes, client=c)))


def run_status(paths: FixedPaths, terminal: Terminal) -> int:
    return asyncio.run(_with_client(paths, lambda c: status(paths, terminal, client=c)))


async def _with_client(paths: FixedPaths, command: Any) -> int:
    client = BrokerClient(paths, "cli")
    try:
        result: int = await command(client)
        return result
    finally:
        await client.close()
