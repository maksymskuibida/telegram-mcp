"""Claude Code's side of the confirmation flow, as the hooks see it."""

from __future__ import annotations

import json
import re
from typing import Any

SEND = "tg_messages_send"
PREFIX = "mcp__plugin_telegram-mcp_telegram__"
PREFIXED_SEND = f"{PREFIX}{SEND}"
SEND_DESCRIPTIONS = ("Send it", "Do not send")
_JSON_TEXT = r'("(?:[^"\\]|\\.)*")'
_DESCRIPTIONS = re.compile(
    rf"options Allow \(description {_JSON_TEXT}\) and Deny \(description {_JSON_TEXT}\)"
)


def pre_hook_input(args: dict[str, Any], tool: str = SEND) -> str:
    return json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": f"{PREFIX}{tool}", "tool_input": args}
    )


def reason_of(pre_answer: dict[str, Any]) -> str:
    return str(pre_answer["hookSpecificOutput"]["permissionDecisionReason"])


def question_from(pre_answer: dict[str, Any]) -> str:
    """The question the deny reason tells Claude to ask, decoded from its JSON string."""
    encoded = reason_of(pre_answer).split("exactly: question ", 1)[1].split("; options", 1)[0]
    return str(json.loads(encoded))


def descriptions_from(pre_answer: dict[str, Any]) -> tuple[str, str]:
    """The descriptions the deny reason tells Claude to show under Allow and Deny."""
    match = _DESCRIPTIONS.search(reason_of(pre_answer))
    assert match is not None, reason_of(pre_answer)
    return str(json.loads(match[1])), str(json.loads(match[2]))


def post_hook_input(
    question: str, pick: str | None, descriptions: tuple[str, str] = SEND_DESCRIPTIONS
) -> str:
    """The PostToolUse input after AskUserQuestion showed ``question``; None = no answer."""
    item = {
        "question": question,
        "header": "Telegram",
        "multiSelect": False,
        "options": [
            {"label": "Allow", "description": descriptions[0]},
            {"label": "Deny", "description": descriptions[1]},
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
