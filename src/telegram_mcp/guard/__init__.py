"""The GuardHook (`telegram-mcp hook guard`; C6, FR11.1 to FR11.3).

Claude Code runs it before Claude's own shell, file, search, fetch and subagent calls. It keeps
them away from the tool's data folder, its Keychain items and its own commands, and refuses
whatever it cannot decide. A pass is the empty JSON object, so Claude Code's usual permission
prompts still apply. The guard reads no file, writes nothing and loads only the standard
library and the tool's fixed paths."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import TextIO

from telegram_mcp.guard import answer, rules
from telegram_mcp.guard.model import PASS, GuardDecision, Refusal, Route
from telegram_mcp.paths import FixedPaths

__all__ = ["GuardDecision", "Route", "decide", "run_guard"]


def decide(call: object, paths: FixedPaths, environ: Mapping[str, str]) -> GuardDecision:
    """Decide one call, as parsed from the hook's standard input. Never raises."""
    tool = ""
    what = ""
    try:
        guard_call = rules.read_call(call)
        tool = guard_call.tool_name
        if tool not in rules.WATCHED_TOOLS:
            return PASS
        what = rules.summary(guard_call)
        context = rules.Context.build(paths, environ, guard_call)
        getattr(rules, rules.RULES[tool])(guard_call, context)
        return PASS
    except Refusal as refused:
        return answer.refusal(refused.route, tool, what, refused.matched)
    except Exception as error:
        return answer.failure(tool, what, error)


def run_guard(
    paths: FixedPaths,
    stdin: TextIO,
    stdout: TextIO,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Read one call from ``stdin`` and print one JSON answer. Always exits 0: a refusal is
    an answer, and any failure here is answered as a refusal too (BR4.2)."""
    try:
        try:
            call: object = json.loads(stdin.read())
        except ValueError:
            call = None
        result = decide(call, paths, os.environ if environ is None else environ).answer()
    except Exception as error:
        result = answer.failure("", "", error).answer()
    stdout.write(json.dumps(result) + "\n")
    stdout.flush()
    return 0
