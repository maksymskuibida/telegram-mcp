#!/bin/sh
# Installs, moves or removes the telegram-mcp plugin for Claude Code, at one release tag.
#
#   curl -fsSL https://raw.githubusercontent.com/maksymskuibida/telegram-mcp/v0.1.0/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/maksymskuibida/telegram-mcp/v0.1.0/install.sh | sh -s -- --uninstall
#   sh install.sh [--tag vX.Y.Z] [--uninstall] [--help]
#
# It changes nothing until every check has passed. It only uses the documented `claude plugin`
# commands, and reads their JSON with `osascript -l JavaScript`, which every Mac has.
#
# Exit codes: 0 done or nothing to change; 1 failed (the message says what and how far it got);
# 2 refused (not macOS, a missing program, a bad argument, an install from another source).

set -eu

# The tag this copy of the script installs; it always equals "v" plus the plugin's version.
DEFAULT_TAG="v0.1.0"
REPO="maksymskuibida/telegram-mcp"
MARKETPLACE="telegram-mcp"
PLUGIN_ID="telegram-mcp@telegram-mcp"
WARMUP_SECONDS=120
UV_INSTALL_URL="https://docs.astral.sh/uv/getting-started/installation/"
CLAUDE_INSTALL_URL="https://code.claude.com/docs/en/setup"
DATA_DIR="${HOME:-~}/Library/Application Support/telegram-mcp"

# Reads the two lists `claude plugin … --json` prints and answers one `key=value` per line:
# kind (none, ours or other), ref, where, installed (0 or 1), path, version.
STATE_SCRIPT='
function run(argv) {
  var markets = JSON.parse(argv[0]);
  var plugins = JSON.parse(argv[1]);
  var repo = argv[2].toLowerCase();
  if (!Array.isArray(markets) || !Array.isArray(plugins)) {
    throw new Error("expected two JSON lists");
  }
  var line = function (value) {
    return String(value === undefined || value === null ? "" : value).replace(/[\r\n]+/g, " ");
  };
  var kind = "none", ref = "", where = "";
  markets.forEach(function (m) {
    if (!m || m.name !== "telegram-mcp") { return; }
    if (m.source === "github" && String(m.repo || "").toLowerCase() === repo) {
      kind = "ours"; ref = m.ref || ""; where = m.repo;
    } else {
      kind = "other"; where = m.path || m.url || m.repo || m.source || "an unknown source";
    }
  });
  var installed = "0", path = "", version = "";
  plugins.forEach(function (p) {
    if (p && p.id === "telegram-mcp@telegram-mcp" && p.scope === "user") {
      installed = "1"; path = p.installPath || ""; version = p.version || "";
    }
  });
  return ["kind=" + kind, "ref=" + line(ref), "where=" + line(where),
          "installed=" + installed, "path=" + line(path), "version=" + line(version)].join("\n");
}
'

say() {
    printf '%s\n' "$*"
}

warn() {
    printf 'telegram-mcp install: warning: %s\n' "$*" >&2
}

fail() {
    printf 'telegram-mcp install: %s\n' "$*" >&2
    exit 1
}

refuse() {
    printf 'telegram-mcp install: %s\n' "$*" >&2
    exit 2
}

usage() {
    cat <<EOF
Usage: install.sh [--tag vX.Y.Z] [--uninstall] [--help]

Installs the telegram-mcp plugin for Claude Code (user scope) at a release tag, from
https://github.com/$REPO. Without --tag it installs $DEFAULT_TAG.

  --tag vX.Y.Z   install this release tag instead; moves an installed copy to it
  --uninstall    remove the plugin and its marketplace from Claude Code
  --help         show this text

Running it again for the installed tag changes nothing. It needs macOS, uv and the Claude Code
command line (claude). Exit codes: 0 done, 1 failed, 2 refused.
EOF
}

has() {
    command -v "$1" >/dev/null 2>&1
}

check_macos() {
    system=$(uname -s 2>/dev/null || true)
    [ "$system" = "Darwin" ] ||
        refuse "telegram-mcp runs on macOS only; this machine reports '${system:-an unknown system}'. Nothing was changed."
}

