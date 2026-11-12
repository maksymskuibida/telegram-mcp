"""The part-by-part wildcard matcher (BR1.5).

A pattern is refused only when it can reach the data folder by naming it. ``*``, ``?`` and
``[…]`` match within one folder part; ``**`` matches any number of parts. With every ``**``
taken as matching no part, a pattern that can match the data folder or a path inside it is
refused. A pattern whose parts after the last ``**`` can match the end of a protected path,
from a part that names the data folder's own ``telegram-mcp`` part onwards, is refused too.
A ``**`` that merely crosses the data folder on the way down is a search above it and passes,
and so does a part that is wildcards only, which names nothing."""

from __future__ import annotations

import posixpath
from fnmatch import fnmatchcase

GLOBSTAR = "**"
MAX_BRACE_RESULTS = 256


def split_parts(path: str) -> list[str]:
    return [part for part in posixpath.normpath(path).casefold().split("/") if part]


def _matches(pattern: str, name: str) -> bool:
    return fnmatchcase(name, pattern)


def _names_something(pattern: str) -> bool:
    return any(char not in "*?" for char in pattern)


def can_reach(pattern: str, data_dir: str) -> bool:
    """True when the absolute wildcard ``pattern`` names its way into ``data_dir``."""
    parts = split_parts(pattern)
    protected = split_parts(data_dir)
    plain = [part for part in parts if part != GLOBSTAR]
    if len(plain) >= len(protected) and all(
        _matches(plain[i], protected[i]) for i in range(len(protected))
    ):
        return True
    if GLOBSTAR not in parts:
        return False
    last = len(parts) - 1 - parts[::-1].index(GLOBSTAR)
    tail = parts[last + 1 :]
    for start in range(len(protected)):
        span = len(protected) - start
        if len(tail) < span:
            continue
        if all(_matches(tail[j], protected[start + j]) for j in range(span)) and (
            _names_something(tail[span - 1])
        ):
            return True
    return False


def expand_braces(pattern: str) -> list[str]:
    """Brace alternatives in a search tool's pattern (``{a,b}``), up to 256 results.

    Raises ValueError above that."""
    results = [pattern]
    done: list[str] = []
    while results:
        current = results.pop()
        found = _first_brace(current)
        if found is None:
            done.append(current)
        else:
            start, end, alternatives = found
            for alternative in alternatives:
                results.append(current[:start] + alternative + current[end + 1 :])
        if len(done) + len(results) > MAX_BRACE_RESULTS:
            raise ValueError("more than 256 brace results")
    return done


def _first_brace(text: str) -> tuple[int, int, list[str]] | None:
    for start, char in enumerate(text):
        if char != "{":
            continue
        depth = 0
        commas: list[int] = []
        for index in range(start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    if commas:
                        edges = [start, *commas, index]
                        return (
                            start,
                            index,
                            [text[edges[i] + 1 : edges[i + 1]] for i in range(len(edges) - 1)],
                        )
                    break
            elif text[index] == "," and depth == 1:
                commas.append(index)
    return None
