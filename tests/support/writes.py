"""Confirming and sending messages through an in-process broker, for the scheduling tests."""

from __future__ import annotations

import asyncio
from typing import Any

from support.rpc import RawClient
from support.samples import ALICE
from telegram_mcp.paths import FixedPaths

SEND = "tg_messages_send"
LIST = "tg_chats_list"
DESCRIPTIONS = ["Send it", "Do not send"]


def send_args(text: str) -> dict[str, Any]:
    return {"chat": ALICE.id, "text": text}


async def hook(paths: FixedPaths) -> RawClient:
    return await RawClient.connect(paths, client="approval-hook", client_name=None)


async def check(client: RawClient, text: str) -> dict[str, Any]:
    result: dict[str, Any] = await client.result(
        "approval.check", {"tool": SEND, "args": send_args(text)}
    )
    return result


async def confirm(paths: FixedPaths, text: str) -> str:
    """Ask and allow a send of ``text`` the way the hook does; returns the approval id."""
    approval_hook = await hook(paths)
    try:
        asked = await check(approval_hook, text)
        assert asked["decision"] == "ask", asked
        answered = await approval_hook.result(
            "approval.answer",
            {
                "question": asked["question"],
                "options": ["Allow", "Deny"],
                "option_descriptions": DESCRIPTIONS,
                "multi_select": False,
                "pick": "Allow",
            },
        )
        assert answered == {"outcome": "confirmed"}
        return str(asked["approval_id"])
    finally:
        await approval_hook.close()


async def send(client: RawClient, text: str, seconds: float = 5.0) -> dict[str, Any]:
    result: dict[str, Any] = await asyncio.wait_for(
        client.result("tool.call", {"tool": SEND, "args": send_args(text)}), seconds
    )
    return result


async def list_chats(client: RawClient, seconds: float = 5.0) -> dict[str, Any]:
    result: dict[str, Any] = await asyncio.wait_for(
        client.result("tool.call", {"tool": LIST, "args": {}}), seconds
    )
    return result
