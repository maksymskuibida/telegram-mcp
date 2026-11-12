"""Draw a QR login link in the terminal (login --qr).

Two module rows per text line with half-block characters, dark modules drawn as blocks, a
2-module quiet zone. The link itself is printed under the code, so a terminal font that breaks
the drawing still leaves a way to log in. Nothing here touches the network or a file: the link
is a login secret and goes to the terminal only."""

from __future__ import annotations

import segno

QUIET_ZONE = 2
SCAN_HINT = (
    "On your phone, open Telegram → Settings → Devices → Link Desktop Device and scan this code."
)
_GLYPHS = {(False, False): " ", (True, False): "▀", (False, True): "▄", (True, True): "█"}


def modules(url: str) -> list[list[bool]]:
    """The code's modules, quiet zone included; True is a dark module."""
    code = segno.make_qr(url, error="l")
    return [[bool(cell) for cell in row] for row in code.matrix_iter(scale=1, border=QUIET_ZONE)]


def draw(url: str) -> list[str]:
    """The lines to print for one token: the code, the link, and where to scan it."""
    rows = modules(url)
    if len(rows) % 2:
        rows.append([False] * len(rows[0]))
    picture = [
        "".join(_GLYPHS[(top, bottom)] for top, bottom in zip(upper, lower, strict=True))
        for upper, lower in zip(rows[0::2], rows[1::2], strict=True)
    ]
    return [*picture, url, SCAN_HINT]
