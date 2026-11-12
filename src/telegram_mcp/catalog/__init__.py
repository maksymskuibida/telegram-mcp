"""The curated tool catalogue: what the broker runs for each tool beyond U1's two (C5).

One module per tool family registers its actions: chats, messages, people (members, admins,
contacts, users), profile and media (file sends and downloads). ``tools.py`` keeps every
tool's declaration and schema; the modules here hold the validators, the describers that fill
a question's fields, and the runners that make the Telegram call."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from telegram_mcp.actions import ConsentAction
from telegram_mcp.catalog import chats, media, messages, people, profile
from telegram_mcp.catalog.common import CatalogContext
from telegram_mcp.files import FileStore
from telegram_mcp.paths import FixedPaths


def build_actions(
    paths: FixedPaths, clock: Callable[[], datetime]
) -> tuple[CatalogContext, list[ConsentAction]]:
    """Every U4 action, sharing one context."""
    context = CatalogContext(paths=paths, files=FileStore(clock=clock), clock=clock)
    actions: list[ConsentAction] = []
    for family in (messages, chats, people, profile, media):
        actions.extend(family.write_actions(context))
    actions.extend([chats.mark_action(), messages.read_action()])
    for reads in (chats, messages, people, profile):
        actions.extend(reads.read_actions())
    actions.extend(media.read_actions(context))
    return context, actions
