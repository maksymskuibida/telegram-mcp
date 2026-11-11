# Development

## Requirements

- macOS (the tool supports macOS only).
- [uv](https://docs.astral.sh/uv/) 0.12 or later on `PATH`. `uv` installs Python 3.12 or later
  and the locked dependencies; nothing else is needed.
- For a real login: your own Telegram `api_id` and `api_hash` from
  [my.telegram.org](https://my.telegram.org). The package ships no app credentials.

## Run the checks

From the repository root:

```bash
uv sync --locked
uv run ruff check
uv run ruff format --check
uv run mypy --strict src tests scripts
uv run pytest --cov=telegram_mcp --cov-branch --cov-report=term-missing --cov-fail-under=90
uv run python scripts/check_constraints.py
uv run pip-audit -r constraints.txt --disable-pip --no-deps
```

CI runs the same steps on every pull request and push to `main`, plus a gitleaks scan of the
full history. The 90% coverage floor is never lowered to make a change pass.

Tests never reach Telegram. Every test uses the in-memory `FakeGateway`, and a test that makes
Telethon open a connection fails. Tests get their own temporary home folder under `/tmp`, an
in-memory keyring, and, for the processes they start, a file keyring inside that temporary
home, so no test reads or writes your real Keychain or data folder.

The confirmation logic (`consent.py`), the broker's approval path and the hooks were written
test-first, and every branch of them is covered.

## Dependencies

Direct dependencies are pinned with `==` in `pyproject.toml` and locked in `uv.lock`.
`uvx --from <source>` does not read `uv.lock`, so the plugin also ships `constraints.txt`,
exported from the lock, and runs every command as
`uvx --constraints constraints.txt --from <source> telegram-mcp …`. After changing a
dependency:

```bash
uv lock
uv export --no-hashes --no-dev --no-emit-project --format requirements-txt -o constraints.txt
uv run python scripts/check_constraints.py
```

## Install from a local checkout

The repository is itself a Claude Code plugin and a one-plugin marketplace.

For one session only, without installing anything:

```bash
claude --plugin-dir /path/to/telegram-mcp
```

To install it:

```bash
claude plugin marketplace add /path/to/telegram-mcp
claude plugin install telegram-mcp@telegram-mcp
```

Then log in once, in a terminal (not inside Claude Code):

```bash
uvx --constraints /path/to/telegram-mcp/constraints.txt --from /path/to/telegram-mcp telegram-mcp login
```

Login asks for `api_id`, `api_hash` (hidden), your phone number, the code Telegram sends, and
the two-step password (hidden) if your account has one. Before the code it says where
Telegram sent it; when Telegram offers another way, Enter with no code asks for it that way.
The session is stored in the macOS Keychain only.

## Commands

| Command | Started by | Exit codes |
|---|---|---|
| `telegram-mcp login` | you, in a terminal | 0 ok, 1 failed with a message, 2 refused (Keychain unavailable, wrong platform) |
| `telegram-mcp serve` | Claude Code (the plugin's MCP server) | |
| `telegram-mcp hook approval-pre` / `approval-post` | Claude Code, through `hooks/run` | always prints one JSON answer; `hooks/run` exits 2 when it cannot |
| `telegram-mcp broker` | the other commands, on demand | |

## Real-client checks

Some behaviour can only be checked in a real Claude Code client with a real account: the tool
name prefix, the shape of the question box's answer in the terminal and in the desktop app,
what Claude Code does when a hook times out, the Keychain prompt under `uvx`, and that only the
person can answer the question box in every permission mode. They are listed, with what has
been checked so far, in [real-client-checks.md](real-client-checks.md). Before each release, run
the manual smoke test there on your own account.
