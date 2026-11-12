"""Shell lines for the guard's differential test (NFR2.4): for each, the guard's reader must
split and expand the words exactly as zsh and bash do.

Each entry is (prefix, words): the shells run ``<prefix>print -rN -- <words>`` (zsh) and
``<prefix>printf '%s\\0' <words>`` (bash), with a temporary HOME and only print commands. The
lines use the syntax both shells share; forms where they differ are refused by the reader and
tested separately."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping

ENVIRONMENT_VALUES = {"TG_ONE": "single", "TG_TWO": "two words", "TG_EMPTY": ""}

CORPUS: list[tuple[str, str]] = [
    # plain words, quotes and escapes
    ("", "plain words here"),
    ("", "'single quoted' \"double quoted\""),
    ("", "mixed'single'\"double\"plain"),
    ("", "a\\ b c\\\\d \\'e \\\"f"),
    ("", '"a \\"quoted\\" word"'),
    ("", '"back\\\\slash" "dollar\\$sign" "tick\\`s"'),
    ("", '"keep \\n and \\t literal"'),
    ("", "'it''s'"),
    ("", "'a'\\''b'"),
    ("", "\\$HOME '$HOME' \"\\$HOME\""),
    ("", "\"a\"b'c'$'d'\"e\""),
    ("", "\\a\\b\\c"),
    ("", '"\\a\\b"'),
    ("", "a\\\nb"),
    ("", "'line\\'"),
    ("", "'a b'\"c d\""),
    ("", "\"'nested single'\""),
    ("", "'\"nested double\"'"),
    ("", "\"a;b|c&d\" 'x>y<z'"),
    ("", "a\\;b a\\|b a\\&b a\\>b a\\(b\\)"),
    ("", "\\#notcomment"),
    ("", "\"#hash\" '#'"),
    ("", "a#b c#"),
    ("", "x # a comment"),
    ("", '"two\nlines"'),
    ("", "'tab\there'"),
    ("", "\\\\\\\\server"),
    ("", "'$(not a sub)' \"\\$(not a sub)\""),
    ("", "'`backtick`'"),
    ("", '"\\`literal\\`"'),
    ("", "'semi;colon' \"pipe|char\""),
    ("", "'ünïcode' \"é\""),
    ("", "@ % ^ + = ,"),
    ("", "a:b:c ,x, .y."),
    ("", "x=1 y=2"),
    ("", "--opt=value -x key=val -n -e"),
    ("", "\"\" '' x"),
    ("", "a 2>/dev/null b"),
    ("", "a 2>&1 b 2>>/dev/null c"),
    # $'...'
    ("", "$'tab\\there' $'nl\\nx' $'quote\\'s'"),
    ("", "$'\\x41\\x42' $'\\101\\102' $'\\e[0m' $'\\a\\b\\f\\v\\r'"),
    ("", "$'\\\\' $'\\\"' $'\\?'"),
    ("", "$'\\x01\\x1a'"),
    ("", "$'\\''"),
    ("", "$'\\x7e/x'"),
    ("", "$'a\\x20b' $'\\x2Fetc'"),
    ("", "$'\\101BC' $'\\E[1m'"),
    # tilde
    ("", "~"),
    ("", "~/"),
    ("", "\"~\" '~' \\~"),
    ("", '~/"quoted dir"/x'),
    ("", "~/a~b"),
    ("", "~/.config/telegram-mcp"),
    ("", "~+"),
    ("", "~+/x"),
    # the data folder, written in the ways the examples use
    ("", "~/Library/Application\\ Support/telegram-mcp/activity.log"),
    ("", '"$HOME/Library/Application Support/telegram-mcp"'),
    ("", "~/Library/Appl{ication\\ S,x}upport/telegram-mcp/activity.log"),
    ("X=Application; ", '~/Library/"$X Support"/telegram-mcp/config.toml'),
    ('D="$HOME/Library/Application Support/telegram-mcp"; ', '"$D/activity.log"'),
    ("", '"$HOME/Library/Application Support/telegram-mcp/broker.sock"'),
    ("", "~/Library//Application\\ Support///telegram-mcp/"),
    ("", '"${HOME}"/Library/"Application Support"/telegram-mcp'),
    ("P=telegram; ", '~/Library/Application\\ Support/"$P"-mcp'),
    ("", "~/library/application\\ support/TELEGRAM-MCP"),
    ("", "$'\\x7e'/Library/Application\\ Support/telegram-mcp"),
    ("", "~/Library/Application' 'Support/telegram-mcp/config.toml"),
    ("", "~/telegram-mcp-notes ~/Downloads/telegram-mcp/outbox"),
    # brace expansion
    ("", "{a,b,c}"),
    ("", "x{1..3}y"),
    ("", "{a,{b,c}}d"),
    ("", "\"{a,b}\" '{a,b}' \\{a,b\\}"),
    ("", "{a} {} a,b {a..}"),
    ("", "{a..e} {e..a} {A..E}"),
    ("", "{1..5} {5..1} {-2..2}"),
    ("", "{a,b}{1,2}"),
    ("", 'pre{"quoted part",x}post'),
    ("", "{a,b\\,c}"),
    ("", "x{,}y"),
    ("", "a{b,c}d{e,f}g"),
    ("", "{1..3}{a,b}"),
    ("", "'{1..3}' {1..1} {a..a}"),
    ("", "{a,b}c{d,e}"),
    ("", ".{a,b} x,{a,b},y"),
    ("", "{a,b}{c,{d,e}}"),
    ("", "{{a,b},c}"),
    ("", '"$HOME"{/a,/b}'),
    ("", "{x,y}/{1,2}"),
    ("", "~/{a,b}"),
    ("", "{01,02} {a.b,c.d}"),
    # variables from the line and the environment
    ("", "${HOME}/x ${HOME:-default} ${NOPE:-default}"),
    ("", '"${NOPE:-two words}"'),
    ("", '${NOPE-dflt} "${NOPE-}"x'),
    ("E=; ", '"${E:-was empty}" "${E-not used}"x'),
    ("X=1; Y=2; ", '$X$Y "$X-$Y" ${X}_${Y}'),
    ("export Z=exported; ", "$Z"),
    ("EMPTY=; ", '$EMPTY kept "$EMPTY"'),
    ("", "\"$HOME\"/'$HOME'/$HOME"),
    ("", '"$TG_TWO" ${TG_ONE} $TG_ONE'),
    ("", '$TG_EMPTY "$TG_EMPTY" x'),
    ("", '"$HOME" "${HOME}" "$HOME"x'),
    ("", "${HOME:-} ${HOME-}"),
    ("", '"${NOPE:-$HOME}" ${NOPE:-$TG_ONE}'),
    ("", "${NOPE:-~/x}"),
    ("A=b; ", "${A:=c} ${C_NEW:=set} $C_NEW"),
    ("V=/tmp; V=$V/sub; ", '"$V" ${V}'),
    ("unset HOME_COPY; HOME_COPY=$HOME; ", '"$HOME_COPY/x"'),
]


def run_zsh(prefix: str, words: str, env: Mapping[str, str], cwd: str) -> list[str]:
    return _run(["/bin/zsh", "-f", "-c", f"{prefix}print -rN -- {words}"], env, cwd)


def run_bash(prefix: str, words: str, env: Mapping[str, str], cwd: str) -> list[str]:
    script = f"{prefix}printf '%s\\0' {words}"
    return _run(["/bin/bash", "--norc", "--noprofile", "-c", script], env, cwd)


def _run(argv: list[str], env: Mapping[str, str], cwd: str) -> list[str]:
    # Bytes, not text: text mode would turn a printed \r into \n.
    result = subprocess.run(
        argv, capture_output=True, env=dict(env), cwd=cwd, check=False, timeout=10
    )
    stdout = result.stdout.decode()
    assert result.returncode == 0, (argv, result.stderr)
    assert result.stderr == b"", (argv, result.stderr)
    assert stdout.endswith("\0"), (argv, stdout)
    return stdout[:-1].split("\0")
