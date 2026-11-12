"""No file, log line, diagnostic or tool result ever holds the session or the api_hash, and
every file the tool writes is private (NFR1.1, NFR1.2, NFR1.5, NFR7.1, NFR7.4)."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from support.brokers import FakeClock, running_broker, subprocess_brokers
from support.file_keyring import FileKeyring
from support.flows import SEND, post_hook_input, pre_hook_input, question_from
from support.rpc import RawClient
from support.samples import ALICE, FAMILY, fake_secrets, sample_chats
from support.standins import SAMPLE_ARGS, seeded_fake, stand_in_registry
from telegram_mcp.broker.client import BrokerClient
from telegram_mcp.gateway.fake import FakeGateway
from telegram_mcp.gateway.models import TelegramError
from telegram_mcp.hooks import approval_post, approval_pre
from telegram_mcp.login import Terminal, login
from telegram_mcp.paths import FixedPaths
from telegram_mcp.session_commands import logout, status
from telegram_mcp.vault import SessionVault, StoredSession

__all__ = ["subprocess_brokers"]

TOOL_FILES = {"activity.log", "broker.err", "broker.lock", "broker.secret", "broker.sock"}


@pytest.fixture
def paths(home: Path) -> FixedPaths:
    return FixedPaths.from_home(home)


def never_spawn() -> None:
    pytest.fail("the broker runs in this process")


box_for = post_hook_input


async def ask(hook: BrokerClient, args: dict[str, Any]) -> str:
    return question_from(await approval_pre(pre_hook_input(args), hook))


async def test_no_secret_reaches_any_file_log_or_result(paths: FixedPaths) -> None:
    session = "1Bsecret-session-" + os.urandom(24).hex()
    api_hash = os.urandom(16).hex()
    login_gateway = FakeGateway(authorized=False, session_after_login=session)
    answers = iter(["4242", "+15550001234", login_gateway.login_code])
    output: list[str] = []
    terminal = Terminal(
        ask=lambda p: next(answers), ask_secret=lambda p: api_hash, say=output.append
    )

    async def no_broker() -> None:
        return None

    assert (
        await login(
            paths,
            terminal,
            gateway_factory=lambda s, i, h: login_gateway,
            stop_broker=no_broker,
            method="phone",
        )
        == 0
    )

    clock = FakeClock()
    gateway = FakeGateway(chats=list(sample_chats()))
    results: list[Any] = []
    async with running_broker(paths, gateway, clock=clock) as broker:
        server = BrokerClient(paths, "mcp-server", client_name="claude-code", spawn=never_spawn)
        hook = BrokerClient(paths, "approval-hook", spawn=never_spawn)
        results.append(await server.tool_call("tg_chats_list", {}))
        confirmed = {"chat": ALICE.id, "text": f"note: {api_hash[:6]} is not the hash"}
        await approval_post(box_for(await ask(hook, confirmed), "Allow"), hook)
        results.append(await server.tool_call(SEND, confirmed))
        denied = {"chat": FAMILY.id, "text": "denied one"}
        await approval_post(box_for(await ask(hook, denied), "Deny"), hook)
        results.append(await server.tool_call(SEND, denied))
        await ask(hook, {"chat": ALICE.id, "text": "left to expire"})
        clock.advance(121)
        broker.sweep_expired()
        leaky = {"chat": ALICE.id, "text": "the error will quote secrets"}
        await approval_post(box_for(await ask(hook, leaky), "Allow"), hook)
        gateway.script("send_text", error=TelegramError(f"FLOOD {session} {api_hash}"))
        results.append(await server.tool_call(SEND, leaky))
        assert results[-1]["error"]["code"] == "telegram_error"
        assert "[redacted]" in results[-1]["error"]["message"]
        socket_mode = stat.S_IMODE(paths.socket.stat().st_mode)
        await server.close()
        await hook.close()

    assert socket_mode == 0o600
    written = {p.name: p for p in paths.data_dir.rglob("*")}
    assert set(written) <= TOOL_FILES
    for path in written.values():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name
        content = path.read_bytes()
        assert session.encode() not in content, path.name
        assert api_hash.encode() not in content, path.name
    assert stat.S_IMODE(paths.data_dir.stat().st_mode) == 0o700
    log = paths.log.read_text()
    assert '"decision":"allowed"' in log
    assert '"decision":"declined"' in log
    assert '"decision":"timed_out"' in log
    shown = repr(results) + "\n".join(output)
    assert session not in shown
    assert api_hash not in shown


@pytest.mark.usefixtures("subprocess_brokers")
async def test_files_are_private_even_with_a_022_umask(paths: FixedPaths, home: Path) -> None:
    keyring = FileKeyring()
    keyring.set_password("telegram-mcp", "session", "1Bsubprocess-session-value")
    keyring.set_password("telegram-mcp", "api_id", "4242")
    keyring.set_password("telegram-mcp", "api_hash", os.urandom(16).hex())
    command = f'umask 022; exec "{sys.executable}" -m telegram_mcp.cli broker'
    process = await asyncio.create_subprocess_exec("/bin/sh", "-c", command, env=os.environ.copy())
    try:
        client = BrokerClient(paths, "mcp-server", client_name="claude-code", spawn=never_spawn)
        for _ in range(100):
            if paths.socket.exists():
                break
            await asyncio.sleep(0.05)
        assert (await client.tool_call("tg_chats_list", {}))["ok"] is True
        modes = {p.name: stat.S_IMODE(p.stat().st_mode) for p in paths.data_dir.iterdir()}
        assert modes == {name: 0o600 for name in TOOL_FILES}
        assert stat.S_IMODE(paths.data_dir.stat().st_mode) == 0o700
        await client.stop_broker()
        await client.close()
        assert await asyncio.wait_for(process.wait(), 10) == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


# QR login, status and logout -----------------------------------------------------------------


def files_holding(home: Path, secret: str) -> list[str]:
    return [
        str(path.relative_to(home))
        for path in home.rglob("*")
        if path.is_file() and secret.encode() in path.read_bytes()
    ]


@pytest.mark.parametrize("scanned", [True, False], ids=["scanned", "refused"])
async def test_a_qr_token_is_shown_on_the_terminal_only(
    paths: FixedPaths, home: Path, scanned: bool
) -> None:
    token = "tg://login?token=SECRETQR" + os.urandom(16).hex()
    clock = FakeClock()
    gateway = FakeGateway(
        authorized=False,
        clock=clock,
        qr_time_passes=clock.advance,
        qr_tokens=[token],
        qr_scan_on_wait=1 if scanned else None,
    )
    if not scanned:
        gateway.script("wait_qr", error=TelegramError(f"refused {token}"))
    answers = iter(["4242"])
    output: list[str] = []
    terminal = Terminal(
        ask=lambda p: next(answers), ask_secret=lambda p: os.urandom(16).hex(), say=output.append
    )

    async def no_broker() -> None:
        return None

    code = await login(
        paths,
        terminal,
        gateway_factory=lambda s, i, h: gateway,
        stop_broker=no_broker,
        method="qr",
        clock=clock,
    )
    assert code == (0 if scanned else 1)
    assert [line for line in output if token in line] == [token]
    assert files_holding(home, token) == []
    assert files_holding(home, token.split("=", 1)[1]) == []


async def test_status_and_logout_never_show_a_secret_or_the_phone_number(
    paths: FixedPaths, home: Path
) -> None:
    phone = "+15550009876"
    session = "1Bsecret-session-" + os.urandom(24).hex()
    api_id, api_hash = 918273645, os.urandom(16).hex()
    SessionVault().save(StoredSession(session, api_id, api_hash))
    gateway = FakeGateway(chats=list(sample_chats()))
    secrets = [session, api_hash, str(api_id), phone, phone.lstrip("+")]
    shown: list[str] = []
    terminal = Terminal(ask=lambda p: "y", ask_secret=lambda p: "", say=shown.append)

    async with running_broker(paths, gateway) as broker:
        raw = await RawClient.connect(paths, client="cli", client_name=None)
        shown.append(repr(await raw.result("broker.status")))
        client = BrokerClient(paths, "cli", spawn=never_spawn)
        assert await status(paths, terminal, client=client) == 0
        assert await logout(paths, terminal, yes=False, client=client) == 0
        await client.close()
        await raw.close()
        await asyncio.wait_for(broker.wait_stopped(), 2)

    assert any("Logged out of Telegram as Test Person" in line for line in shown)
    log = paths.log.read_text()
    assert '"tool":"logout"' in log
    everything = "\n".join(shown) + log + paths.diagnostics.read_text()
    for secret in secrets:
        assert secret not in everything
        assert files_holding(home / "Library", secret) == []


# U2 NFR1.1: a run of every stand-in kind writes no secret ---------------------------------------


def with_secret(tool: str, args: dict[str, Any], secret: str) -> dict[str, Any]:
    """The sample call, with the secret placed in every text the question shows."""
    text_fields = {"text", "title", "first_name"}
    return {k: (f"{v} {secret}" if k in text_fields else v) for k, v in args.items()}


async def test_every_kind_logs_no_secret(paths: FixedPaths, fake_gateway: FakeGateway) -> None:
    values = fake_secrets()
    SessionVault().save(StoredSession(values.session, values.api_id, values.api_hash))
    seeded_fake(fake_gateway)
    secret_text = f"{values.session} {values.api_hash}"
    fake_gateway.add_message(ALICE.id, f"a message holding {secret_text}")
    async with running_broker(paths, fake_gateway, registry=stand_in_registry()):
        approval_hook = await RawClient.connect(paths, client="approval-hook", client_name=None)
        server = await RawClient.connect(paths)
        for tool, sample in sorted(SAMPLE_ARGS.items()):
            args = with_secret(tool, sample, secret_text)
            asked = await approval_hook.result("approval.check", {"tool": tool, "args": args})
            assert asked["decision"] == "ask", (tool, asked)
            answer = {
                "question": asked["question"],
                "options": asked["options"],
                "option_descriptions": asked["option_descriptions"],
                "multi_select": False,
                "pick": "Allow",
            }
            await approval_hook.result("approval.answer", answer)
            done = await server.result("tool.call", {"tool": tool, "args": args})
            assert done["ok"] is True, (tool, done)
        await approval_hook.close()
        await server.close()
    for path in (paths.log, paths.diagnostics):
        content = path.read_text(encoding="utf-8")
        assert values.session not in content, path.name
        assert values.api_hash not in content, path.name
    assert "SecretPart123" not in paths.log.read_text(encoding="utf-8")
    assert "[redacted]" in paths.log.read_text(encoding="utf-8")
