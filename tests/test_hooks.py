"""The ApprovalHook, written before the code (team Testing Posture). Covers BR4.1, BR4.2, BR4.3
and NFR2.4. Claude Code's hook input and the AskUserQuestion result follow the shapes Claude
Code records: ``tool_response`` holds ``questions`` and ``answers`` keyed by question text."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from support.brokers import subprocess_brokers
from support.flows import question_from
from telegram_mcp.broker.client import BrokerUnavailable, RequestLost
from telegram_mcp.hooks import approval_post, approval_pre, run_hook
from telegram_mcp.paths import FixedPaths

__all__ = ["subprocess_brokers"]

PREFIXED = "mcp__plugin_telegram-mcp_telegram__tg_messages_send"
ARGS = {"chat": 101, "text": "hi"}
QUESTION = "[tg-abcdefghij] Send this message to «Alice» (user, id 101)?\nhi"
ASK = {
    "decision": "ask",
    "approval_id": "abcdefghij",
    "question": QUESTION,
    "options": ["Allow", "Deny"],
    "option_descriptions": ["Send it", "Do not send"],
    "multi_select": False,
}


class StubBroker:
    def __init__(self, check: Any = None, error: Exception | None = None) -> None:
        self.check_answer = check
        self.error = error
        self.checks: list[tuple[str, object]] = []
        self.answers: list[dict[str, Any]] = []

    async def approval_check(self, tool: str, args: object) -> Any:
        self.checks.append((tool, args))
        if self.error is not None:
            raise self.error
        return self.check_answer

    async def approval_answer(self, params: dict[str, Any]) -> Any:
        self.answers.append(params)
        if self.error is not None:
            raise self.error
        return {"outcome": "confirmed"}


def pre_input(tool_name: str = PREFIXED, tool_input: object = ARGS) -> str:
    return json.dumps(
        {
            "session_id": "s",
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": tool_input,
            "permission_mode": "default",
        }
    )


def decision(output: dict[str, Any]) -> tuple[str, str | None]:
    specific = output["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    return specific["permissionDecision"], specific.get("permissionDecisionReason")


# PreToolUse -------------------------------------------------------------------------------


async def test_the_prefix_is_stripped_and_allow_is_passed_through() -> None:
    broker = StubBroker({"decision": "allow"})
    output = await approval_pre(pre_input(), broker)
    assert output == {
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}
    }
    assert broker.checks == [("tg_messages_send", ARGS)]


async def test_ask_denies_with_the_exact_question_options_and_descriptions() -> None:
    output = await approval_pre(pre_input(), StubBroker(ASK))
    verdict, reason = decision(output)
    assert verdict == "deny"
    assert reason == (
        "Not sent yet: this is the confirmation step. "
        "Ask the person with AskUserQuestion, exactly: question "
        + json.dumps(QUESTION, ensure_ascii=False)
        + '; options Allow (description "Send it") and Deny (description "Do not send"), '
        "in that order, multiSelect false; then call the tool again with the same arguments."
    )


async def test_the_deny_reason_starts_with_the_confirmation_step_and_carries_the_question() -> None:
    _, reason = decision(await approval_pre(pre_input(), StubBroker(ASK)))
    assert reason is not None
    assert reason.startswith("Not sent yet: this is the confirmation step. ")
    assert question_from(await approval_pre(pre_input(), StubBroker(ASK))) == QUESTION


async def test_refuse_denies_with_the_code_and_message() -> None:
    refusal = {"decision": "refuse", "code": "no_session", "message": "Not logged in."}
    verdict, reason = decision(await approval_pre(pre_input(), StubBroker(refusal)))
    assert (verdict, reason) == ("deny", "no_session: Not logged in.")


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        json.dumps({"tool_name": PREFIXED}),
        pre_input(tool_input="chat=1"),
        pre_input(tool_name=""),
        json.dumps({"tool_name": 5, "tool_input": {}}),
    ],
)
async def test_malformed_input_denies_without_asking_the_broker(raw: str) -> None:
    broker = StubBroker({"decision": "allow"})
    verdict, reason = decision(await approval_pre(raw, broker))
    assert verdict == "deny"
    assert reason
    assert broker.checks == []


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (None, BrokerUnavailable("login_in_progress", "A login is running.")),
        (None, RequestLost(timed_out=True)),
        (None, RuntimeError("anything at all")),
        ({"decision": "maybe"}, None),
        ({"decision": "ask", "question": 5}, None),
        ({"decision": "refuse"}, None),
        ("allow", None),
    ],
)
async def test_a_broker_failure_or_a_bad_answer_denies(
    answer: Any, error: Exception | None
) -> None:
    verdict, reason = decision(await approval_pre(pre_input(), StubBroker(answer, error)))
    assert verdict == "deny"
    assert reason
    assert "was not run" in reason


async def test_a_login_in_progress_is_named_in_the_deny_reason() -> None:
    broker = StubBroker(error=BrokerUnavailable("login_in_progress", "A login is running."))
    _, reason = decision(await approval_pre(pre_input(), broker))
    assert reason is not None
    assert "login_in_progress: A login is running." in reason


# PostToolUse -------------------------------------------------------------------------------


def post_input(questions: list[dict[str, Any]], answers: dict[str, str] | None) -> str:
    response: dict[str, Any] = {"questions": questions}
    if answers is not None:
        response["answers"] = answers
    return json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": questions},
            "tool_response": response,
        }
    )


def box(question: str, multi_select: bool = False) -> dict[str, Any]:
    return {
        "question": question,
        "header": "Telegram",
        "options": [
            {"label": "Allow", "description": "Send it"},
            {"label": "Deny", "description": "Do not send"},
        ],
        "multiSelect": multi_select,
    }


async def test_only_tg_questions_are_relayed_with_everything_the_person_saw() -> None:
    broker = StubBroker()
    other = {"question": "Which colour?", "header": "Colour", "options": [], "multiSelect": False}
    output = await approval_post(
        post_input(
            [other, box(QUESTION, multi_select=True)], {QUESTION: "Allow", "Which colour?": "Red"}
        ),
        broker,
    )
    assert output == {}
    assert broker.answers == [
        {
            "question": QUESTION,
            "options": ["Allow", "Deny"],
            "option_descriptions": ["Send it", "Do not send"],
            "multi_select": True,
            "pick": "Allow",
        }
    ]


@pytest.mark.parametrize("answers", [None, {}, {"another question": "Allow"}])
async def test_a_missing_answer_is_relayed_as_empty_so_it_declines(
    answers: dict[str, str] | None,
) -> None:
    broker = StubBroker()
    assert await approval_post(post_input([box(QUESTION)], answers), broker) == {}
    assert [answer["pick"] for answer in broker.answers] == [""]


async def test_a_question_without_the_prefix_is_ignored() -> None:
    broker = StubBroker()
    lookalike = "Send this message to «Alice»? [tg-abcdefghij]"
    assert await approval_post(post_input([box(lookalike)], {lookalike: "Allow"}), broker) == {}
    assert broker.answers == []


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps({"tool_input": {"questions": "none"}}),
        json.dumps({"tool_input": {"questions": [5, {"question": 7}]}}),
    ],
)
async def test_malformed_post_input_relays_nothing_and_decides_nothing(raw: str) -> None:
    broker = StubBroker()
    assert await approval_post(raw, broker) == {}
    assert broker.answers == []


async def test_a_failed_relay_decides_nothing() -> None:
    broker = StubBroker(error=BrokerUnavailable("telegram_error", "down"))
    assert await approval_post(post_input([box(QUESTION)], {QUESTION: "Allow"}), broker) == {}


# The command, against a real broker in a subprocess ---------------------------------------


@pytest.mark.usefixtures("subprocess_brokers")
def test_the_hook_command_starts_the_broker_and_prints_one_json_answer(home: Path) -> None:
    out = io.StringIO()
    code = run_hook("approval-pre", FixedPaths.from_home(home), io.StringIO(pre_input()), out)
    assert code == 0
    verdict, reason = decision(json.loads(out.getvalue()))
    assert verdict == "deny"
    assert reason == "no_session: Not logged in: run `telegram-mcp login` in a terminal."
    post_out = io.StringIO()
    post = post_input([box(QUESTION)], {QUESTION: "Allow"})
    assert run_hook("approval-post", FixedPaths.from_home(home), io.StringIO(post), post_out) == 0
    assert json.loads(post_out.getvalue()) == {}


async def test_an_unprefixed_tool_name_is_used_as_given() -> None:
    broker = StubBroker({"decision": "allow"})
    await approval_pre(pre_input(tool_name="tg_messages_send"), broker)
    assert broker.checks == [("tg_messages_send", ARGS)]


@pytest.mark.parametrize("raw", ["[]", "5", json.dumps({"tool_response": "x"})])
async def test_a_post_payload_that_is_not_an_object_relays_nothing(raw: str) -> None:
    broker = StubBroker()
    assert await approval_post(raw, broker) == {}
    assert broker.answers == []
