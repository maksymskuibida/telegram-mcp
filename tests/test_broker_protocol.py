"""The broker's protocol, files and lifecycle (BR2.1, BR2.3, BR7.5, BR8.1, NFR7.1, NFR7.3,
NFR7.5, NFR11.3)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

import pytest

from support.brokers import make_broker, running_broker, save_fake_session
from support.rpc import RawClient
from support.samples import ALICE
from telegram_mcp.broker.lock import read_lock
from telegram_mcp.broker.server import BrokerStartError
from telegram_mcp.gateway.fake import FakeGateway
from telegram_mcp.paths import FixedPaths


@pytest.fixture
def paths(home: Path) -> FixedPaths:
    return FixedPaths.from_home(home)


def hello_params(paths: FixedPaths, **overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "protocol": 1,
        "secret": paths.secret.read_text(),
        "client": "mcp-server",
        "package_version": "test",
        "client_name": "claude-code",
        "client_version": "1",
    }
    params.update(overrides)
    return params


@pytest.mark.parametrize(
    ("method", "overrides", "code"),
    [
        ("tool.call", {}, -32600),
        ("hello", {"protocol": 2}, -32001),
        ("hello", {"secret": "0" * 64}, -32002),
        ("hello", {"secret": None}, -32002),
        ("hello", {"client": "browser"}, -32600),
    ],
)
async def test_hello_rules_refuse_and_close(
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    method: str,
    overrides: dict[str, object],
    code: int,
) -> None:
    async with running_broker(paths, fake_gateway):
        raw = await RawClient.open(paths)
        reply = await raw.call(method, hello_params(paths, **overrides))
        assert reply["error"]["code"] == code
        assert await raw.reader.readline() == b""


async def test_framing_errors_after_hello(paths: FixedPaths, fake_gateway: FakeGateway) -> None:
    async with running_broker(paths, fake_gateway):
        raw = await RawClient.connect(paths)
        broken = await raw.send_line(b"{not json\n")
        assert broken is not None
        assert broken["error"]["code"] == -32700
        unknown = await raw.call("session.export")
        assert unknown["error"]["code"] == -32601
        bad_params = await raw.send_line(
            b'{"jsonrpc":"2.0","id":3,"method":"tool.call","params":[]}\n'
        )
        assert bad_params is not None
        assert bad_params["error"]["code"] == -32600
        status = await raw.result("broker.status")
        assert status["clients"] == 1


async def test_a_version_mismatch_makes_the_idle_broker_exit(
    paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    broker = make_broker(paths, fake_gateway)
    await broker.start()
    raw = await RawClient.open(paths)
    reply = await raw.call("hello", hello_params(paths, protocol=99))
    assert reply["error"]["message"] == "version_mismatch"
    await asyncio.wait_for(broker.wait_stopped(), 2)
    assert not paths.socket.exists()


async def test_files_are_private_after_start(paths: FixedPaths, fake_gateway: FakeGateway) -> None:
    previous = os.umask(0o022)
    try:
        async with running_broker(paths, fake_gateway):
            modes = {
                path.name: path.stat().st_mode & 0o777
                for path in (
                    paths.data_dir,
                    paths.secret,
                    paths.socket,
                    paths.lock,
                    paths.diagnostics,
                )
            }
            assert modes == {
                "telegram-mcp": 0o700,
                "broker.secret": 0o600,
                "broker.sock": 0o600,
                "broker.lock": 0o600,
                "broker.err": 0o600,
            }
            assert re.fullmatch(r"[0-9a-f]{64}", paths.secret.read_text())
    finally:
        os.umask(previous)


async def test_a_wrong_mode_stops_the_broker_from_serving(
    paths: FixedPaths, fake_gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "chmod", lambda path, mode: None)
    broker = make_broker(paths, fake_gateway)
    paths.data_dir.mkdir(parents=True, mode=0o700)
    with pytest.raises(BrokerStartError, match="mode"):
        await broker.start()
    assert not paths.socket.exists()
    assert not paths.secret.exists()
    assert (read_lock(paths.lock) or {}).get("state") == "released"


async def test_the_secret_is_new_at_every_start_and_removed_at_stop(
    paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    async with running_broker(paths, fake_gateway):
        first = paths.secret.read_text()
    assert not paths.secret.exists()
    assert not paths.socket.exists()
    assert (read_lock(paths.lock) or {}).get("state") == "released"
    async with running_broker(paths, fake_gateway):
        assert paths.secret.read_text() != first


async def test_a_second_broker_does_not_start(paths: FixedPaths, fake_gateway: FakeGateway) -> None:
    async with running_broker(paths, fake_gateway):
        secret = paths.secret.read_text()
        with pytest.raises(BrokerStartError, match="holds the lock"):
            await make_broker(paths, fake_gateway).start()
        assert paths.secret.read_text() == secret


async def test_status_and_stop(paths: FixedPaths, fake_gateway: FakeGateway) -> None:
    save_fake_session()
    started = time.monotonic()
    broker = make_broker(paths, fake_gateway)
    await broker.start()
    assert time.monotonic() - started < 2  # NFR11.3: socket ready well inside the 5 s poll
    raw = await RawClient.connect(paths, client="cli")
    status = await raw.result("broker.status")
    # clients counts Claude Code sessions; the status command itself is not one.
    assert status == {
        "logged_in": True,
        "account": "Test Person (@tester, id 4242)",
        "clients": 0,
        "idle_seconds": 0,
        "session_state": "logged_in",
        "cause": None,
        "pid": os.getpid(),
        "mcp_clients": 0,
    }
    assert await raw.result("broker.stop") == {"ok": True}
    await asyncio.wait_for(broker.wait_stopped(), 2)
    assert not paths.socket.exists()


async def test_a_bad_settings_file_is_held_and_ordinary_work_continues(
    paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    save_fake_session()
    paths.data_dir.mkdir(parents=True, mode=0o700)
    paths.config.write_text('outbox_dir = "/usr/local/elsewhere"\n')
    async with running_broker(paths, fake_gateway) as broker:
        assert broker.settings_refusal is not None
        assert broker.settings_refusal.key == "outbox_dir"
        raw = await RawClient.connect(paths)
        listed = await raw.result("tool.call", {"tool": "tg_chats_list", "args": {}})
        assert listed["ok"] is True
    entries = [json.loads(line) for line in paths.log.read_text().splitlines()]
    assert entries[0]["tool"] == "settings"
    assert entries[0]["result"] == "not_allowed"
    assert "outbox_dir" in entries[0]["content"]
    assert "settings refused" in paths.diagnostics.read_text()


async def test_a_confirmed_send_logs_call_prompt_decision_and_result(
    paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    save_fake_session()
    async with running_broker(paths, fake_gateway):
        hook = await RawClient.connect(paths, client="approval-hook", client_name=None)
        args = {"chat": ALICE.id, "text": "logged text"}
        asked = await hook.result("approval.check", {"tool": "tg_messages_send", "args": args})
        await hook.result(
            "approval.answer",
            {
                "question": asked["question"],
                "options": ["Allow", "Deny"],
                "option_descriptions": ["Send it", "Do not send"],
                "multi_select": False,
                "pick": "Allow",
            },
        )
        server = await RawClient.connect(paths)
        sent = await server.result("tool.call", {"tool": "tg_messages_send", "args": args})
        assert sent["ok"] is True
    entries = [json.loads(line) for line in paths.log.read_text().splitlines()]
    events = [(e["event"], e["client"], e["decision"], e["result"]) for e in entries]
    assert events == [
        ("prompt", "approval-hook", None, "ok"),
        ("decision", "approval-hook", "allowed", "ok"),
        ("call", "mcp-server:claude-code", None, "ok"),
        ("result", "mcp-server:claude-code", None, "ok"),
    ]
    assert all(e["approval_id"] == asked["approval_id"] for e in entries if e["event"] != "call")
    assert entries[2]["content"] == "logged text"
    assert entries[3]["chat_id"] == ALICE.id
