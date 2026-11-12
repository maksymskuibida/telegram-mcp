"""The guard's shell reader (BR2.1, BR2.4, NFR2.4): it splits and expands a line the way zsh
and bash do, keeps what it cannot know as an unknown value, and refuses what it cannot read."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from support.shell_corpus import CORPUS, ENVIRONMENT_VALUES, run_bash, run_zsh
from telegram_mcp.guard.shell import Command, ShellState, Unparsable, expand_line

UNKNOWN = "<?>"


@pytest.fixture
def cwd(home: Path) -> Path:
    folder = home / "work"
    folder.mkdir()
    return folder


@pytest.fixture
def environ(home: Path, cwd: Path) -> dict[str, str]:
    return dict(ENVIRONMENT_VALUES, HOME=str(home), PWD=str(cwd), PATH="/usr/bin:/bin")


def read(line: str, environ: dict[str, str], cwd: Path) -> list[Command]:
    return expand_line(line, ShellState.start(environ, home=environ["HOME"], cwd=str(cwd)))


def words(line: str, environ: dict[str, str], cwd: Path) -> list[list[str]]:
    return [[w.render(UNKNOWN) for w in c.words] for c in read(line, environ, cwd)]


# Splitting into simple commands and words


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("ls", [["ls"]]),
        ("ls -la  src\tdocs", [["ls", "-la", "src", "docs"]]),
        (
            "a b; c && d || e | f & g\nh",
            [["a", "b"], ["c"], ["d"], ["e"], ["f"], ["g"], ["h"]],
        ),
        ("a |& b", [["a"], ["b"]]),
        ("echo 'a b' \"c d\" e\\ f", [["echo", "a b", "c d", "e f"]]),
        ("echo a#b # the rest is a comment", [["echo", "a#b"]]),
        ("# only a comment", []),
        ("echo a \\\n  b", [["echo", "a", "b"]]),
        ("if [ -f x ]; then cat x; else ls; fi", [["[", "-f", "x", "]"], ["cat", "x"], ["ls"]]),
        ("while true; do sleep 1; done", [["true"], ["sleep", "1"]]),
        ("{ echo a; echo b; } > out", [["echo", "a"], ["echo", "b"], []]),
        ("(cd src && ls)", [["cd", "src"], ["ls"]]),
        ("! grep -q x f", [["grep", "-q", "x", "f"]]),
        ("[[ -n $HOME && $HOME != / ]]", [["[[", "-n", "<home>", "&&", "<home>", "!=", "/", "]]"]]),
        ("f() { ls; }; f", [["ls"], ["f"]]),
        ("function g { pwd; }", [["pwd"]]),
        ("(( n = 1 + 2 )); echo $n", [["echo", UNKNOWN]]),
        ("time ls", [["time", "ls"]]),
    ],
)
def test_lines_split_into_simple_commands_and_words(
    line: str, expected: list[list[str]], environ: dict[str, str], cwd: Path
) -> None:
    filled = [[w.replace("<home>", environ["HOME"]) for w in c] for c in expected]
    assert words(line, environ, cwd) == filled


def test_quotes_escapes_and_ansi_c_strings_are_taken_off(
    environ: dict[str, str], cwd: Path
) -> None:
    line = "printf %s 'a\\b' \"c\\\"d\\$e\\x\" $'t\\tn\\nx\\x41\\101\\'' $\"loc\""
    assert words(line, environ, cwd) == [["printf", "%s", "a\\b", 'c"d$e\\x', "t\tn\nxAA'", "loc"]]


def test_redirect_targets_are_split_off(environ: dict[str, str], cwd: Path) -> None:
    (command,) = read("cat < in > out 2>> err 2>&1 &> both >| clobber 3<> rw >&-", environ, cwd)
    assert [w.render() for w in command.words] == ["cat"]
    targets = [(r.op, None if r.target is None else r.target.render()) for r in command.redirects]
    assert targets == [
        ("<", "in"),
        (">", "out"),
        (">>", "err"),
        (">&", None),
        ("&>", "both"),
        (">|", "clobber"),
        ("<>", "rw"),
        (">&", None),
    ]


def test_a_dup_to_a_file_name_is_a_redirect_target(environ: dict[str, str], cwd: Path) -> None:
    (command,) = read("echo hi >&log.txt", environ, cwd)
    assert [r.target.render() for r in command.redirects if r.target] == ["log.txt"]


# Braces


def test_braces_expand_up_to_256_words(environ: dict[str, str], cwd: Path) -> None:
    assert words("echo {a,b}{1..3}", environ, cwd) == [["echo", "a1", "a2", "a3", "b1", "b2", "b3"]]
    (command,) = read("echo {1..256}", environ, cwd)
    assert len(command.words) == 257


@pytest.mark.parametrize("line", ["echo {1..257}", "echo {a,b}{1..200}", "echo {1..9}{1..9}{1..9}"])
def test_more_than_256_brace_results_are_refused(
    line: str, environ: dict[str, str], cwd: Path
) -> None:
    with pytest.raises(Unparsable):
        read(line, environ, cwd)


# Tilde and variables


def test_tilde_forms(environ: dict[str, str], cwd: Path) -> None:
    home = environ["HOME"]
    assert words(
        'echo ~ ~/x ~+ ~+/y "~" x~ ~tester/z ~nosuchuser', environ | {"USER": "tester"}, cwd
    ) == [["echo", home, f"{home}/x", str(cwd), f"{cwd}/y", "~", "x~", f"{home}/z", UNKNOWN]]


def test_variables_from_the_line_are_applied_in_order(environ: dict[str, str], cwd: Path) -> None:
    line = 'X=1; echo $X; X=$X/2; export Y="$X b"; echo "$X" "$Y"; unset X; echo "[$X]"'
    assert words(line, environ, cwd) == [
        [],
        ["echo", "1"],
        [],
        ["export", "Y=1/2 b"],
        ["echo", "1/2", "1/2 b"],
        ["unset", "X"],
        ["echo", "[]"],
    ]
    (assignment,) = read("X=~/a", environ, cwd)
    assert [(name, value.render()) for name, value in assignment.assignments] == [
        ("X", f"{environ['HOME']}/a")
    ]


def test_a_prefix_assignment_holds_only_for_its_command(environ: dict[str, str], cwd: Path) -> None:
    assert words('A=1 env; echo "$A"', environ, cwd) == [["env"], ["echo", UNKNOWN]]


def test_variables_come_from_the_environment(environ: dict[str, str], cwd: Path) -> None:
    assert words('echo $TG_ONE "${TG_TWO}" "$TG_EMPTY"', environ, cwd) == [
        ["echo", "single", "two words", ""]
    ]


def test_pwd_follows_cd_within_the_line(environ: dict[str, str], cwd: Path) -> None:
    home = environ["HOME"]
    assert words('cd /tmp; echo "$PWD" "$OLDPWD"; cd; echo "$PWD"', environ, cwd) == [
        ["cd", "/tmp"],
        ["echo", "/tmp", str(cwd)],
        ["cd"],
        ["echo", UNKNOWN],
    ]
    commands = read("cd /tmp && cd sub && cd ..; ls", environ, cwd)
    assert [c.base for c in commands] == [str(cwd), "/tmp", "/tmp/sub", "/tmp"]
    assert read(f"cd {home}; cd -; ls", environ, cwd)[-1].base is None


def test_for_lists_set_the_variable_once_per_value(environ: dict[str, str], cwd: Path) -> None:
    assert words('for f in a "b c"; do cat "$f"; done; echo "$f"', environ, cwd) == [
        ["cat", "a"],
        ["cat", "b c"],
        ["echo", "a"],
        ["echo", "b c"],
    ]
    assert words('for f; do cat "$f"; done', environ, cwd) == [["cat", UNKNOWN]]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("echo ${NOPE:-dflt} ${NOPE-d2} ${TG_ONE:-x}", ["echo", "dflt", "d2", "single"]),
        ("echo ${NOPE:=set}; echo $NOPE", ["echo", "set"]),
        ('echo "${TG_EMPTY:-was empty}" "${TG_EMPTY-not used}"', ["echo", "was empty", ""]),
        ('echo "${NOPE:-$HOME/x}"', ["echo", "<home>/x"]),
    ],
)
def test_default_value_forms(
    line: str, expected: list[str], environ: dict[str, str], cwd: Path
) -> None:
    filled = [w.replace("<home>", environ["HOME"]) for w in expected]
    assert words(line, environ, cwd)[0] == filled


@pytest.mark.parametrize(
    "word",
    [
        "$NOPE",
        '"$NOPE"',
        "${NOPE}",
        "$1",
        "$@",
        '"$*"',
        "$#",
        "$!",
        "$-",
        "$0",
        "${#HOME}",
        "${HOME%/*}",
        "${HOME##*/}",
        "${HOME/a/b}",
        "${HOME:1:3}",
        "${HOME:+x}",
        "${NOPE:?x}",
        "${arr[1]}",
        "$((1 + 2))",
        '"$(date)"',
        "`date`",
    ],
)
def test_values_the_reader_cannot_know_are_unknown(
    word: str, environ: dict[str, str], cwd: Path
) -> None:
    command = read(f"echo x{word}", environ, cwd)[-1]
    (_echo, argument) = command.words
    assert not argument.known
    assert argument.render(UNKNOWN).startswith("x")
    assert argument.fixed_part() == "x"


def test_status_and_process_id_are_decidable(environ: dict[str, str], cwd: Path) -> None:
    (command,) = read("echo $? $$ x$?", environ, cwd)
    assert all(w.known for w in command.words)


def test_read_and_source_make_variables_unknown(environ: dict[str, str], cwd: Path) -> None:
    assert words('read -r X; echo "$X"', environ, cwd)[-1] == ["echo", UNKNOWN]
    assert words('source ./env.sh; echo "$HOME"', environ, cwd)[-1] == ["echo", UNKNOWN]
    assert words('. ./env.sh; X=1; echo "$X"', environ, cwd)[-1] == ["echo", "1"]


# Nested lines: substitutions and here-documents


def test_substitutions_are_read_as_nested_lines(environ: dict[str, str], cwd: Path) -> None:
    commands = read('echo "$(cat a; ls b)" `pwd` <(sort c) $(( $n + 1 ))', environ, cwd)
    assert [([w.render(UNKNOWN) for w in c.words], c.depth) for c in commands] == [
        (["cat", "a"], 1),
        (["ls", "b"], 1),
        (["pwd"], 1),
        (["sort", "c"], 1),
        (["echo", UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN], 0),
    ]


def test_a_substitution_changes_nothing_outside_it(environ: dict[str, str], cwd: Path) -> None:
    commands = read('echo "$(cd /tmp; X=1)"; echo "$X"; ls', environ, cwd)
    assert commands[-2].words[1].render(UNKNOWN) == UNKNOWN
    assert commands[-1].base == str(cwd)


def test_here_documents_are_kept_as_text(environ: dict[str, str], cwd: Path) -> None:
    line = "cat <<EOF\nhello $TG_ONE\n\\$HOME stays\nEOF\ncat <<'END' >out\n$HOME stays\nEND"
    first, second = read(line, environ, cwd)
    assert first.redirects[0].heredoc is not None
    assert first.redirects[0].heredoc.text.render() == "hello single\n$HOME stays\n"
    assert not first.redirects[0].heredoc.quoted
    assert second.redirects[0].heredoc is not None
    assert second.redirects[0].heredoc.text.render() == "$HOME stays\n"
    assert second.redirects[0].heredoc.quoted
    assert second.redirects[1].target is not None
    assert second.redirects[1].target.render() == "out"


def test_a_here_document_with_tabs_stripped(environ: dict[str, str], cwd: Path) -> None:
    (command,) = read("cat <<-EOF\n\tindented\n\tEOF", environ, cwd)
    assert command.redirects[0].heredoc is not None
    assert command.redirects[0].heredoc.text.render() == "indented\n"


def test_a_substitution_in_a_here_document_is_a_nested_line(
    environ: dict[str, str], cwd: Path
) -> None:
    commands = read("cat <<EOF\n$(whoami) was here\nEOF", environ, cwd)
    assert [(c.words[0].render(), c.depth) for c in commands] == [("whoami", 1), ("cat", 0)]
    assert not commands[-1].redirects[0].heredoc.text.known  # type: ignore[union-attr]


def test_the_commit_message_idiom_is_read(environ: dict[str, str], cwd: Path) -> None:
    line = "git commit -m \"$(cat <<'EOF'\nFix it (really)\n\nCo-Authored-By: A <a@b.c>\nEOF\n)\""
    commands = read(line, environ, cwd)
    assert [c.words[0].render() for c in commands] == ["cat", "git"]
    heredoc = commands[0].redirects[0].heredoc
    assert heredoc is not None
    assert heredoc.text.render() == "Fix it (really)\n\nCo-Authored-By: A <a@b.c>\n"


def test_pipelines_remember_the_command_before(environ: dict[str, str], cwd: Path) -> None:
    first, second, third = read("echo ls | sh; cat x", environ, cwd)
    assert second.piped_from is first
    assert first.piped_from is None
    assert third.piped_from is None


# Lines the reader cannot read (BR2.1)


@pytest.mark.parametrize(
    "line",
    [
        'echo "unclosed',
        "echo 'unclosed",
        "echo $'unclosed",
        "echo $(unclosed",
        "echo `unclosed",
        "echo ${unclosed",
        "echo $((1 + 2)",
        "cat <<EOF\nno end",
        "cat <<EOF",
        "(echo a",
        "echo a)",
        "echo a {b,c",
        "cat ~/Library/Appl{ication S,x}upport/telegram-mcp/activity.log",
        "echo a (b)",
        "echo >",
        "echo a |",
        "&& echo a",
        "case $x in a) echo;; esac",
        "select x in a b; do echo; done",
        "arr=(a b c)",
        "echo a;; echo b",
        "echo $'\\u00e9'",
        "echo $'\\cA'",
        "echo $'a\\0b'",
        "echo {1..10..2}",
        "echo {01..10}",
        "echo {a..9}",
        "echo {,a}",
        "echo ${!name}",
        "echo ${x^^}",
        "echo ${x,,}",
        "echo ${x@Q}",
    ],
)
def test_a_line_that_cannot_be_read_is_refused(
    line: str, environ: dict[str, str], cwd: Path
) -> None:
    with pytest.raises(Unparsable):
        read(line, environ, cwd)


# zsh-only forms (NFR2.4)


@pytest.mark.parametrize(
    "line",
    [
        "echo ${(U)x}",
        'echo ${(f)"$(ls)"}',
        "echo ${~x}",
        "echo ${=x}",
        "echo ${^x}",
        "echo $=x",
        "echo $~x",
        "echo ${x:h}",
        "echo ${x:t:r}",
        'echo "$x[1]"',
        "echo $HOME[1]",
        "diff =(ls) =(ls -a)",
        "echo =ls",
        "[ a == b ]",
        "ls *(.)",
        "ls *(om[1])",
        "ls (#i)readme*",
        "ls ^*.txt",
        "ls *~*.log",
        "ls **/*#",
        "cat <<< 'here string'",
        "echo !!",
        "echo !$",
        'echo "a!b"',
        "echo hi;& echo",
        "echo hi;| echo",
        "echo a=~/x",
        "X=1 echo b=a:~/y",
        "echo $X_UNQUOTED_TWO",
        "echo $TG_TWO",
        "S='*'; echo $S",
    ],
)
def test_zsh_only_forms_are_refused(line: str, environ: dict[str, str], cwd: Path) -> None:
    with pytest.raises(Unparsable):
        read(line, environ | {"X_UNQUOTED_TWO": "two words"}, cwd)


@pytest.mark.parametrize(
    "line",
    [
        "[ ! -f x ]",
        "! true",
        "[ a != b ]",
        "echo 'a!b'",
        'echo "Done!"',
        "echo hi!",
        "git log HEAD~1 HEAD^",
        "[ a = b ]",
        "echo a=b c==d",
        "ls *.txt",
        "ls [ab]*",
        "awk '!seen[$0]++' f",
    ],
)
def test_shared_forms_near_the_refused_ones_are_read(
    line: str, environ: dict[str, str], cwd: Path
) -> None:
    assert read(line, environ, cwd)


# The differential test: the reader's words equal zsh's and bash's (NFR2.4)


def test_the_corpus_is_large_enough() -> None:
    assert len(CORPUS) >= 100


@pytest.fixture
def shells_present() -> None:
    # The test fails, never skips, when a shell is missing: CI runs on macOS, which has both.
    assert os.access("/bin/zsh", os.X_OK), "/bin/zsh is missing"
    assert os.access("/bin/bash", os.X_OK), "/bin/bash is missing"


@pytest.mark.usefixtures("shells_present")
@pytest.mark.parametrize(("prefix", "line"), CORPUS)
def test_the_reader_splits_words_like_zsh_and_bash(
    prefix: str, line: str, environ: dict[str, str], cwd: Path
) -> None:
    zsh = run_zsh(prefix, line, environ, str(cwd))
    bash = run_bash(prefix, line, environ, str(cwd))
    commands = read(f"{prefix}printf '%s\\0' {line}", environ, cwd)
    last = commands[-1]
    assert all(w.known for w in last.words)
    ours = [w.render() for w in last.words[2:]]
    assert ours == zsh
    assert ours == bash


# Less common forms: each is read, or refused, on purpose


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("&>out ls", [["ls"]]),
        ("(echo a\n)", [["echo", "a"]]),
        ("for ((i = 0; i < 3; i++)); do echo $i; done", [["echo", UNKNOWN]]),
        ("[[ a < b || c > d ]]", [["[[", "a", "<", "b", "||", "c", ">", "d", "]]"]]),
        ("echo a\\", [["echo", "a\\"]]),
        ("echo a$ b$", [["echo", "a$", "b$"]]),
        ('echo "a\\qb" "x$" "`pwd`"', [["pwd"], ["echo", "a\\qb", "x$", UNKNOWN]]),
        ("echo `echo \\`date\\``", [["date"], ["echo", UNKNOWN], ["echo", UNKNOWN]]),
        ("echo $'\\q' $'\\xZ'", [["echo", "\\q", "\\xZ"]]),
        ("echo $(( (1 + 2) * 3 ))", [["echo", UNKNOWN]]),
        ("echo ${1} ${1:-x}", [["echo", UNKNOWN, UNKNOWN]]),
        (
            "echo ${NOPE:-a\\b} ${NOPE:-'q'} ${NOPE:-\"r\"} ${NOPE:-`pwd`}",
            [["pwd"], ["echo", "ab", "q", "r", UNKNOWN]],
        ),
        ("echo { {'a'..c}", [["echo", "{", "{a..c}"]]),
        ("X=a; X+=b; Y+=z; echo $X $Y", [[], [], [], ["echo", "ab", "z"]]),
        ("cd -- /tmp; pwd", [["cd", "--", "/tmp"], ["pwd"]]),
        ("echo ~'a'/b", [["echo", "~a/b"]]),
        ("unset HOME; echo ~", [["unset", "HOME"], ["echo", "<home>"]]),
        ("cd /tmp; echo ~-", [["cd", "/tmp"], ["echo", "<cwd>"]]),
        ("cd -; echo ~+", [["cd", "-"], ["echo", UNKNOWN]]),
        ("cat <<EOF\ncost $ 5\nEOF", [["cat"]]),
        (
            "echo ${#$(date)} ${NOPE%${X:-$(whoami)}}",
            [["date"], ["whoami"], ["echo", UNKNOWN, UNKNOWN]],
        ),
    ],
)
def test_less_common_forms_are_read(
    line: str, expected: list[list[str]], environ: dict[str, str], cwd: Path
) -> None:
    filled = [
        [w.replace("<home>", environ["HOME"]).replace("<cwd>", str(cwd)) for w in c]
        for c in expected
    ]
    assert words(line, environ, cwd) == filled


@pytest.mark.parametrize(
    "line",
    [
        "( )",
        "echo $(cat <<EOF)",
        "(a) (b)",
        "(a) b",
        "(a) > out b",
        "for 1x in a; do :; done",
        "[[ -n x",
        "cat <<$X\nx\n$X",
        "echo $(( $(date) ))",
        "echo $(( `date` ))",
        "echo ${.}",
        "echo {a..C}",
        "for x in {1..200} {1..200}; do :; done",
        "for a in {1..20}; do for b in {1..20}; do echo $a$b; done; done",
        "for a in {1..20}; do for b in $a$a; do :; done; done",
        "echo > {a,b}",
        "X=a:~/b",
    ],
)
def test_less_common_forms_are_refused(line: str, environ: dict[str, str], cwd: Path) -> None:
    with pytest.raises(Unparsable):
        read(line, environ, cwd)
