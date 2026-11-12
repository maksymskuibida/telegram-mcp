"""The `telegram-mcp` command: login, logout, status, log, serve, broker and hook.

Every command first makes new files private (umask 077) and checks the platform. Exit codes:
0 ok, 1 failed with a message, 2 refused (wrong platform, Keychain unavailable, not logged in).
A bad `log` option exits 1 with the usage message."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import NoReturn

from telegram_mcp import __version__
from telegram_mcp.paths import FixedPaths
from telegram_mcp.platform import require_macos, secure_umask

HOOK_NAMES = ("approval-pre", "approval-post", "guard")


class _CommandParser(argparse.ArgumentParser):
    """argparse with a chosen exit code for a usage error (argparse itself always uses 2)."""

    usage_exit = 2

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(self.usage_exit, f"{self.prog}: error: {message}\n")


def _whole_number_from_1(text: str) -> int:
    if not text.isdigit() or int(text) < 1:
        raise argparse.ArgumentTypeError(f"must be a whole number of at least 1, not {text!r}")
    return int(text)


def build_parser() -> argparse.ArgumentParser:
    parser = _CommandParser(
        prog="telegram-mcp",
        description="Use your own Telegram account from Claude Code, confirming every write.",
    )
    parser.add_argument("--version", action="version", version=f"telegram-mcp {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_CommandParser)
    login = commands.add_parser("login", help="log in to Telegram with your phone or a QR code")
    login.add_argument("--qr", action="store_true", help="log in by scanning a QR code")
    logout = commands.add_parser("logout", help="log out of Telegram and delete the session")
    logout.add_argument("--yes", action="store_true", help="do not ask before logging out")
    commands.add_parser("status", help="whether you are logged in, and who uses the connection")
    log = commands.add_parser("log", help="print the activity log")
    log.usage_exit = 1
    log.add_argument("--lines", type=_whole_number_from_1, metavar="N", help="the last N only")
    log.add_argument("--follow", action="store_true", help="keep printing new entries")
    log.add_argument("--json", action="store_true", help="the stored JSON lines")
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
    command: str = args.command
    if command == "hook" and args.name == "guard":
        # The guard runs before every shell and file call: it loads only the standard library
        # and the fixed paths, never the broker client that telegram_mcp.hooks pulls in.
        from telegram_mcp.guard import run_guard

        return run_guard(paths, sys.stdin, sys.stdout)
    if command in ("login", "logout", "status", "log"):
        return _person_command(command, args, paths)
    if command == "broker":
        from telegram_mcp.broker.server import run_broker

        return run_broker(paths)
    if command == "serve":
        from telegram_mcp.mcp_server import run_server

        return run_server(paths)
    from telegram_mcp.hooks import run_hook

    return run_hook(args.name, paths)


def _person_command(command: str, args: argparse.Namespace, paths: FixedPaths) -> int:
    """The commands the person runs in their own terminal."""
    if command == "log":
        from telegram_mcp.log_reader import LogOptions, run_log

        return run_log(paths, LogOptions(lines=args.lines, follow=args.follow, as_json=args.json))
    if command == "login":
        from telegram_mcp.login import run_login_command

        return run_login_command(paths, qr=args.qr)
    from telegram_mcp.login import Terminal
    from telegram_mcp.session_commands import run_logout, run_status

    if command == "logout":
        return run_logout(paths, Terminal(), args.yes)
    return run_status(paths, Terminal())


if __name__ == "__main__":
    sys.exit(main())
