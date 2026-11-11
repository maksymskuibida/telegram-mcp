"""The `telegram-mcp` command (C7): serve, broker, login and hook.

Every command first makes new files private (umask 077) and checks the platform. Exit codes:
0 ok, 1 failed with a message, 2 refused (wrong platform, Keychain unavailable, not logged in).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from telegram_mcp import __version__
from telegram_mcp.paths import FixedPaths
from telegram_mcp.platform import require_macos, secure_umask

HOOK_NAMES = ("approval-pre", "approval-post")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telegram-mcp",
        description="Use your own Telegram account from Claude Code, confirming every write.",
    )
    parser.add_argument("--version", action="version", version=f"telegram-mcp {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="log in to Telegram with your phone number")
    commands.add_parser("serve", help="the MCP server; Claude Code starts it")
    commands.add_parser("broker", help="the background connection; started on demand")
    hook = commands.add_parser("hook", help="a Claude Code hook; Claude Code runs it")
    hook.add_argument("name", choices=HOOK_NAMES)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    secure_umask()
    require_macos()
    args = build_parser().parse_args(argv)
    paths = FixedPaths.from_home()
    if args.command == "broker":
        from telegram_mcp.broker.server import run_broker

        return run_broker(paths)
    if args.command == "serve":
        from telegram_mcp.mcp_server import run_server

        return run_server(paths)
    if args.command == "login":
        from telegram_mcp.login import run_login_command

        return run_login_command(paths)
    from telegram_mcp.hooks import run_hook

    return run_hook(args.name, paths)


if __name__ == "__main__":
    sys.exit(main())
