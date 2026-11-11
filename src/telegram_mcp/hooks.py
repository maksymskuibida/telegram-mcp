"""The ApprovalHook (`telegram-mcp hook approval-pre|approval-post`; C6, BR4.1 to BR4.3).

approval-pre turns the broker's approval check into Claude Code's permission decision, and
denies on every failure. approval-post relays the person's pick from the question box for
each question that carries a ``[tg-`` id, and decides nothing itself."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, Protocol, TextIO

from telegram_mcp.broker.client import BrokerClient, BrokerUnavailable
from telegram_mcp.paths import FixedPaths

TG_PREFIX = "[tg-"
NOT_RUN = "it was not run"
CONFIRMATION_STEP = "Not sent yet: this is the confirmation step."


class ApprovalBroker(Protocol):
    async def approval_check(self, tool: str, args: object) -> Any: ...

    async def approval_answer(self, params: dict[str, Any]) -> Any: ...


def _pre_answer(decision: str, reason: str | None = None) -> dict[str, Any]:
    specific: dict[str, Any] = {"hookEventName": "PreToolUse", "permissionDecision": decision}
    if reason is not None:
        specific["permissionDecisionReason"] = reason
    return {"hookSpecificOutput": specific}


def _deny(problem: str) -> dict[str, Any]:
    return _pre_answer("deny", f"telegram-mcp could not check this call ({problem}), so {NOT_RUN}.")


def ask_reason(question: str) -> str:
    """The deny reason that dictates the question box (C6, NFR3.1). The question is given as
    a JSON string so that its line break and quotes reach the box exactly. It opens by saying
    this is a step, not a failure (BR4.1, BR5.2)."""
    return (
        CONFIRMATION_STEP
        + " Ask the person with AskUserQuestion, exactly: question "
        + json.dumps(question, ensure_ascii=False)
        + '; options Allow (description "Send it") and Deny (description "Do not send"), '
        "in that order, multiSelect false; then call the tool again with the same arguments."
    )


def strip_prefix(tool_name: str) -> str:
    """Claude Code names a plugin's MCP tools ``mcp__<plugin and server>__<tool>``."""
    if tool_name.startswith("mcp__"):
        return tool_name.rsplit("__", 1)[-1]
    return tool_name


async def approval_pre(raw: str, broker: ApprovalBroker) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except ValueError:
        return _deny("the hook input is not JSON")
    if not isinstance(payload, dict):
        return _deny("the hook input is not an object")
    tool_name, tool_input = payload.get("tool_name"), payload.get("tool_input")
    if not isinstance(tool_name, str) or not tool_name or not isinstance(tool_input, dict):
        return _deny("the hook input has no tool name or arguments")
    try:
        answer = await broker.approval_check(strip_prefix(tool_name), tool_input)
    except BrokerUnavailable as unavailable:
        return _deny(f"{unavailable.code}: {unavailable.message}")
    except Exception as error:  # any failure blocks the call (BR4.1, NFR2.4)
        return _deny(f"the background process failed: {type(error).__name__}")
    return _decision_from(answer)


def _decision_from(answer: Any) -> dict[str, Any]:
    if not isinstance(answer, dict):
        return _deny("the background process gave no decision")
    decision = answer.get("decision")
    if decision == "allow":
        return _pre_answer("allow")
    if decision == "ask" and isinstance(answer.get("question"), str):
        return _pre_answer("deny", ask_reason(answer["question"]))
    code, message = answer.get("code"), answer.get("message")
    if decision == "refuse" and isinstance(code, str) and isinstance(message, str):
        return _pre_answer("deny", f"{code}: {message}")
    return _deny("the background process gave an unknown decision")


async def approval_post(raw: str, broker: ApprovalBroker) -> dict[str, Any]:
    """Relay every ``[tg-`` question's pick; the output never decides anything."""
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    for params in _relays(payload):
        try:
            await broker.approval_answer(params)
        except Exception as error:  # a lost relay leaves the approval pending; it expires
            print(
                f"telegram-mcp: could not record the answer ({type(error).__name__})",
                file=sys.stderr,
            )
    return {}


def _relays(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    tool_input = payload.get("tool_input")
    response = payload.get("tool_response")
    response = response if isinstance(response, dict) else {}
    questions = response.get("questions")
    if not isinstance(questions, list) and isinstance(tool_input, dict):
        questions = tool_input.get("questions")
    answers = response.get("answers")
    answers = answers if isinstance(answers, dict) else {}
    if not isinstance(questions, list):
        return []
    relays = []
    for item in questions:
        if not isinstance(item, dict) or not isinstance(item.get("question"), str):
            continue
        text = item["question"]
        if not text.startswith(TG_PREFIX):
            continue
        relays.append(_relay(item, text, answers.get(text)))
    return relays


def _relay(item: dict[str, Any], text: str, pick: object) -> dict[str, Any]:
    options = item.get("options")
    options = options if isinstance(options, list) else []
    labels = [o.get("label") if isinstance(o, dict) else None for o in options]
    descriptions = [o.get("description") if isinstance(o, dict) else None for o in options]
    multi_select = item.get("multiSelect", False)
    return {
        "question": text,
        "options": [label if isinstance(label, str) else "" for label in labels],
        "option_descriptions": (
            descriptions if all(isinstance(d, str) for d in descriptions) else None
        ),
        "multi_select": multi_select if isinstance(multi_select, bool) else None,
        "pick": pick if isinstance(pick, str) else "",
    }


def run_hook(
    name: str, paths: FixedPaths, stdin: TextIO | None = None, stdout: TextIO | None = None
) -> int:
    """Read the hook input, ask the broker, print one JSON answer. Always exit 0: a pre hook
    that cannot decide prints a deny."""
    raw = (stdin or sys.stdin).read()
    output = asyncio.run(_run(name, raw, paths))
    print(json.dumps(output, ensure_ascii=False), file=stdout or sys.stdout)
    return 0


async def _run(name: str, raw: str, paths: FixedPaths) -> dict[str, Any]:
    broker = BrokerClient(paths, "approval-hook")
    try:
        if name == "approval-pre":
            return await approval_pre(raw, broker)
        return await approval_post(raw, broker)
    finally:
        await broker.close()
