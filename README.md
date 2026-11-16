# telegram-mcp

Use your own Telegram account from Claude Code. Claude can read your chats, search messages,
look up people and download files freely. Every action that changes anything — sending,
editing, deleting, forwarding, reacting, pinning, creating or leaving chats, invite links,
admin rights, your profile, file sends — and every marking as read runs only after **you**
pick **Allow** in Claude Code's question box. Each Allow runs exactly one action.

Everything runs locally on your Mac. Your Telegram session is kept in the macOS Keychain and
never leaves the machine except to talk to Telegram.

> This version is for trying out, not for daily use. Some behaviour is only checked by
> automated tests so far; see [What is not verified yet](#what-is-not-verified-yet).

## Contents

- [Requirements](#requirements)
- [Install](#install)
- [Log in](#log-in)
- [Using it](#using-it)
- [Folder settings](#folder-settings)
- [Status and the activity log](#status-and-the-activity-log)
- [Log out](#log-out)
- [Update or roll back](#update-or-roll-back)
- [Uninstall](#uninstall)
- [The guard, and what it does not cover](#the-guard-and-what-it-does-not-cover)
- [What is not verified yet](#what-is-not-verified-yet)
- [Troubleshooting](#troubleshooting)

## Requirements

- **macOS.** Other systems are refused.
- **Claude Code**, in the terminal or in the desktop app's Code tab, with its `claude`
  command line on your `PATH` ([install Claude Code](https://code.claude.com/docs/en/setup)).
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)**, which brings `uv` and
  `uvx`. uv installs Python 3.12 and the exact pinned packages the first time the tool runs;
  nothing else is needed.
- **Your own Telegram `api_id` and `api_hash`**, from
  [my.telegram.org](https://my.telegram.org) → API development tools. The tool ships no app
  credentials.

## Install

Run this in your terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/maksymskuibida/telegram-mcp/v0.1.0/install.sh | sh
```

To install a specific release tag instead:

```bash
curl -fsSL https://raw.githubusercontent.com/maksymskuibida/telegram-mcp/v0.1.0/install.sh | sh -s -- --tag vX.Y.Z
```

What the script does, in order:

1. **Checks**, and changes nothing if one fails: the machine is a Mac; `uv`, `uvx` and
   `claude` are on your `PATH` (it names whatever is missing and where to get it); the tag has
   the form `vMAJOR.MINOR.PATCH`.
2. **Reads** what Claude Code has installed (`claude plugin marketplace list --json` and
   `claude plugin list --json`).
   - Already installed at this tag: it says so and changes nothing.
   - Installed at another tag: it first checks with `git ls-remote` that the tag exists on
     GitHub (if it does not, or GitHub cannot be reached, it refuses and changes nothing), then
     removes that copy and installs this one, so the MCP server and the hooks move together.
   - A marketplace named `telegram-mcp` added from somewhere else (a local folder, another
     repository): it refuses and changes nothing; run `--uninstall` first.
3. **Installs** the plugin for your user, pinned to the tag:
   `claude plugin install telegram-mcp --marketplace maksymskuibida/telegram-mcp#<tag> --scope user`,
   then checks that Claude Code reports exactly that tag.
4. **Prepares the guard hook** by running it once, so uv has every package cached before
   Claude Code's first call. If this fails you get a warning only; the plugin is installed.
5. **Prints the next steps**, including an alias for the `telegram-mcp` command.

The script exits 0 when it is done or had nothing to do, 1 when a step failed (the message
says which, and how to get back to where you were), and 2 when it refused.

The plugin registers its MCP server, named `telegram`, and its hooks by itself: there is no
MCP configuration to write. Start a new Claude Code session and run `/mcp`; `telegram` is
listed with its tools (they appear to Claude as
`mcp__plugin_telegram-mcp_telegram__tg_…`). `/hooks` lists the plugin's three hooks.

### Without the script

The script runs only documented Claude Code commands. To do the same by hand:

```bash
claude plugin install telegram-mcp --marketplace maksymskuibida/telegram-mcp#v0.1.0 --scope user
```

This skips the checks and the guard hook's first run, so the first call in Claude Code may
take several seconds while uv downloads its packages.

### The `telegram-mcp` command

Login, logout, status and the log command run in **your own terminal, never inside Claude
Code**. The install script prints a line like this one, with your plugin folder filled in; add
it to `~/.zshrc`:

```bash
alias telegram-mcp='uvx --quiet --constraints "<plugin folder>/constraints.txt" --from "<plugin folder>" telegram-mcp'
```

It runs the exact version Claude Code uses, with the pinned packages. The plugin folder
changes when you move to another tag; the script prints the new line each time.

## Log in

```bash
telegram-mcp login        # asks: phone number or QR code
telegram-mcp login --qr   # QR code straight away
```

- **Phone:** it asks for `api_id`, `api_hash` (hidden), your phone number, the code Telegram
  sends, and your two-step password (hidden) if you have one. Before the code it says where
  Telegram sent it; when Telegram offers another way, pressing Enter with no code asks for it
  that way.
- **QR code:** it asks for `api_id` and `api_hash`, then draws a QR code in the terminal. Scan
  it in Telegram on your phone: Settings → Devices → Link Desktop Device. Codes refresh by
  themselves; login gives up after three minutes.

The session, `api_id` and `api_hash` are stored as three items of the Keychain service
`telegram-mcp`, and nowhere else. Login exits 0 when you are logged in (or nothing changed),
1 when it failed and 2 when the Keychain is unavailable.

## Using it

Ask Claude in plain words: "list my unread chats", "what did Anna write yesterday", "send
'running late' to my Saved Messages". Reads run straight away and never mark anything as read.

For anything that changes something, the first call never acts. Claude shows you a question
box such as:

```
Send this message to your Saved Messages (id 123456789)?
running late
```

with two choices:

- **Allow** — the action runs once, exactly as shown. Calling again asks again.
- **Deny** — nothing happens. For 60 seconds the same call is refused without a new question,
  unless you tell Claude in the chat to try again.

An unanswered question expires after two minutes, and an Allow must be used within one
minute. Some actions ask a choice first (delete for everyone or only for you, group or
channel). The full list of questions and rules is in
[docs/confirmation-rules.md](docs/confirmation-rules.md).

## Folder settings

The tool keeps its own files in `~/Library/Application Support/telegram-mcp/` (the data
folder, mode 0700). Two settings choose where files go; put them in
`~/Library/Application Support/telegram-mcp/config.toml`:

```toml
outbox_dir = "~/Downloads/telegram-mcp/outbox"   # files Claude may send (default)
downloads_dir = "~/Downloads/telegram-mcp"       # where downloads are saved (default)
```

- Only a file inside `outbox_dir` can be sent, at most 20 MB. Put a file there yourself, then
  ask Claude to send it.
- Downloads go into `downloads_dir` only, never replacing a file, at most 20 MB.
- Each value is an absolute path or starts with `~`, lies inside your home folder, contains no
  link, and does not overlap the data folder. A refused `outbox_dir` stops file sends only; a
  refused `downloads_dir` stops downloads only. Missing folders are created with mode 0700.

## Status and the activity log

```bash
telegram-mcp status
```

prints whether you are logged in (and as whom), whether the background connection is running,
and how many Claude sessions use it. It exits 0 when logged in, 1 when Telegram could not be
reached and 2 when not logged in.

Every tool call, question, decision and result is written to the activity log,
`~/Library/Application Support/telegram-mcp/activity.log` (mode 0600). Secrets are never
written to it. When it would pass 10 MB it is rotated to `activity.log.1`, and older files move
up to `activity.log.4`; the oldest is deleted.

```bash
telegram-mcp log                # every entry, oldest first
telegram-mcp log --lines 50     # the last 50
telegram-mcp log --follow       # keep printing new entries (Ctrl-C to stop)
telegram-mcp log --json         # one JSON object per line
```

The log command only reads the files. To delete the log:

```bash
rm ~/Library/Application\ Support/telegram-mcp/activity.log*
```

## Log out

```bash
telegram-mcp logout          # asks "Log out of Telegram as …? [y/N]"
telegram-mcp logout --yes
```

It ends the session at Telegram and deletes the three Keychain items. If Telegram does not
answer, this Mac is still logged out, and it tells you how to end the session from another
device (Settings → Devices).

## Update or roll back

A release is a git tag `vX.Y.Z`. To move to another release, newer or older, run that
release's own install script (replace both `vX.Y.Z` with the tag):

```bash
curl -fsSL https://raw.githubusercontent.com/maksymskuibida/telegram-mcp/vX.Y.Z/install.sh | sh -s -- --tag vX.Y.Z
```

The script removes the installed copy and installs the tag you named; your login, settings and
log stay. Rolling back is the same command with the previous tag. Update the `telegram-mcp`
alias with the line the script prints, and start a new Claude Code session.

## Uninstall

1. Log out first, so the session is removed from the Keychain and ended at Telegram:
   `telegram-mcp logout`
2. Remove the plugin, its MCP server and its hooks:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/maksymskuibida/telegram-mcp/v0.1.0/install.sh | sh -s -- --uninstall
   ```

3. Optionally delete the data folder (settings and activity log):
   `rm -rf ~/Library/Application\ Support/telegram-mcp`
4. Remove the `telegram-mcp` alias from `~/.zshrc`.

If you skipped step 1, the uninstall tells you how to delete the three Keychain items with
`security delete-generic-password`; end the session in Telegram under Settings → Devices.

## The guard, and what it does not cover

The plugin also installs a **guard hook** that runs before Claude's own shell, file, search,
fetch and subagent calls. It refuses the ones that reach the data folder, the Keychain items
or the tool's own commands (`login`, `logout`, `log`, `broker` and the rest), and tells Claude
to ask you to run them in your own terminal. It passes everything else untouched, so your
usual permission prompts still apply.

**This guard and the question box are best effort.** Claude can run programs as your macOS
user. The guard blocks the routes listed above, not every conceivable one. These are **not
covered**:

- Searches and copies started above the data folder, such as `grep -r … ~` or
  `find ~ -name '*.log'`, and a search pattern whose `**` merely crosses it.
- The tool's command name built while code runs (`'te' + 'legram-mcp'`, a name read from a
  file or a variable); only names written out whole are seen.
- The plugin's own files and Claude Code's settings files, which could switch the hooks off.
- Scripts written to a file first and run later: file contents are not checked.
- Programs outside the guard's list of programs that open files (for example `jq`, `vim`,
  `git -C`), and text piped into a shell (`curl … | sh`).
- Other programs running as your user, which can read the data folder directly.
- A guard that does not answer within its 10-second limit, if Claude Code then runs the call.

Keep an eye on what Claude runs, and do not leave it unattended with this plugin installed.

**If the guard blocks wrongly or cannot start**, fix it from your own terminal, outside
Claude: run the install command again, or remove the plugin with `--uninstall` (or
`claude plugin uninstall telegram-mcp@telegram-mcp`) and install it again.

## What is not verified yet

Some behaviour can only be checked in a real Claude Code client with a real account: the tool
name prefix, the question box's answer in the terminal and in the desktop app, what Claude
Code does when a hook times out, the Keychain prompt under `uvx`, the install and uninstall on
a real machine, and that only you can answer the question box in every permission mode. Each
is listed, with what has been checked so far, in
[docs/real-client-checks.md](docs/real-client-checks.md).

## Troubleshooting

- **The first call is slow or blocked right after installing, with no network.** uv needs to
  download Python and the pinned packages once. Connect, then run the install command again
  (it prepares the guard hook) or any `telegram-mcp` command.
- **macOS asks whether `python` may use the Keychain items.** The items were written by an
  earlier run of the tool, possibly under another Python that uv installed. Choose
  **Always Allow** for the tool to use them; with **Deny** the tool reports the Keychain as
  unavailable. (Whether and when this prompt appears is not verified yet.)
- **"Another login is already running in a terminal."** (from `login`) or **"A login is
  running in another terminal."** (from `status` and `logout`). A `telegram-mcp login` is still
  open somewhere. Finish or cancel it (Ctrl-C), then try again.
- **`telegram-mcp` fails with a path error after an update or an uninstall.** The alias in
  `~/.zshrc` still names the old plugin folder. Replace it with the line the install script
  printed for the new tag (or remove it after an uninstall).
- **The install stops while cloning from GitHub** on a Mac with no GitHub SSH key. Claude Code
  may try SSH first; tell git to use HTTPS for GitHub, then run the install again:
  `git config --global url."https://github.com/".insteadOf git@github.com:` (whether this is
  needed is not verified yet).
- **Two Claude Code sessions at once** (say, the terminal and the desktop app) share one
  Telegram connection through one background process; you do not need to close either. The
  background process stops by itself after 10 minutes without calls.
- **Tools answer `no_session`.** You are not logged in, or the session was ended from another
  device: run `telegram-mcp login`.
- **The install script refuses because of another marketplace named `telegram-mcp`.** You
  installed it from a local folder or another repository. Run the script with `--uninstall`,
  then install again.

Contributors: see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
