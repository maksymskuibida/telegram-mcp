"""Claude Code's side of the confirmation flow, as the hooks see it."""

from __future__ import annotations

import json
from typing import Any

SEND = "tg_messages_send"
PREFIXED_SEND = f"mcp__plugin_telegram-mcp_telegram__{SEND}"


def pre_hook_input(args: dict[str, Any]) -> str:
    return json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": PREFIXED_SEND, "tool_input": args}
    )


def question_from(pre_answer: dict[str, Any]) -> str:
    """The question the deny reason tells Claude to ask, decoded from its JSON string."""
    reason = pre_answer["hookSpecificOutput"]["permissionDecisionReason"]
    encoded = reason.split("exactly: question ", 1)[1].split("; options", 1)[0]
    return str(json.loads(encoded))


def post_hook_input(question: str, pick: str | None) -> str:
    """The PostToolUse input after AskUserQuestion showed ``question``; None = no answer."""
    item = {
        "question": question,
        "header": "Telegram",
        "multiSelect": False,
        "options": [
            {"label": "Allow", "description": "Send it"},
            {"label": "Deny", "description": "Do not send"},
        ],
    }
    answers = {question: pick} if pick is not None else {}
    return json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [item]},
            "tool_response": {"questions": [item], "answers": answers},
        }
    )
