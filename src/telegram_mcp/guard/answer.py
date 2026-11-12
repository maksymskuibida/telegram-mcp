"""The guard's refusal text (BR4.2, BR4.6, NFR1.1).

A reason says what was refused and why, in words Claude can act on, then what to do instead.
It quotes at most 200 characters of Claude's own call (120 of the call and 80 of the part that
matched) and never anything else: the guard reads no file, so it has no content to leak."""

from __future__ import annotations

import re

from telegram_mcp.guard.model import GuardDecision, Route

PREFIX = "telegram-mcp guard: refused"
CALL_QUOTE = 120
MATCH_QUOTE = 80
OWN_TERMINAL = "The person runs it in their own terminal."
READ_THE_LOG = "The person reads it with telegram-mcp log in their own terminal."
WRITE_IN_FULL = "Write the path out in full."
LOG_RE = re.compile(r"activity\.log|telegram-mcp\S*\s+log\b", re.IGNORECASE)

WHY = {
    Route.DATA_PATH: "it reaches telegram-mcp's private data folder",
    Route.DATA_TEXT: "it names telegram-mcp's private data folder or one of its files",
    Route.UNDECIDABLE: "the guard could not tell where a word in it points",
    Route.KEYCHAIN: "it could read or change telegram-mcp's Keychain items",
    Route.OWN_COMMAND: "telegram-mcp's own commands and code are run by the person, not by Claude",
    Route.UNPARSABLE: "the guard could not read the command",
    Route.UNREADABLE_CALL: "the guard could not read the call",
}
WHAT = {
    "Bash": "the shell command",
    "Read": "reading",
    "Write": "writing",
    "Edit": "editing",
    "MultiEdit": "editing",
    "NotebookEdit": "editing",
    "Glob": "the file search",
    "Grep": "the text search",
    "WebFetch": "fetching",
    "Task": "the subagent prompt",
    "Agent": "the subagent prompt",
}


def quote(text: str, limit: int) -> str:
    """One line, no backticks, at most ``limit`` characters."""
    clean = "".join(" " if not c.isprintable() else c for c in text).replace("`", "'")
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _subject(tool: str, what: str) -> str:
    action = WHAT.get(tool, "the call")
    return f"{action} `{quote(what, CALL_QUOTE)}`" if what else action


def _advice(route: Route, what: str, matched: str) -> str:
    if route in (Route.OWN_COMMAND, Route.DATA_PATH, Route.DATA_TEXT) and (
        LOG_RE.search(matched) or LOG_RE.search(what)
    ):
        return " " + READ_THE_LOG
    if route == Route.OWN_COMMAND:
        return " " + OWN_TERMINAL
    if route in (Route.UNDECIDABLE, Route.UNPARSABLE):
        return " " + WRITE_IN_FULL
    return ""


def refusal(route: Route, tool: str, what: str, matched: str) -> GuardDecision:
    reason = f"{PREFIX} {_subject(tool, what)} because {WHY[route]}"
    if matched and matched != what:
        reason += f" (`{quote(matched, MATCH_QUOTE)}`)"
    reason += "." + _advice(route, what, matched)
    return GuardDecision("refuse", route, matched, reason)


def failure(tool: str, what: str, error: BaseException) -> GuardDecision:
    """Any failure while deciding refuses; the reason names only the kind of failure."""
    kind = type(error).__name__
    reason = f"{PREFIX} {_subject(tool, what)} because the guard failed while checking it ({kind})."
    return GuardDecision("refuse", Route.GUARD_ERROR, kind, reason)
