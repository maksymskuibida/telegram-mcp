"""The MCP server through the SDK's in-process client, against a broker on the fake gateway
(BR5.1 to BR5.3, NFR10.2, C5)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import mcp_types as types
import pytest
from mcp import Client

from support.brokers import running_broker, save_fake_session
from support.samples import ALICE
from telegram_mcp.broker.client import BrokerClient
from telegram_mcp.gateway.fake import FakeGateway
from telegram_mcp.hooks import approval_post, approval_pre
from telegram_mcp.mcp_server import build_server, to_call_result
from telegram_mcp.paths import FixedPaths

SEND = "tg_messages_send"
CLAUDE = types.Implementation(name="claude-code", version="2.1.317")


@pytest.fixture
def paths(home: Path) -> FixedPaths:
    return FixedPaths.from_home(home)


def never_spawn() -> None:
    pytest.fail("the MCP server must not start a broker here")


def payload(result: types.CallToolResult) -> Any:
    assert len(result.content) == 1
    content = result.content[0]
    assert isinstance(content, types.TextContent)
    return json.loads(content.text)


def client(paths: FixedPaths, info: types.Implementation = CLAUDE) -> Client:
    return Client(build_server(paths, spawn=never_spawn), client_info=info)


async def test_lists_the_two_tools_with_closed_schemas(paths: FixedPaths) -> None:
    async with client(paths) as session:
        listed = await session.list_tools()
    tools = {tool.name: tool for tool in listed.tools}
    assert set(tools) == {"tg_chats_list", SEND}
    assert all(tool.input_schema["additionalProperties"] is False for tool in tools.values())
    description = tools[SEND].description or ""
    assert "Nothing is sent until the person confirms" in description


async def test_a_send_runs_through_check_answer_and_call(
    paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    save_fake_session()
    args = {"chat": str(ALICE.id), "text": "hello from claude"}
    async with running_broker(paths, fake_gateway), client(paths) as session:
        first = await session.call_tool(SEND, args)
        assert first.is_error
        assert payload(first)["code"] == "needs_confirmation"

        hook = BrokerClient(paths, "approval-hook", spawn=never_spawn)
        hook_input = {"tool_name": f"mcp__plugin_telegram-mcp_telegram__{SEND}", "tool_input": args}
        pre = await approval_pre(json.dumps(hook_input), hook)
        reason = pre["hookSpecificOutput"]["permissionDecisionReason"]
        question = json.loads(reason.split("exactly: question ", 1)[1].split("; options", 1)[0])
        box = {
            "question": question,
            "header": "Telegram",
            "multiSelect": False,
            "options": [
                {"label": "Allow", "description": "Send it"},
                {"label": "Deny", "description": "Do not send"},
            ],
        }
        post_input = {
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [box]},
            "tool_response": {"questions": [box], "answers": {question: "Allow"}},
        }
        await approval_post(json.dumps(post_input), hook)
        allowed = await approval_pre(json.dumps(hook_input), hook)
        assert allowed["hookSpecificOutput"]["permissionDecision"] == "allow"
        await hook.close()

        sent = await session.call_tool(SEND, args)
        assert not sent.is_error
        data = payload(sent)
        assert data["chat_id"] == ALICE.id
        assert isinstance(data["message_id"], int)
    assert [s.text for s in fake_gateway.sent] == ["hello from claude"]


async def test_another_client_cannot_write_but_can_read(
    paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    save_fake_session()
    other = types.Implementation(name="some-desktop-app", version="1")
    async with client(paths, other) as session:
        refused = await session.call_tool(SEND, {"chat": ALICE.id, "text": "x"})
        assert refused.is_error
        assert payload(refused) == {
            "code": "unsupported_client",
            "message": (
                "This client cannot show confirmation prompts; this action is not supported here."
            ),
        }
        async with running_broker(paths, fake_gateway):
            listed = await session.call_tool("tg_chats_list", {})
            assert not listed.is_error
            assert len(payload(listed)["chats"]) == 5


async def test_no_session_is_a_tool_error(paths: FixedPaths, fake_gateway: FakeGateway) -> None:
    async with running_broker(paths, fake_gateway), client(paths) as session:
        result = await session.call_tool("tg_chats_list", {})
    assert result.is_error
    assert payload(result)["code"] == "no_session"
    assert fake_gateway.calls == []


async def test_invalid_arguments_are_a_tool_error_naming_the_argument(
    paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    save_fake_session()
    async with running_broker(paths, fake_gateway), client(paths) as session:
        result = await session.call_tool("tg_chats_list", {"limit": 500})
    assert result.is_error
    error = payload(result)
    assert error["code"] == "invalid_arguments"
    assert "limit" in error["message"]


def test_the_error_shape_is_code_and_message_plus_retry_after_when_given() -> None:
    plain = to_call_result({"ok": False, "error": {"code": "telegram_error", "message": "m"}})
    assert plain.is_error
    assert payload(plain) == {"code": "telegram_error", "message": "m"}
    waited = to_call_result(
        {"ok": False, "error": {"code": "flood_wait", "message": "w", "retry_after": 120}}
    )
    assert payload(waited) == {"code": "flood_wait", "message": "w", "retry_after": 120}
    nonsense = to_call_result({"ok": False, "error": "x"})
    assert payload(nonsense)["code"] == "telegram_error"


async def test_one_broker_connection_is_kept_for_the_session(
    paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    save_fake_session()
    async with running_broker(paths, fake_gateway) as broker, client(paths) as session:
        for _ in range(3):
            assert not (await session.call_tool("tg_chats_list", {})).is_error
        status = await broker._status(None, {})  # type: ignore[arg-type]
        assert status["clients"] == 1
