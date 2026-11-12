"""The guard's rules by tool (BR1 to BR3, BR4.1).

Each check raises Refusal for the first rule that refuses; returning means the call passes."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from functools import lru_cache
from urllib.parse import unquote, urlsplit

from telegram_mcp.guard import patterns
from telegram_mcp.guard.model import (
    ALLOWED_SUBCOMMANDS,
    COMMAND_NAME,
    DATA_FILE_NAMES,
    DATA_FOLDER_TEXT,
    PACKAGE_MODULE,
    REFUSED_SUBCOMMAND_WORDS,
    UNIQUE_FILE_NAMES,
    GuardCall,
    ProtectedSet,
    Refusal,
    Route,
)
from telegram_mcp.guard.paths import Resolver, Unresolvable, absolute, expand_home
from telegram_mcp.guard.shell import (
    Command,
    Piece,
    ShellState,
    Unparsable,
    Word,
    expand_line,
    known_word,
)
from telegram_mcp.paths import FixedPaths

WATCHED_TOOLS = frozenset(
    {
        "Bash",
        "Monitor",
        "Read",
        "Write",
        "Edit",
        "MultiEdit",
        "NotebookEdit",
        "Glob",
        "Grep",
        "Task",
        "Agent",
        "WebFetch",
    }
)
RULES = {
    "Read": "check_file_tool",
    "Write": "check_file_tool",
    "Edit": "check_file_tool",
    "MultiEdit": "check_file_tool",
    "NotebookEdit": "check_file_tool",
    "Glob": "check_search_tool",
    "Grep": "check_search_tool",
    "WebFetch": "check_fetch_tool",
    "Bash": "check_bash",
    "Monitor": "check_bash",  # Monitor runs a shell command, read exactly as Bash's
    "Task": "check_subagent",
    "Agent": "check_subagent",
}
REQUIRED = {
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
    "Glob": "pattern",
    "Grep": "pattern",
    "Bash": "command",
    "Monitor": "command",
    "Task": "prompt",
    "Agent": "prompt",
    "WebFetch": "url",
}
OPTIONAL = {
    "Glob": ("path", "glob"),
    "Grep": ("path", "glob"),
    "Task": ("description",),
    "Agent": ("description",),
}
MAX_NESTING = 8
MAX_TEXT = 64 * 1024
MAX_DECODING = 3

OWN_TEXT_RE = re.compile(
    COMMAND_NAME
    + r"(?:[@=][^\s'\"`]*)?['\"`]?\s+['\"`]?(?:"
    + "|".join(REFUSED_SUBCOMMAND_WORDS)
    + r")\b",
    re.IGNORECASE,
)
PACKAGE_RE = re.compile(r"\b" + PACKAGE_MODULE + r"\b")
KEYCHAIN_WORDS = ("keyring", "keychain", "secitem", "security")
SLASHES_RE = re.compile(r"/+")
PYTHON_RE = re.compile(r"(?:python|pypy)(?:\d+(?:\.\d+)*)?")

SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
INLINE = {"node": ("-e", "--eval", "-p", "--print"), "osascript": ("-e",), "swift": ("-e",)}
CLUSTER = {"perl": "eE", "ruby": "e"}
FILE_PROGRAMS = frozenset(
    [
        "cat",
        "less",
        "more",
        "head",
        "tail",
        "cp",
        "mv",
        "ln",
        "rsync",
        "scp",
        "tar",
        "zip",
        "unzip",
        "ditto",
        "open",
        "file",
        "stat",
        "strings",
        "xxd",
        "hexdump",
        "od",
        "base64",
        "grep",
        "egrep",
        "rg",
        "find",
        "ls",
        "du",
        "wc",
        "sort",
        "diff",
        "cmp",
        "sed",
        "awk",
        "perl",
        "ruby",
        "python",
        "python3",
        "node",
        "sqlite3",
        "plutil",
        "defaults",
        "nc",
        "socat",
        "curl",
        "wget",
        "dd",
        "tee",
        "install",
        "chmod",
        "chown",
        "touch",
        "rm",
        "mkdir",
        "rmdir",
        "truncate",
        "split",
        "csplit",
        "xattr",
        "mdls",
        "mdfind",
        "textutil",
        "pbcopy",
    ]
)
GREP_PROGRAMS = frozenset({"grep", "egrep", "fgrep", "rg"})
GREP_VALUE_OPTIONS = frozenset(
    [
        "-e",
        "-f",
        "-m",
        "-A",
        "-B",
        "-C",
        "-d",
        "-D",
        "--regexp",
        "--file",
        "--max-count",
        "--context",
        "--after-context",
        "--before-context",
        "--include",
        "--exclude",
        "--exclude-dir",
        "--label",
    ]
)
RG_VALUE_OPTIONS = GREP_VALUE_OPTIONS | frozenset(
    [
        "-g",
        "-t",
        "-T",
        "-j",
        "-M",
        "-r",
        "-E",
        "--glob",
        "--iglob",
        "--type",
        "--type-not",
        "--threads",
        "--max-columns",
        "--max-depth",
        "--replace",
        "--sort",
        "--sortr",
        "--encoding",
        "--ignore-file",
        "--type-add",
        "--pre",
        "--path-separator",
    ]
)
FIND_PATTERN_OPTIONS = frozenset(
    [
        "-name",
        "-iname",
        "-path",
        "-ipath",
        "-regex",
        "-iregex",
        "-wholename",
        "-iwholename",
        "-lname",
        "-ilname",
    ]
)
FIND_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
UV_OPTIONS = frozenset(
    [
        "--from",
        "--with",
        "--with-requirements",
        "--with-editable",
        "--python",
        "-p",
        "--constraints",
        "-c",
        "--index",
        "--default-index",
        "--directory",
        "--project",
        "--env-file",
    ]
)
WRAPPERS: dict[str, frozenset[str]] = {
    "env": frozenset({"-u", "-C", "-S"}),
    "command": frozenset(),
    "builtin": frozenset(),
    "exec": frozenset({"-a"}),
    "nohup": frozenset(),
    "time": frozenset({"-o"}),
    "nice": frozenset({"-n"}),
    "timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
    "caffeinate": frozenset({"-t", "-w"}),
    "sudo": frozenset({"-u", "-g", "-C", "-D", "-h", "-p", "-U"}),
    "xargs": frozenset({"-I", "-J", "-L", "-n", "-P", "-s", "-E", "-d"}),
    "uvx": UV_OPTIONS,
    "uv run": UV_OPTIONS,
    "uv tool run": UV_OPTIONS,
    "pipx run": frozenset({"--spec", "--python"}),
}


@dataclass
class Context:
    paths: FixedPaths
    environ: Mapping[str, str]
    protected: ProtectedSet
    resolver: Resolver
    cwd: str
    home: str

    @classmethod
    def build(cls, paths: FixedPaths, environ: Mapping[str, str], call: GuardCall) -> Context:
        protected = ProtectedSet.from_paths(paths)
        home = protected.home
        return cls(
            paths=paths,
            environ=environ,
            protected=protected,
            resolver=Resolver(protected),
            cwd=expand_home(call.cwd, home) if call.cwd else home,
            home=home,
        )


# Reading the call (BR4.1)


def read_call(raw: object) -> GuardCall:
    if not isinstance(raw, dict):
        raise Refusal(Route.UNREADABLE_CALL, "")
    tool = raw.get("tool_name")
    if not isinstance(tool, str) or not tool:
        raise Refusal(Route.UNREADABLE_CALL, "")
    tool_input = raw.get("tool_input")
    cwd = raw.get("cwd")
    if tool not in WATCHED_TOOLS:
        return GuardCall(tool, {}, None)
    if not isinstance(tool_input, dict):
        raise Refusal(Route.UNREADABLE_CALL, "")
    if cwd is not None and not isinstance(cwd, str):
        raise Refusal(Route.UNREADABLE_CALL, "")
    if not isinstance(tool_input.get(REQUIRED[tool]), str):
        raise Refusal(Route.UNREADABLE_CALL, "")
    for name in OPTIONAL.get(tool, ()):
        if name in tool_input and not isinstance(tool_input[name], str):
            raise Refusal(Route.UNREADABLE_CALL, "")
    return GuardCall(tool, tool_input, cwd)


def check_size(call: GuardCall) -> None:
    """Every text the guard reads is at most 64 KiB, which keeps its time bounded."""
    fields = (REQUIRED[call.tool_name], *OPTIONAL.get(call.tool_name, ()))
    texts = [call.tool_input.get(name) or "" for name in fields] + [call.cwd or ""]
    if any(len(text) > MAX_TEXT for text in texts):
        raise Refusal(Route.UNPARSABLE, "a text in the call is longer than 64 KiB")


def summary(call: GuardCall) -> str:
    """The part of the call a refusal quotes."""
    value = call.tool_input.get(REQUIRED.get(call.tool_name, ""), "")
    return value if isinstance(value, str) else ""


# Text checks shared by every tool


def _is_name_char(char: str) -> bool:
    return char.isalnum() or char in "-_.+@~"


def normalize(text: str) -> str:
    folded = SLASHES_RE.sub("/", text.casefold())
    while "/./" in folded:
        folded = folded.replace("/./", "/")
    return folded


def find_data_text(text: str) -> str | None:
    """Data folder text ending at a path boundary, or a unique file name of the data folder."""
    folded = normalize(text)
    start = folded.find(DATA_FOLDER_TEXT)
    while start >= 0:
        end = start + len(DATA_FOLDER_TEXT)
        if end == len(folded) or not _is_name_char(folded[end]):
            return DATA_FOLDER_TEXT
        start = folded.find(DATA_FOLDER_TEXT, start + 1)
    for name in UNIQUE_FILE_NAMES:
        if name in folded:
            return name
    return None


def decoded_forms(text: str) -> list[str]:
    """The text and its percent-decoded forms (``%20`` and the like, up to three times)."""
    forms = [text]
    while "%" in forms[-1] and len(forms) <= MAX_DECODING:
        decoded = unquote(forms[-1])
        if decoded == forms[-1]:
            break
        forms.append(decoded)
    return forms


def check_data_text(text: str, matched: str | None = None) -> None:
    """Refuses data folder text in the text as written or percent-decoded."""
    if any(find_data_text(form) is not None for form in decoded_forms(text)):
        raise Refusal(Route.DATA_TEXT, text if matched is None else matched)


def check_script_text(text: str) -> None:
    """Inline scripts, here-documents and text fed to an interpreter (BR2.7)."""
    check_data_text(text)
    if PACKAGE_RE.search(text) or OWN_TEXT_RE.search(text):
        raise Refusal(Route.OWN_COMMAND, text)
    folded = text.casefold()
    if COMMAND_NAME in folded and any(word in folded for word in KEYCHAIN_WORDS):
        raise Refusal(Route.KEYCHAIN, text)


# Path tools (BR1.1 to BR1.5)


def check_path(path: str, base: str, ctx: Context) -> None:
    target = absolute(expand_home(path, ctx.home), base)
    try:
        protected = ctx.resolver.is_protected(target)
    except Unresolvable as error:
        raise Refusal(Route.DATA_PATH, path) from error
    if protected:
        raise Refusal(Route.DATA_PATH, path)


def check_pattern(pattern: str, base: str, ctx: Context) -> None:
    try:
        alternatives = patterns.expand_braces(pattern)
    except ValueError as error:
        raise Refusal(Route.UNPARSABLE, pattern) from error
    for alternative in alternatives:
        target = absolute(expand_home(alternative, ctx.home), base)
        if patterns.can_reach(target, ctx.protected.data_dir):
            raise Refusal(Route.DATA_PATH, pattern)
        check_path(target, base, ctx)


def check_file_tool(call: GuardCall, ctx: Context) -> None:
    check_path(call.tool_input[REQUIRED[call.tool_name]], ctx.cwd, ctx)


def check_search_tool(call: GuardCall, ctx: Context) -> None:
    folder = call.tool_input.get("path") or ctx.cwd
    check_path(folder, ctx.cwd, ctx)
    base = absolute(expand_home(folder, ctx.home), ctx.cwd)
    if call.tool_name == "Glob":
        check_pattern(call.tool_input["pattern"], base, ctx)
    glob = call.tool_input.get("glob")
    if glob and "/" in glob:
        check_pattern(glob, base, ctx)


def check_fetch_tool(call: GuardCall, ctx: Context) -> None:
    url = call.tool_input["url"]
    if url[:5].casefold() != "file:":
        return
    path = unquote(urlsplit(url).path)
    if not path:
        raise Refusal(Route.DATA_PATH, url)
    check_path(path, "/", ctx)


# Subagents (BR3.1)


def check_subagent(call: GuardCall, ctx: Context) -> None:
    for name in ("prompt", "description"):
        text = call.tool_input.get(name)
        if not text:
            continue
        check_data_text(text)
        match = OWN_TEXT_RE.search(text)
        if match:
            raise Refusal(Route.OWN_COMMAND, match.group())


# The shell (BR2.1 to BR2.8)


def check_bash(call: GuardCall, ctx: Context) -> None:
    state = ShellState.start(ctx.environ, home=ctx.home, cwd=ctx.cwd)
    check_line(call.tool_input["command"], state, ctx, 0)


def check_line(text: str, state: ShellState, ctx: Context, depth: int) -> None:
    if depth > MAX_NESTING:
        raise Refusal(Route.UNPARSABLE, text)
    raw = text.replace("\\", "").replace("'", "").replace('"', "")
    check_data_text(raw, text)
    try:
        commands = expand_line(text, state, depth)
    except Unparsable as error:
        raise Refusal(Route.UNPARSABLE, text) from error
    texts: list[str] = []
    for command in commands:
        for value in _texts(command):
            check_data_text(value)
            texts.append(value)
    check_data_text(" ".join(texts), text)
    for command in commands:
        check_command(command, ctx)


def _texts(command: Command) -> list[str]:
    words = [*command.words, *(value for _name, value in command.assignments)]
    for redirect in command.redirects:
        if redirect.target is not None:
            words.append(redirect.target)
        if redirect.heredoc is not None:
            words.append(redirect.heredoc.text)
    return [word.render("\0") for word in words]


def _program(word: Word) -> str:
    return _program_name(word.render())


@lru_cache(maxsize=1024)
def _program_name(text: str) -> str:
    name = _base_name(text)
    return "python" if PYTHON_RE.fullmatch(name) else name


@lru_cache(maxsize=1024)
def _base_name(text: str) -> str:
    return posixpath.basename(text).casefold()


@dataclass
class Effective:
    """Where the real program sits after wrappers such as sudo, env or uvx."""

    index: int
    wrapped: bool = False
    values: set[int] = field(default_factory=set)
    nested: list[Word] = field(default_factory=list)
    wrappers: list[str] = field(default_factory=list)


def effective(words: list[Word]) -> Effective:
    found = Effective(0)
    while found.index < len(words) and words[found.index].known:
        name, width = _wrapper_at(words, found.index)
        if name is None:
            break
        found.wrapped = True
        found.wrappers.append(name)
        found.index = _skip_options(words, found.index + width, name, found)
    return found


def _wrapper_at(words: list[Word], index: int) -> tuple[str | None, int]:
    name = _program(words[index])
    following = [w.render() for w in words[index + 1 : index + 3] if w.known]
    if name == "uv" and following[:2] == ["tool", "run"]:
        return "uv tool run", 3
    if name in ("uv", "pipx") and following[:1] == ["run"]:
        return f"{name} run", 2
    return (name, 1) if name in WRAPPERS else (None, 0)


def _skip_options(words: list[Word], index: int, name: str, found: Effective) -> int:
    values = WRAPPERS[name]
    while index < len(words) and words[index].known:
        text = words[index].render()
        if text == "--":
            return index + 1
        if name == "env" and re.match(r"[A-Za-z_][A-Za-z0-9_]*=", text):
            index += 1
            continue
        if name == "env" and (text.startswith("-S") or text.startswith("--split-string")):
            if text in ("-S", "--split-string") and index + 1 < len(words):
                found.nested.append(words[index + 1])
                found.values.add(index + 1)
                index += 2
            else:
                attached = text.split("=", 1)[1] if text.startswith("--") else text[2:]
                found.nested.append(known_word(attached))
                index += 1
            continue
        if not text.startswith("-") or text == "-":
            break
        if text in values:
            found.values.add(index + 1)
            index += 2
            continue
        index += 1
    if name == "timeout" and index < len(words):
        found.values.add(index)
        index += 1
    return index


def check_command(command: Command, ctx: Context) -> None:
    words = command.words
    found = effective(words)
    check_own_command(words, found)
    program = _program(words[found.index]) if found.index < len(words) else None
    if program == "security" and words[found.index].known:
        check_keychain(words[found.index + 1 :])
    check_code(command, found, ctx)
    check_undecidable(command, found)
    if program == "ln":
        check_link(command, found, ctx)
    check_paths(command, found, ctx)
    if program == "find":
        _check_find_exec(command, found, ctx)


def _check_find_exec(command: Command, found: Effective, ctx: Context) -> None:
    words = command.words
    index = found.index + 1
    while index < len(words):
        if words[index].known and words[index].render() in FIND_EXEC:
            end = index + 1
            while end < len(words) and not (words[end].known and words[end].render() in (";", "+")):
                end += 1
            inner = Command(
                words=words[index + 1 : end],
                assignments=[],
                redirects=[],
                base=command.base,
                depth=command.depth,
                state=command.state,
            )
            if inner.words:
                check_command(inner, ctx)
            index = end
        index += 1


# BR2.8: the tool's own commands


def is_tool_command(word: Word) -> bool:
    if not word.known:
        return False
    name = _base_name(word.render())
    return name == COMMAND_NAME or any(
        name.startswith(COMMAND_NAME + mark) for mark in ("@", "==", "[")
    )


def _refused_after(rest: list[Word]) -> bool:
    for word in rest:
        if not word.known:
            return True
        text = word.render()
        if text in ALLOWED_SUBCOMMANDS:
            return False
        if not text.startswith("-"):
            return True
    return False


def _is_hooks_run(word: Word) -> bool:
    text = word.render().casefold()
    return word.known and (text == "hooks/run" or text.endswith("/hooks/run"))


def _allowed_written(rest: list[Word]) -> bool:
    """True when an allowed subcommand (status, help, version) is written after the tool."""
    return any(word.known and word.render() in ALLOWED_SUBCOMMANDS for word in rest)


def check_own_command(words: list[Word], found: Effective) -> None:
    index = found.index
    if index < len(words):
        program = words[index]
        if is_tool_command(program) and _refused_after(words[index + 1 :]):
            raise Refusal(Route.OWN_COMMAND, " ".join(w.render() for w in words[index:]))
        # xargs appends words from standard input: only a subcommand written out and allowed
        # keeps the tool from running whatever arrives there.
        if (
            is_tool_command(program)
            and "xargs" in found.wrappers
            and not _allowed_written(words[index + 1 :])
        ):
            raise Refusal(Route.OWN_COMMAND, " ".join(w.render() for w in words[index:]))
        if _is_hooks_run(program):
            raise Refusal(Route.OWN_COMMAND, program.render())
        name = _program(program) if program.known else ""
        if name == "python":
            module = _python_module(words[index + 1 :])
            if module is not None and (
                not module.known
                or module.render() == PACKAGE_MODULE
                or module.render().startswith(PACKAGE_MODULE + ".")
            ):
                raise Refusal(Route.OWN_COMMAND, module.render())
        if name in SHELLS:
            script = inline_info(name, words[index + 1 :]).file_arg
            if script is not None and _is_hooks_run(script):
                raise Refusal(Route.OWN_COMMAND, script.render())
    if found.wrapped:
        for position in range(1, len(words)):
            if position in found.values:
                continue
            if is_tool_command(words[position]) and _refused_after(words[position + 1 :]):
                raise Refusal(Route.OWN_COMMAND, words[position].render())


def _python_module(rest: list[Word]) -> Word | None:
    index = 0
    while index < len(rest):
        word = rest[index]
        if not word.known:
            return None
        text = word.render()
        if not text.startswith("-") or text in ("-", "--"):
            return None
        if text in ("-W", "-X"):
            index += 2
            continue
        flags = text[1:]
        if "c" in flags.split("m", 1)[0]:
            return None
        if "m" in flags and not text.startswith("--"):
            attached = flags.split("m", 1)[1]
            if attached:
                return known_word(attached)
            return rest[index + 1] if index + 1 < len(rest) else None
        index += 1
    return None


# BR2.6: Keychain commands


def check_keychain(rest: list[Word]) -> None:
    for word in rest:
        if not word.known or COMMAND_NAME in word.render().casefold():
            raise Refusal(Route.KEYCHAIN, word.render("?"))
    texts = [w.render() for w in rest]
    subcommand = next((t for t in texts if not t.startswith("-")), None)
    options = texts[: texts.index(subcommand)] if subcommand else texts
    if subcommand is None or "-i" in options:
        raise Refusal(Route.KEYCHAIN, "security " + " ".join(texts))
    if subcommand in ("dump-keychain", "export"):
        raise Refusal(Route.KEYCHAIN, subcommand)
    if subcommand in ("find-generic-password", "find-internet-password") and not any(
        t.startswith("-s") for t in texts
    ):
        raise Refusal(Route.KEYCHAIN, " ".join(texts))


# BR2.7 and BR2.5: code positions


@dataclass
class Inline:
    kind: str | None
    scripts: list[Word] = field(default_factory=list)
    file_arg: Word | None = None
    reads_stdin: bool = False


def inline_info(name: str, args: list[Word]) -> Inline:
    if name in SHELLS:
        return _shell_inline(args)
    if name == "python":
        return _cluster_inline("python", args, "c")
    if name in CLUSTER:
        return _cluster_inline(name, args, CLUSTER[name])
    if name in INLINE:
        return _option_inline(name, args, INLINE[name])
    return Inline(None)


def _shell_inline(args: list[Word]) -> Inline:
    info = Inline("shell")
    dash_c = stdin_flag = False
    index = 0
    while index < len(args):
        word = args[index]
        text = word.render() if word.known else ""
        if word.known and text in ("-o", "+o", "-O", "+O"):
            index += 2
            continue
        if word.known and text == "--":
            index += 1
            break
        if word.known and (text.startswith("-") or text.startswith("+")) and len(text) > 1:
            if text.startswith("-") and not text.startswith("--"):
                dash_c = dash_c or "c" in text
                stdin_flag = stdin_flag or "s" in text
            index += 1
            continue
        break
    if index < len(args):
        if dash_c:
            info.scripts.append(args[index])
        elif not stdin_flag:
            info.file_arg = args[index]
    info.reads_stdin = not dash_c and info.file_arg is None
    return info


def _cluster_inline(kind: str, args: list[Word], letters: str) -> Inline:
    info = Inline(kind)
    index = 0
    while index < len(args):
        word = args[index]
        if not word.known:
            info.file_arg = word
            return info
        text = word.render()
        if text == "-":
            info.reads_stdin = True
            return info
        if not text.startswith("-") or text == "--":
            if text == "--":
                index += 1
                if index < len(args):
                    info.file_arg = args[index]
                return info
            info.file_arg = word
            return info
        if kind == "python" and text in ("-W", "-X"):
            index += 2
            continue
        flags = text[1:]
        if kind == "python" and "m" in flags.split("c", 1)[0]:
            return info
        position = min((flags.find(c) for c in letters if c in flags), default=-1)
        if position >= 0 and not text.startswith("--"):
            attached = flags[position + 1 :]
            if attached:
                info.scripts.append(known_word(attached))
            elif index + 1 < len(args):
                info.scripts.append(args[index + 1])
                index += 1
        index += 1
    info.reads_stdin = not info.scripts
    return info


def _option_inline(kind: str, args: list[Word], options: tuple[str, ...]) -> Inline:
    info = Inline(kind)
    index = 0
    while index < len(args):
        word = args[index]
        if not word.known:
            info.file_arg = word
            return info
        text = word.render()
        if text in options and index + 1 < len(args):
            info.scripts.append(args[index + 1])
            index += 2
            continue
        option = next((o for o in options if o.startswith("--") and text.startswith(o + "=")), None)
        if option is not None:
            info.scripts.append(known_word(text.split("=", 1)[1]))
        elif text == "-":
            info.reads_stdin = True
            return info
        elif not text.startswith("-"):
            info.file_arg = word
            return info
        index += 1
    info.reads_stdin = not info.scripts
    return info


def _join(words: list[Word]) -> Word:
    """The words as the single text a command such as echo prints."""
    pieces: list[Piece] = []
    for position, word in enumerate(words):
        if position:
            pieces.extend(known_word(" ").pieces)
        pieces.extend(word.pieces)
    return Word(tuple(pieces))


def stdin_texts(command: Command) -> list[Word]:
    """Here-documents of this command, and literal text piped into it."""
    texts = [r.heredoc.text for r in command.redirects if r.heredoc is not None]
    before = command.piped_from
    if before is None or not before.words:
        return texts
    start = effective(before.words).index
    if start >= len(before.words) or not before.words[start].known:
        return texts
    name = _program(before.words[start])
    args = before.words[start + 1 :]
    if name in ("echo", "printf"):
        while args and args[0].known and args[0].render() in ("-n", "-e", "-E"):
            args = args[1:]
        texts.append(_join(args))
    elif name == "cat" and not args:
        texts.extend(r.heredoc.text for r in before.redirects if r.heredoc is not None)
    return texts


def _check_code_text(word: Word, kind: str, command: Command, ctx: Context) -> None:
    if not word.known:
        raise Refusal(Route.UNDECIDABLE, word.render("?"))
    text = word.render()
    check_script_text(text)
    if kind == "shell":
        check_line(text, command.state.copy(), ctx, command.depth + 1)


def check_code(command: Command, found: Effective, ctx: Context) -> None:
    words = command.words
    for nested in found.nested:
        _check_code_text(nested, "shell", command, ctx)
    if found.index >= len(words) or not words[found.index].known:
        return
    name = _program(words[found.index])
    args = words[found.index + 1 :]
    if name == "eval":
        _check_code_text(_join(args), "shell", command, ctx)
        return
    info = inline_info(name, args)
    if info.kind is None:
        return
    for script in info.scripts:
        _check_code_text(script, info.kind, command, ctx)
    if info.file_arg is not None and not info.file_arg.known:
        raise Refusal(Route.UNDECIDABLE, info.file_arg.render("?"))
    if info.reads_stdin:
        for text in stdin_texts(command):
            _check_code_text(text, info.kind, command, ctx)


def pattern_indexes(words: list[Word], start: int) -> set[int]:
    """Arguments that are patterns, not paths: find's -name family and grep's pattern."""
    if start >= len(words) or not words[start].known:
        return set()
    name = _program(words[start])
    found: set[int] = set()
    if name == "find":
        for index in range(start + 1, len(words) - 1):
            if words[index].known and words[index].render() in FIND_PATTERN_OPTIONS:
                found.add(index + 1)
    elif name in GREP_PROGRAMS:
        values = RG_VALUE_OPTIONS if name == "rg" else GREP_VALUE_OPTIONS
        texts = [w.render() if w.known else "" for w in words]
        explicit = False
        index = start + 1
        while index < len(words):
            text = texts[index]
            if text in ("-e", "--regexp"):
                found.add(index + 1)
                explicit = True
            if text in ("-f", "--file") or text.startswith(("-e", "--regexp=", "-f", "--file=")):
                explicit = True
            index += 2 if text in values else 1
        if not explicit:
            index = start + 1
            while index < len(words):
                text = texts[index]
                if text == "--":
                    found.add(index + 1)
                    break
                if text in values:
                    index += 2
                    continue
                if not text.startswith("-") or not words[index].known:
                    found.add(index)
                    break
                index += 1
    return found


def _names_a_data_file(word: Word) -> bool:
    for part in word.render().casefold().split("/"):
        if part == "..":
            return True
        if part in DATA_FILE_NAMES or part.startswith("activity.log"):
            return True
        if word.has_glob and any(fnmatchcase(name, part) for name in DATA_FILE_NAMES):
            return True
    return False


def _check_position(word: Word, command: Command) -> None:
    """A word used as a file, a program or code (BR2.5, as narrowed by the plan)."""
    if not word.known:
        raise Refusal(Route.UNDECIDABLE, word.render("?"))
    text = word.render()
    if command.base is None and not text.startswith("/") and _names_a_data_file(word):
        raise Refusal(Route.UNDECIDABLE, text)


def check_undecidable(command: Command, found: Effective) -> None:
    for redirect in command.redirects:
        if redirect.target is not None:
            _check_position(redirect.target, command)
    words = command.words
    if not words or command.cond:
        return
    for index in {0, found.index}:
        if index < len(words):
            _check_position(words[index], command)
    if found.index >= len(words):
        return
    name = _program(words[found.index])
    args = words[found.index + 1 :]
    if name in ("eval", "source", "."):
        for word in args:
            _check_position(word, command)
        return
    if name in SHELLS:
        script = inline_info(name, args).file_arg
        if script is not None:
            _check_position(script, command)
    if name not in FILE_PROGRAMS:
        return
    skip = pattern_indexes(words, found.index)
    for index in range(found.index + 1, len(words)):
        word = words[index]
        if index in skip or (word.known and word.render().startswith("-")):
            continue
        _check_position(word, command)


# ln: a link made now and read later, in this line or the next call


def check_link(command: Command, found: Effective, ctx: Context) -> None:
    """Refuses ``ln`` (symbolic or hard) whose source is the data folder, lies in it, or is a
    folder holding it. A relative source of a symbolic link is read from the link's folder, so
    it is checked against that folder and the working folder both."""
    words = command.words[found.index + 1 :]
    options = [w.render() for w in words if w.known and w.render().startswith("-")]
    args = [w for w in words if not (w.known and w.render().startswith("-"))]
    if not args or not all(w.known for w in args):
        return  # nothing to link, or an unknown word: refused as undecidable already
    symbolic = any("s" in option for option in options if not option.startswith("--")) or (
        "--symbolic" in options
    )
    sources = args[:-1] if len(args) > 1 else args
    folders: list[str | None] = [command.base]
    if symbolic and len(args) > 1:
        link = args[-1].render()
        if link.startswith("/") or command.base is not None:
            link_path = absolute(link, command.base or "/")
            folders += [link_path, posixpath.dirname(link_path)]
    for source in sources:
        text = source.render()
        if text.startswith("/"):
            _check_link_source(text, ctx)
            continue
        if command.base is None:
            raise Refusal(Route.UNDECIDABLE, text)
        for folder in folders:
            if folder is not None:
                _check_link_source(absolute(text, folder), ctx)


def _check_link_source(path: str, ctx: Context) -> None:
    try:
        reaches = ctx.resolver.holds_data(path)
    except Unresolvable as error:
        raise Refusal(Route.DATA_PATH, path) from error
    if reaches:
        raise Refusal(Route.DATA_PATH, path)


# BR2.3: words resolving into the data folder


def check_paths(command: Command, found: Effective, ctx: Context) -> None:
    skip = pattern_indexes(command.words, found.index)
    words = [w for i, w in enumerate(command.words) if i not in skip]
    words.extend(r.target for r in command.redirects if r.target is not None)
    for word in words:
        if not word.known:
            continue
        text = word.render()
        if word.has_glob:
            _check_shell_glob(word, command.base, ctx)
        targets = [text]
        if "=" in text[1:]:
            targets.append(text.split("=", 1)[1])
        for target in [form for t in targets for form in decoded_forms(t)]:
            if target[:5].casefold() == "file:":
                check_path(unquote(urlsplit(target).path) or "/", "/", ctx)
            elif target and (target.startswith("/") or command.base is not None):
                check_path(target, command.base or "/", ctx)


def _check_shell_glob(word: Word, base: str | None, ctx: Context) -> None:
    pattern = word.glob_pattern()
    if not pattern.startswith("/") and base is None:
        return
    target = absolute(pattern, base or "/")
    if patterns.can_match(target, ctx.protected.data_dir):
        raise Refusal(Route.DATA_PATH, word.render())