check_programs() {
    missing=""
    for program in "$@"; do
        has "$program" || missing="$missing $program"
    done
    [ -n "$missing" ] || return 0
    for program in $missing; do
        case "$program" in
            uv | uvx) printf 'telegram-mcp install: %s was not found. Install uv (it brings uv and uvx): %s\n' \
                "$program" "$UV_INSTALL_URL" >&2 ;;
            claude) printf 'telegram-mcp install: the Claude Code command line (claude) was not found. Install Claude Code: %s\n' \
                "$CLAUDE_INSTALL_URL" >&2 ;;
        esac
    done
    refuse "nothing was installed. Run this script again once the missing programs are on your PATH."
}

is_tag() {
    printf '%s\n' "$1" | grep -Eq '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'
}

# Runs one `claude` command with no standard input (under `curl | sh` it is the script itself).
claude_step() {
    claude "$@" </dev/null || {
        step_status=$?
        return "$step_status"
    }
}

read_state() {
    markets=$(claude plugin marketplace list --json </dev/null) ||
        fail "'claude plugin marketplace list --json' failed (exit $?)."
    plugins=$(claude plugin list --json </dev/null) ||
        fail "'claude plugin list --json' failed (exit $?)."
    state=$(osascript -l JavaScript -e "$STATE_SCRIPT" "$markets" "$plugins" "$REPO" 2>&1) ||
        fail "could not read Claude Code's plugin lists: $state"
    mp_kind=$(state_field kind)
    mp_ref=$(state_field ref)
    mp_where=$(state_field where)
    plugin_installed=$(state_field installed)
    install_path=$(state_field path)
}

state_field() {
    printf '%s\n' "$state" | sed -n "s/^$1=//p"
}

reinstall_hint() {
    if [ -n "$1" ] && is_tag "$1"; then
        say "curl -fsSL https://raw.githubusercontent.com/$REPO/$1/install.sh | sh"
    else
        say "sh install.sh --tag <the version you had>"
    fi
}

# Called when a step fails after the previous version was removed: say so, and how to get it back.
fail_after_removal() {
    {
        printf 'telegram-mcp install: %s\n' "$1"
        printf 'The previous version (%s) was removed and %s is not installed. To reinstall it, run:\n' \
            "${previous:-unknown}" "$tag"
        printf '  %s\n' "$(reinstall_hint "$previous")"
    } >&2
    exit 1
}

remove_previous() {
    if [ "$plugin_installed" = "1" ]; then
        claude_step plugin uninstall "$PLUGIN_ID" --scope user ||
            fail "'claude plugin uninstall $PLUGIN_ID --scope user' failed (exit $step_status); nothing was moved."
        removed=1
    fi
    if [ "$mp_kind" = "ours" ]; then
        claude_step plugin marketplace remove "$MARKETPLACE" || {
            message="'claude plugin marketplace remove $MARKETPLACE' failed (exit $step_status)."
            [ "$removed" = "1" ] && fail_after_removal "$message"
            fail "$message Nothing was moved."
        }
        removed=1
    fi
}

check_installed() {
    read_state
    [ "$mp_kind" = "ours" ] && [ "$mp_ref" = "$tag" ] && [ "$plugin_installed" = "1" ] && return 0
    message="Claude Code reported the install as done, but telegram-mcp is not installed at $tag"
    message="$message (marketplace ${mp_kind}, tag '${mp_ref:-none}', plugin installed: $plugin_installed)."
    [ "$removed" = "1" ] && fail_after_removal "$message"
    fail "$message"
}

# Runs a command with a time limit; macOS has no `timeout`. Answers the command's exit status.
run_limited() {
    limit=$1
    shift
    "$@" &
    job=$!
    (
        n=0
        while [ "$n" -lt "$limit" ] && kill -0 "$job" 2>/dev/null; do
            sleep 1
            n=$((n + 1))
        done
        kill "$job" 2>/dev/null
    ) >/dev/null 2>&1 &
    watcher=$!
    job_status=0
    wait "$job" || job_status=$?
    kill "$watcher" >/dev/null 2>&1 || true
    return "$job_status"
}

run_guard_once() {
    printf '{}' | CLAUDE_PLUGIN_ROOT="$install_path" sh "$install_path/hooks/run" guard >/dev/null
}

# Runs the guard hook once so uv has every package cached before Claude Code's first call.
warm_up_guard() {
    if [ -z "$install_path" ] || [ ! -f "$install_path/hooks/run" ]; then
        warn "the plugin folder was not found, so the guard hook was not prepared; the first call in Claude Code may be slow."
        return 0
    fi
    say "Preparing the guard hook (the first run downloads the Python packages; this can take a minute)..."
    if run_limited "$WARMUP_SECONDS" run_guard_once; then
        say "The guard hook is ready."
    else
        warn "the guard hook did not finish its first run (exit $job_status). The plugin is installed; the first call in Claude Code may be slow, or blocked once until uv has its packages."
    fi
}

