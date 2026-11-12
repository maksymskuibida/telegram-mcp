"""Drawing the QR login link in the terminal."""

from __future__ import annotations

import base64
import time

import pytest

from telegram_mcp.qr import QUIET_ZONE, SCAN_HINT, draw, modules

# A token of Telegram's length (32 bytes), as Telethon turns it into a link.
TOKEN = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
URL = f"tg://login?token={TOKEN}"
_TOP = {" ": False, "▀": True, "▄": False, "█": True}
_BOTTOM = {" ": False, "▀": False, "▄": True, "█": True}


def picture_of(lines: list[str]) -> list[str]:
    return lines[:-2]


def test_the_same_link_draws_the_same_lines_every_time() -> None:
    assert draw(URL) == draw(URL)
    assert draw(URL) != draw(URL + "x")


def test_the_link_and_where_to_scan_follow_the_code() -> None:
    lines = draw(URL)
    assert lines[-2:] == [URL, SCAN_HINT]
    assert set("".join(picture_of(lines))) <= set(_TOP)


def test_a_telegram_length_link_fits_an_80_column_terminal() -> None:
    picture = picture_of(draw(URL))
    width = len(picture[0])
    assert all(len(line) == width for line in picture)
    assert width <= 80
    assert len(picture) <= 25


def test_the_drawing_decodes_back_to_the_code_modules() -> None:
    expected = modules(URL)
    decoded: list[list[bool]] = []
    for line in picture_of(draw(URL)):
        decoded.append([_TOP[glyph] for glyph in line])
        decoded.append([_BOTTOM[glyph] for glyph in line])
    assert decoded[: len(expected)] == expected
    assert not any(any(row) for row in decoded[len(expected) :])


def test_the_quiet_zone_is_two_modules_wide() -> None:
    rows = modules(URL)
    for row in rows[:QUIET_ZONE] + rows[-QUIET_ZONE:]:
        assert not any(row)
    for row in rows:
        assert not any(row[:QUIET_ZONE])
        assert not any(row[-QUIET_ZONE:])
    assert any(rows[QUIET_ZONE])


@pytest.mark.parametrize("repeat", range(3))
def test_drawing_is_fast_and_needs_no_network(repeat: int) -> None:
    started = time.monotonic()
    draw(f"{URL}{repeat}")
    assert time.monotonic() - started < 1.0
