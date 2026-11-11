"""The W4 send flow end to end: the MCP server, both hooks and the broker each run as their own
process, as Claude Code runs them, on the fake gateway (FR4.1 to FR4.4, FR9.1).

Expiry needs 120 s to pass, so that path runs with the broker in this process and an injected
clock; everything else is real processes talking over the real socket."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import mcp_types as types
import pytest
from mcp import Client, StdioServerParameters

from support.brokers import FakeClock, running_broker, save_fake_session, subprocess_brokers
from support.file_keyring import FileKeyring
from support.flows import SEND, post_hook_input, pre_hook_input, question_from
from support.samples import ALICE, TEAM
from telegram_mcp.broker.client import BrokerClient
from telegram_mcp.gateway.fake import FAKE_RECORD_NAME, FakeGateway
from telegram_mcp.hooks import approval_post, approval_pre
from telegram_mcp.mcp_server import build_server
from telegram_mcp.paths import FixedPaths

__all__ = ["subprocess_brokers"]

CLAUDE = types.Implementation(name="claude-code", version="2.1.317")


async def hook(name: str, payload: str) -> dict[str, Any]:
    """Run `telegram-mcp hook <name>` as Claude Code does, with the hook JSON on stdin."""
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "telegram_mcp.cli", "hook", name],
        input=payload,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    answer: dict[str, Any] = json.loads(result.stdout)
    return answer


def sent_texts(home: Path) -> list[str]:
    record = home / FAKE_RECORD_NAME
    if not record.exists():
        return []
    calls = [json.loads(line) for line in record.read_text().splitlines()]
    return [call["args"]["text"] for call in calls if call["call"] == "send_text"]


def payload(result: types.CallToolResult) -> Any:
    content = result.content[0]
    assert isinstance(content, types.TextContent)
    return json.loads(content.text)


@pytest.fixture(autouse=True)
def logged_in_for_subprocesses(home: Path) -> None:
    """The subprocesses read the session from the test's file keyring."""
    keyring = FileKeyring()
    keyring.set_password("telegram-mcp", "session", "1Be2e-session-value-for-tests")
    keyring.set_password("telegram-mcp", "api_id", "4242")
    keyring.set_password("telegram-mcp", "api_hash", "ab" * 16)


@contextlib.asynccontextmanager
async def claude_code_session() -> AsyncIterator[Client]:
    """`telegram-mcp serve` started over stdio, as Claude Code starts it."""
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "telegram_mcp.cli", "serve"], env=os.environ.copy()
    )
    async with Client(server, client_info=CLAUDE) as client:
        yield client


async def confirm_with(question: str, pick: str | None) -> None:
    assert await hook("approval-post", post_hook_input(question, pick)) == {}


@pytest.mark.usefixtures("subprocess_brokers")
async def test_the_confirmed_send_runs_once(home: Path) -> None:
    args: dict[str, Any] = {"chat": ALICE.id, "text": "Hello from the end-to-end test"}
    async with claude_code_session() as session:
        listed = await session.call_tool("tg_chats_list", {})
        assert not listed.is_error
        assert len(payload(listed)["chats"]) == 5

        unasked = await session.call_tool(SEND, args)  # no hook ran: nothing is confirmed
        assert payload(unasked)["code"] == "needs_confirmation"

        asked = await hook("approval-pre", pre_hook_input(args))
        assert asked["hookSpecificOutput"]["permissionDecision"] == "deny"
        question = question_from(asked)
        assert question.endswith("Send this message to «Alice» (user, id 101)?\n" + args["text"])
        await confirm_with(question, "Allow")
        allowed = await hook("approval-pre", pre_hook_input(args))
        assert allowed["hookSpecificOutput"]["permissionDecision"] == "allow"

        sent = await session.call_tool(SEND, args)
        assert not sent.is_error
        assert payload(sent)["chat_id"] == ALICE.id
        again = await session.call_tool(SEND, args)
        assert payload(again)["code"] == "needs_confirmation"
    assert sent_texts(home) == [args["text"]]
    log = FixedPaths.from_home(home).log.read_text()
    assert '"decision":"allowed"' in log


@pytest.mark.parametrize("path", ["deny", "no answer", "reworded question", "changed arguments"])
@pytest.mark.usefixtures("subprocess_brokers")
async def test_the_alternative_paths_never_send(home: Path, path: str) -> None:
    args: dict[str, Any] = {"chat": TEAM.id, "text": f"alternative path: {path}"}
    question = question_from(await hook("approval-pre", pre_hook_input(args)))
    if path == "deny":
        await confirm_with(question, "Deny")
    elif path == "no answer":
        await confirm_with(question, None)
    elif path == "reworded question":
        await confirm_with(question.replace("Send this", "Post this"), "Allow")
    else:
        await confirm_with(question, "Allow")
        args = {**args, "text": args["text"] + " (changed)"}
    async with claude_code_session() as session:
        result = await session.call_tool(SEND, args)
    assert payload(result)["code"] == "needs_confirmation"
    asked_again = await hook("approval-pre", pre_hook_input(args))
    assert asked_again["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert sent_texts(home) == []


async def test_an_unanswered_question_expires_after_120_seconds(home: Path) -> None:
    save_fake_session()
    paths = FixedPaths.from_home(home)
    clock = FakeClock()
    gateway = FakeGateway(chats=[ALICE])
    args = {"chat": ALICE.id, "text": "too late"}

    def no_spawn() -> None:
        pytest.fail("the broker runs in this process")

    async with running_broker(paths, gateway, clock=clock) as broker:
        hook_client = BrokerClient(paths, "approval-hook", spawn=no_spawn)
        question = question_from(await approval_pre(pre_hook_input(args), hook_client))
        clock.advance(121)
        broker.sweep_expired()
        await approval_post(post_hook_input(question, "Allow"), hook_client)
        async with Client(build_server(paths, spawn=no_spawn), client_info=CLAUDE) as client:
            result = await client.call_tool(SEND, args)
        await hook_client.close()
    assert payload(result)["code"] == "needs_confirmation"
    assert gateway.sent == []
    assert '"decision":"timed_out"' in paths.log.read_text()