print_next_steps() {
    say ""
    say "Next, in your own terminal (not inside Claude Code):"
    say "  1. Make the telegram-mcp command available, for example by adding this line to ~/.zshrc:"
    say "     alias telegram-mcp='uvx --quiet --constraints \"$install_path/constraints.txt\" --from \"$install_path\" telegram-mcp'"
    say "  2. Log in once: telegram-mcp login   (or: telegram-mcp login --qr)"
    say "  3. Start a new Claude Code session and run /mcp: the server 'telegram' is listed."
}

install() {
    is_tag "$tag" || refuse "'$tag' is not a release tag; use the form vMAJOR.MINOR.PATCH, for example $DEFAULT_TAG."
    read_state
    if [ "$mp_kind" = "other" ]; then
        refuse "a marketplace named $MARKETPLACE is already added from $mp_where. Nothing was changed. Remove it first with: sh install.sh --uninstall"
    fi
    if [ "$mp_kind" = "ours" ] && [ "$mp_ref" = "$tag" ] && [ "$plugin_installed" = "1" ]; then
        say "telegram-mcp $tag is already installed; nothing changed."
        exit 0
    fi
    removed=0
    previous=""
    if [ "$mp_kind" = "ours" ] && [ "$mp_ref" = "$tag" ]; then
        say "The telegram-mcp marketplace is at $tag but the plugin is missing; installing it."
    else
        [ "$mp_kind" = "ours" ] && previous=$mp_ref
        [ -n "$previous" ] && say "Moving telegram-mcp from $previous to $tag."
        remove_previous
    fi
    claude_step plugin install telegram-mcp --marketplace "$REPO#$tag" --scope user || {
        message="'claude plugin install telegram-mcp --marketplace $REPO#$tag --scope user' failed (exit $step_status)."
        [ "$removed" = "1" ] && fail_after_removal "$message"
        fail "$message"
    }
    check_installed
    say "telegram-mcp $tag is installed for Claude Code (user scope), with its MCP server and hooks."
    warm_up_guard
    print_next_steps
}

uninstall() {
    read_state
    if [ "$mp_kind" = "none" ] && [ "$plugin_installed" = "0" ]; then
        say "telegram-mcp is not installed; nothing changed."
        exit 0
    fi
    if [ "$plugin_installed" = "1" ]; then
        claude_step plugin uninstall "$PLUGIN_ID" --scope user ||
            fail "'claude plugin uninstall $PLUGIN_ID --scope user' failed (exit $step_status); nothing was removed."
        say "Removed the telegram-mcp plugin (its MCP server and hooks) from Claude Code."
    fi
    if [ "$mp_kind" != "none" ]; then
        claude_step plugin marketplace remove "$MARKETPLACE" ||
            fail "'claude plugin marketplace remove $MARKETPLACE' failed (exit $step_status)."
        say "Removed the telegram-mcp marketplace."
    fi
    say ""
    say "Your Telegram session stays in the macOS Keychain (service telegram-mcp) unless you ran"
    say "'telegram-mcp logout' before uninstalling. To remove it now, end the session in Telegram"
    say "(Settings > Devices) and delete the three Keychain items:"
    for item in session api_id api_hash; do
        say "  security delete-generic-password -s telegram-mcp -a $item"
    done
    say "Your settings and activity log stay in $DATA_DIR/ (delete that folder to remove them)."
}

main() {
    tag=""
    action="install"
    while [ $# -gt 0 ]; do
        case "$1" in
            --tag)
                [ $# -ge 2 ] || refuse "--tag needs a value, for example --tag $DEFAULT_TAG."
                tag=$2
                shift 2
                ;;
            --tag=*)
                tag=${1#--tag=}
                shift
                ;;
            --uninstall)
                action="uninstall"
                shift
                ;;
            -h | --help)
                usage
                exit 0
                ;;
            *) refuse "unknown argument '$1'; see --help." ;;
        esac
    done
    check_macos
    if [ "$action" = "uninstall" ]; then
        [ -z "$tag" ] || refuse "--tag and --uninstall cannot be used together."
        check_programs claude
        uninstall
    else
        tag=${tag:-$DEFAULT_TAG}
        check_programs uv uvx claude
        install
    fi
}

# The whole script is read before anything runs, so `curl … | sh` never runs half a script.
main "$@"
