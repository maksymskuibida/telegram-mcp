# Real-client checks

Automated tests cover the code against a fake Telegram and an in-process MCP client. These
checks need a real Claude Code client, and some a real Telegram account. Each row records what
was actually run; anything not run is **not verified**.

The safety of a send does not rest on checks 1 to 3: with no confirmed approval, `tool.call`
always answers `needs_confirmation`. Check 6 is different. The design trusts that only the
person can answer the question box; if any client or mode can produce an Allow without the
person, sending must not be treated as safe.

| # | Check | Date | Client | Claude Code | Permission mode | Outcome |
|---|---|---|---|---|---|---|
| 1 | The prefix Claude Code gives this plugin's MCP tools, which the hook matcher and the prefix stripping depend on (expected `mcp__plugin_telegram-mcp_telegram__`) | 2026-11-11 | none (plugin not installed in a client) | 2.1.317 | n/a | **Not verified.** Seen only indirectly: this machine's Claude Code 2.1.317 names other plugins' tools `mcp__plugin_<plugin>_<server>__<tool>`, and `claude plugin validate` accepts this plugin with no errors. |
| 2 | The shape of the question box's pick in the PostToolUse input, in the terminal and in the desktop app, and that the question text reaches the hook unchanged | 2026-11-11 | none through this hook | 2.1.317 | n/a | **Not verified.** Seen only indirectly: AskUserQuestion results recorded by this machine's Claude Code hold `{questions, answers: {<question text>: <label>}}`, which is what the hook reads. A dismissed box records a non-label answer, which declines. |
| 3 | What Claude Code does when the PreToolUse hook passes its 20 s timeout, and the cold `uvx` start of a no-op hook | 2026-11-11 | none (launcher run from a terminal) | n/a | n/a | **Timeout behaviour not verified.** Start time measured outside Claude Code with `hooks/run approval-post` and `{}` on standard input: 4.86 s with an empty uv cache (Python already installed), 0.44 to 0.47 s warm. |
| 4 | One broker connection per MCP server session against one per call | 2026-11-11 | automated test (in-process MCP client) | n/a | n/a | **Verified by automated test, not in a real client.** The server keeps one connection for the session; median 0.16 ms per call over 30 calls on the fake gateway. |
| 5 | Whether a later `uvx` run gets a macOS prompt when it reads the Keychain items | | | | | **Not verified** (needs a real login). |
| 6 | Only the person can answer the question box: in the terminal and the desktop app, in the default, acceptEdits and bypassPermissions modes the box waits for the person, and a `claude -p` run cannot produce an Allow for a `[tg-` question | | | | | **Not verified.** Until it is, this version is for trying out only. |
| 7 | Cold start of the `approval-pre` hook within its 20 s timeout (first call after install), and that a hook made to sleep past 20 s never lets a send run | | | | | **Not verified in Claude Code.** See check 3 for the launcher's measured start time. |
| 8 | The versions installed by `uvx` equal the lock | 2026-11-11 | none (the plugin's `uvx` command run from a terminal) | n/a | n/a | **Verified outside Claude Code.** `uvx --constraints constraints.txt --from .` with an empty uv cache: 42 pinned packages, none different from `constraints.txt`, none missing, none extra. |

## Manual smoke test (before each release)

On your own account, in a real Claude Code session with the plugin installed:

1. `telegram-mcp login` in a terminal; it prints `Logged in as …`.
2. Ask Claude to list your chats. Time it with the broker already running (target 5 s) and from
   a cold start (target 10 s).
3. Ask Claude to send a message to yourself (Saved Messages) by chat id. Check that the
   question box shows the chat and the full text, pick **Deny**, and check nothing was sent.
4. Ask again, pick **Allow**, and check exactly one message arrives.
5. Read `~/Library/Application Support/telegram-mcp/activity.log` and check the call, the
   question, both decisions and the result are there, with no session or api_hash in it.
