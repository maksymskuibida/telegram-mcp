"""The guard's shell reader (BR2.1, BR2.4, NFR2.4).

Claude Code's Bash tool runs the person's login shell, zsh by default on macOS, sometimes bash.
This reader models the syntax the two share: simple commands split at ``; && || | &`` and
newlines, words with quotes, escapes and ``$'…'`` taken off, redirects, brace, tilde and
variable expansion, and command substitutions and here-documents as nested text. It reads a
line in two steps: a parser builds the syntax, and a walker expands it in order, with the
variables and the working folder the line sets along the way.

What the reader cannot know (a variable set nowhere, the output of a command) is kept as an
unknown value inside the word, so the rules can tell where it is used. Anything it does not
model, and any form where zsh and bash read the same text differently, raises Unparsable: the
guard refuses what it cannot read."""

from __future__ import annotations

import itertools
import posixpath
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace

MAX_RESULTS = 256
# All the expansion one decision may do, nested lines and loop values included. A line within
# the 64 KiB input limit needs far less; past this, the line is refused rather than read slowly.
MAX_WORK = 600_000
MAX_RANGE_TEXT = 32
GLOB_CHARS = "*?["
NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ASSIGNMENT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(\+?)=")
REDIRECT_RE = re.compile(r"(\d*)(&>>|&>|<<<|<<-|<<|<>|<&|>&|>>|>\||<|>)")
RANGE_RE = re.compile(r"(-?\d+)\.\.(-?\d+)|([A-Za-z])\.\.([A-Za-z])")
STEPPED_RANGE_RE = re.compile(r"[^.,]+\.\.[^.,]+\.\.[^.,]+")
META = frozenset(" \t\n;&|()<>")
OPERATORS = ("&&", "||", "|&", ";;", ";&", ";|", "|", ";", "&")
START_KEYWORDS = frozenset({"if", "then", "else", "elif", "do", "while", "until", "{", "!"})
END_KEYWORDS = frozenset({"fi", "done", "}"})
REFUSED_KEYWORDS = frozenset({"case", "esac", "select", "coproc", "in"})
DECLARATIONS = frozenset({"export", "declare", "typeset", "local", "readonly", "integer"})
TAINTING = frozenset({"source", ".", "eval"})
SPECIAL_KNOWN = {"$": "0", "?": "0"}
SPECIAL_UNKNOWN = frozenset("@*#!-0123456789")
ANSI_C = {
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "?": "?",
}


class Unparsable(Exception):
    """A line the reader cannot read, or reads differently from one of the two shells."""


# Syntax


@dataclass(frozen=True)
class Lit:
    text: str
    quoted: bool


@dataclass(frozen=True)
class Param:
    """``$NAME`` or ``${…}``. ``op`` is "" (plain), one of the default forms ``:- - := =``,
    or "?" for a form whose value cannot be known."""

    name: str
    op: str
    arg: tuple[Seg, ...] | None
    quoted: bool


@dataclass(frozen=True)
class Sub:
    """``$( )``, backticks, ``<( )`` or ``>( )``: a nested line whose output is unknown."""

    body: tuple[Node, ...]
    quoted: bool


@dataclass(frozen=True)
class Arith:
    quoted: bool


Seg = Lit | Param | Sub | Arith
RawWord = tuple[Seg, ...]


@dataclass(eq=False)
class HereDoc:
    delimiter: str
    quoted: bool
    strip_tabs: bool
    body: RawWord = ()


@dataclass(frozen=True)
class RawRedirect:
    op: str
    target: RawWord | None
    heredoc: HereDoc | None


@dataclass(eq=False)
class SimpleCmd:
    words: list[RawWord]
    redirects: list[RawRedirect]
    pipe_prev: SimpleCmd | None = None
    cond: bool = False


@dataclass(frozen=True)
class ForCmd:
    name: str
    words: tuple[RawWord, ...] | None


@dataclass(frozen=True)
class ArithCmd:
    names: frozenset[str]


@dataclass(frozen=True)
class Group:
    opening: bool


Node = SimpleCmd | ForCmd | ArithCmd | Group


# Expanded words


@dataclass(frozen=True)
class Piece:
    """Part of an expanded word. In a ``quoted`` piece, wildcard characters are plain text."""

    text: str
    quoted: bool
    unknown: bool = False


UNKNOWN_PIECE = Piece("", True, True)


@dataclass(frozen=True)
class Word:
    pieces: tuple[Piece, ...]

    @property
    def known(self) -> bool:
        return not any(piece.unknown for piece in self.pieces)

    def render(self, marker: str = "") -> str:
        return "".join(marker if piece.unknown else piece.text for piece in self.pieces)

    def fixed_part(self) -> str:
        """The text before the first unknown value."""
        text = []
        for piece in self.pieces:
            if piece.unknown:
                break
            text.append(piece.text)
        return "".join(text)

    @property
    def has_glob(self) -> bool:
        active = "".join(p.text for p in self.pieces if not p.quoted and not p.unknown)
        return "*" in active or "?" in active or ("[" in active and "]" in active)

    def glob_pattern(self) -> str:
        """The word as an fnmatch pattern: wildcards from quoted text match only themselves."""
        out = []
        for piece in self.pieces:
            if piece.quoted:
                out.append("".join(f"[{c}]" if c in GLOB_CHARS else c for c in piece.text))
            else:
                out.append(piece.text)
        return "".join(out)


def _word(pieces: Iterable[Piece]) -> Word:
    """Joins neighbouring known pieces of the same kind, in one pass."""
    merged: list[Piece] = []
    texts: list[str] = []
    quoted = True

    def close() -> None:
        if texts:
            merged.append(Piece("".join(texts), quoted))
            texts.clear()

    for piece in pieces:
        if piece.unknown:
            close()
            merged.append(piece)
        elif piece.text:
            if texts and piece.quoted != quoted:
                close()
            quoted = piece.quoted
            texts.append(piece.text)
    close()
    return Word(tuple(merged))


def known_word(text: str) -> Word:
    return Word((Piece(text, True),))


@dataclass(frozen=True)
class HereText:
    text: Word
    quoted: bool


@dataclass(frozen=True)
class Redirect:
    op: str
    target: Word | None
    heredoc: HereText | None = None


# The shell's state while a line is walked

Alternative = tuple[Piece, ...]
Value = tuple[Alternative, ...]
UNKNOWN_VALUE: Value = ((UNKNOWN_PIECE,),)


class _Unset:
    def __init__(self, anywhere: bool) -> None:
        self.anywhere = anywhere


NOT_SET = _Unset(anywhere=True)
EXPLICITLY_UNSET = _Unset(anywhere=False)


class Budget:
    """The expansion work left for one decision, shared by every copy of the state, so the
    guard's time stays bounded whatever the line holds."""

    def __init__(self, limit: int = MAX_WORK) -> None:
        self.left = limit

    def spend(self, amount: int) -> None:
        self.left -= amount
        if self.left < 0:
            raise Unparsable("the command is too large to read")


@dataclass
class ShellState:
    environ: Mapping[str, str]
    home: str
    base: str | None
    variables: dict[str, Value | None] = field(default_factory=dict)
    tainted: bool = False
    budget: Budget = field(default_factory=lambda: Budget())

    @classmethod
    def start(cls, environ: Mapping[str, str], home: str, cwd: str) -> ShellState:
        state = cls(environ=environ, home=home, base=cwd)
        state.variables["PWD"] = (known_word(cwd).pieces,)
        return state

    def copy(self) -> ShellState:
        return replace(self, variables=dict(self.variables))

    def lookup(self, name: str) -> Value | _Unset:
        if name in self.variables:
            value = self.variables[name]
            return EXPLICITLY_UNSET if value is None else value
        if self.tainted:
            return UNKNOWN_VALUE
        if name in self.environ:
            return (known_word(self.environ[name]).pieces,)
        return NOT_SET

    def set_known(self, name: str, text: str | None) -> None:
        self.variables[name] = UNKNOWN_VALUE if text is None else (known_word(text).pieces,)


@dataclass(eq=False)
class Command:
    """One expanded simple command, in the order the shell runs it."""

    words: list[Word]
    assignments: list[tuple[str, Word]]
    redirects: list[Redirect]
    base: str | None
    depth: int
    state: ShellState
    piped_from: Command | None = None
    cond: bool = False


def expand_line(text: str, state: ShellState, depth: int = 0) -> list[Command]:
    """Read ``text`` and expand it in order. ``state`` is changed by the line's assignments
    and ``cd``s. Raises Unparsable, also for nesting deeper than Python's stack allows."""
    state.budget.spend(len(text))
    try:
        nodes = _Parser(text).parse_script(closing=False)
        walker = _Walker(state, depth)
        walker.walk(nodes)
    except RecursionError as error:
        raise Unparsable("the command nests too deeply to read") from error
    return walker.out


# The parser


def _is_name(text: str) -> bool:
    return NAME_RE.fullmatch(text) is not None


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.pending: list[HereDoc] = []

    def peek(self, offset: int = 0) -> str:
        index = self.pos + offset
        return self.text[index] if index < len(self.text) else ""

    def at(self, prefix: str) -> bool:
        return self.text.startswith(prefix, self.pos)

    def skip_blanks(self) -> None:
        while True:
            char = self.peek()
            if char in (" ", "\t"):
                self.pos += 1
            elif char == "\\" and self.peek(1) == "\n":
                self.pos += 2
            elif char == "#":
                end = self.text.find("\n", self.pos)
                self.pos = len(self.text) if end < 0 else end
            else:
                return

    # Lists of commands

    def parse_script(self, closing: bool) -> list[Node]:
        nodes: list[Node] = []
        depth = 0
        last = "start"
        need = False
        empty = True  # nothing read since the last ( or the start
        prev: SimpleCmd | None = None
        carry: SimpleCmd | None = None
        while True:
            self.skip_blanks()
            char = self.peek()
            if char == "":
                if closing or depth or need or self.pending:
                    raise Unparsable("the line ends inside a construct that does not close")
                return nodes
            if char == "\n":
                self.pos += 1
                self.read_heredoc_bodies()
                if last == "command":
                    last = "start"
                if not need:
                    prev = carry = None
                continue
            if char == ")":
                if need or empty:
                    raise Unparsable("a ) with no command before it")
                self.pos += 1
                if depth:
                    depth -= 1
                    nodes.append(Group(opening=False))
                    last, prev, empty = "command", None, False
                    continue
                if closing:
                    if self.pending:
                        raise Unparsable("a here-document does not close")
                    return nodes
                raise Unparsable("a ) has no (")
            operator = self.read_operator()
            if operator is not None:
                if last != "command":
                    raise Unparsable(f"{operator} has no command before it")
                last = "start"
                need = operator in ("&&", "||", "|", "|&")
                carry = prev if operator in ("|", "|&") else None
                prev = None
                continue
            if char == "(":
                if last != "start":
                    raise Unparsable("a ( inside a command")
                if self.at("(("):
                    self.pos += 2
                    nodes.append(ArithCmd(self.read_arithmetic()))
                    last, need, prev, empty = "command", False, None, False
                    continue
                self.pos += 1
                depth += 1
                need, empty = False, True
                nodes.append(Group(opening=True))
                continue
            if last == "command" and not REDIRECT_RE.match(self.text, self.pos):
                raise Unparsable("two commands with no separator")
            node, last = self.parse_command(redirects_only=last == "command")
            need = need and node is None and last == "start"
            empty = False
            if isinstance(node, SimpleCmd):
                node.pipe_prev = carry
                prev, carry = node, None
            else:
                prev = None
            if node is not None:
                nodes.append(node)

    def read_operator(self) -> str | None:
        if self.at("&>"):
            return None
        for operator in OPERATORS:
            if self.at(operator):
                if operator in (";;", ";&", ";|"):
                    raise Unparsable(f"{operator} is not read")
                self.pos += len(operator)
                return operator
        return None

    def peek_plain_word(self) -> str | None:
        end = self.pos
        while end < len(self.text) and self.text[end] not in META:
            if self.text[end] in "'\"\\$`":
                return None
            end += 1
        return self.text[self.pos : end]

    def parse_command(self, redirects_only: bool) -> tuple[Node | None, str]:
        keyword = "" if redirects_only else (self.peek_plain_word() or "")
        if keyword in START_KEYWORDS or keyword in END_KEYWORDS:
            self.pos += len(keyword)
            return None, "command" if keyword in END_KEYWORDS else "start"
        if keyword in REFUSED_KEYWORDS:
            raise Unparsable(f"{keyword} is not read")
        if keyword == "for":
            self.pos += 3
            return self.parse_for(), "command"
        if keyword == "function":
            self.pos += len(keyword)
            self.skip_blanks()
            self.read_word()
            self.skip_blanks()
            self.read_function_parens()
            return None, "start"
        if keyword == "[[":
            self.pos += 2
            return self.parse_cond(), "command"
        simple = self.parse_simple(redirects_only)
        return simple, "start" if simple is None else "command"

    def parse_simple(self, redirects_only: bool) -> SimpleCmd | None:
        words: list[RawWord] = []
        redirects: list[RawRedirect] = []
        while True:
            self.skip_blanks()
            char = self.peek()
            if char in ("<", ">") and self.peek(1) == "(":
                words.append(self.read_word())
                continue
            match = REDIRECT_RE.match(self.text, self.pos)
            if match:
                self.pos = match.end()
                redirects.append(self.read_redirect(match.group(2)))
                continue
            if char == "" or char in META:
                if char == "(":
                    if len(words) == 1 and not redirects and self.is_plain_name(words[0]):
                        self.read_function_parens()
                        return None
                    raise Unparsable("a ( inside a command")
                break
            if redirects_only:
                raise Unparsable("a word after a closed group")
            words.append(self.read_word())
        if not words and not redirects:
            raise Unparsable("an empty command")
        return SimpleCmd(words, redirects)

    @staticmethod
    def is_plain_name(word: RawWord) -> bool:
        return (
            len(word) == 1
            and isinstance(word[0], Lit)
            and not word[0].quoted
            and (_is_name(word[0].text))
        )

    def read_function_parens(self) -> None:
        self.skip_blanks()
        if self.peek() == "(":
            self.pos += 1
            self.skip_blanks()
            if self.peek() != ")":
                raise Unparsable("a ( inside a command")
            self.pos += 1

    def parse_for(self) -> Node | None:
        self.skip_blanks()
        if self.at("(("):
            self.pos += 2
            return ArithCmd(self.read_arithmetic())
        name = self.peek_plain_word()
        if name is None or not _is_name(name):
            raise Unparsable("a for loop without a name")
        self.pos += len(name)
        self.skip_blanks()
        if self.peek_plain_word() != "in":
            return ForCmd(name, None)
        self.pos += 2
        words: list[RawWord] = []
        while True:
            self.skip_blanks()
            if self.peek() == "" or self.peek() in META:
                return ForCmd(name, tuple(words))
            words.append(self.read_word())

    def parse_cond(self) -> SimpleCmd:
        words: list[RawWord] = [(Lit("[[", False),)]
        while True:
            self.skip_blanks()
            char = self.peek()
            if char in ("", "\n", ";"):
                raise Unparsable("a [[ does not close")
            if self.peek_plain_word() == "]]":
                self.pos += 2
                words.append((Lit("]]", False),))
                return SimpleCmd(words, [], cond=True)
            if char in META:
                operator = self.text[self.pos : self.pos + 2]
                if operator not in ("&&", "||"):
                    operator = char
                self.pos += len(operator)
                words.append((Lit(operator, False),))
                continue
            words.append(self.read_word())

    def read_redirect(self, op: str) -> RawRedirect:
        if op == "<<<":
            raise Unparsable("a here-string is not read")
        self.skip_blanks()
        if self.peek() == "" or self.peek() in META:
            raise Unparsable(f"{op} has no target")
        target = self.read_word()
        if op in ("<<", "<<-"):
            if not all(isinstance(seg, Lit) for seg in target):
                raise Unparsable("a here-document delimiter with an expansion")
            delimiter = "".join(seg.text for seg in target if isinstance(seg, Lit))
            quoted = any(isinstance(seg, Lit) and seg.quoted for seg in target)
            heredoc = HereDoc(delimiter, quoted, strip_tabs=op == "<<-")
            self.pending.append(heredoc)
            return RawRedirect(op, None, heredoc)
        plain = _plain_text(target)
        if op in ("<&", ">&") and plain is not None and (plain == "-" or plain.isdigit()):
            return RawRedirect(op, None, None)
        return RawRedirect(op, target, None)

    def read_heredoc_bodies(self) -> None:
        for heredoc in self.pending:
            lines = []
            while True:
                if self.pos >= len(self.text):
                    raise Unparsable("a here-document does not close")
                end = self.text.find("\n", self.pos)
                stop = len(self.text) if end < 0 else end
                line = self.text[self.pos : stop]
                self.pos = stop + 1 if end >= 0 else stop
                if heredoc.strip_tabs:
                    line = line.lstrip("\t")
                if line == heredoc.delimiter:
                    break
                lines.append(line + "\n")
            body = "".join(lines)
            heredoc.body = (Lit(body, True),) if heredoc.quoted else _Parser(body).read_text()
        self.pending.clear()

    # Words

    def read_word(self) -> RawWord:
        segs: list[Seg] = []
        buffer: list[str] = []

        def flush() -> None:
            if buffer:
                segs.append(Lit("".join(buffer), False))
                buffer.clear()

        while True:
            char = self.peek()
            if char == "":
                break
            if char in META:
                if char in "<>" and self.peek(1) == "(":
                    flush()
                    self.pos += 2
                    segs.append(Sub(tuple(self.parse_script(closing=True)), False))
                    continue
                break
            if char == "\\":
                following = self.peek(1)
                self.pos += 2
                if following == "\n":
                    continue
                if following == "":
                    buffer.append("\\")
                    continue
                flush()
                segs.append(Lit(following, True))
            elif char == "'":
                flush()
                segs.append(Lit(self.read_single(), True))
            elif char == '"':
                flush()
                segs.extend(self.read_double())
            elif char == "$":
                result = self.read_dollar(quoted=False)
                if isinstance(result, str):
                    buffer.append(result)
                else:
                    flush()
                    segs.extend(result)
            elif char == "`":
                flush()
                segs.append(self.read_backtick(quoted=False))
            else:
                if char == "!":
                    self.check_history(in_double=False)
                buffer.append(char)
                self.pos += 1
        flush()
        if not segs:
            raise Unparsable("an empty word")
        return tuple(segs)

    def check_history(self, in_double: bool) -> None:
        following = self.peek(1)
        if following in ("", " ", "\t", "\n", "=", "(") or following in META:
            return
        if in_double and following == '"':
            return
        raise Unparsable("! may be read as history expansion")

    def read_single(self) -> str:
        end = self.text.find("'", self.pos + 1)
        if end < 0:
            raise Unparsable("a ' does not close")
        text = self.text[self.pos + 1 : end]
        self.pos = end + 1
        return text

    def read_double(self) -> list[Seg]:
        self.pos += 1
        segs: list[Seg] = [Lit("", True)]
        buffer: list[str] = []

        def flush() -> None:
            if buffer:
                segs.append(Lit("".join(buffer), True))
                buffer.clear()

        while True:
            char = self.peek()
            if char == "":
                raise Unparsable('a " does not close')
            if char == '"':
                self.pos += 1
                flush()
                return segs
            if char == "\\":
                following = self.peek(1)
                if following in ("$", "`", '"', "\\"):
                    buffer.append(following)
                    self.pos += 2
                elif following == "\n":
                    self.pos += 2
                else:
                    buffer.append("\\")
                    self.pos += 1
            elif char == "$":
                result = self.read_dollar(quoted=True)
                if isinstance(result, str):
                    buffer.append(result)
                else:
                    flush()
                    segs.extend(result)
            elif char == "`":
                flush()
                segs.append(self.read_backtick(quoted=True))
            else:
                if char == "!":
                    self.check_history(in_double=True)
                buffer.append(char)
                self.pos += 1

    def read_text(self) -> RawWord:
        """A here-document body with an unquoted delimiter: expansions, no quotes."""
        segs: list[Seg] = [Lit("", True)]
        buffer: list[str] = []
        while self.pos < len(self.text):
            char = self.peek()
            if char == "\\" and self.peek(1) in ("$", "`", "\\", "\n"):
                if self.peek(1) != "\n":
                    buffer.append(self.peek(1))
                self.pos += 2
            elif char in ("$", "`"):
                result = (
                    self.read_dollar(quoted=True)
                    if char == "$"
                    else [self.read_backtick(quoted=True)]
                )
                if isinstance(result, str):
                    buffer.append(result)
                    continue
                if buffer:
                    segs.append(Lit("".join(buffer), True))
                    buffer.clear()
                segs.extend(result)
            else:
                buffer.append(char)
                self.pos += 1
        if buffer:
            segs.append(Lit("".join(buffer), True))
        return tuple(segs)

    def read_backtick(self, quoted: bool) -> Sub:
        self.pos += 1
        inner: list[str] = []
        while True:
            char = self.peek()
            if char == "":
                raise Unparsable("a ` does not close")
            if char == "`":
                self.pos += 1
                break
            if char == "\\" and self.peek(1) in ("`", "\\", "$") + (('"',) if quoted else ()):
                inner.append(self.peek(1))
                self.pos += 2
                continue
            inner.append(char)
            self.pos += 1
        return Sub(tuple(_Parser("".join(inner)).parse_script(closing=False)), quoted)

    def read_dollar(self, quoted: bool) -> list[Seg] | str:
        following = self.peek(1)
        if following == "'" and not quoted:
            self.pos += 2
            return [Lit(self.read_ansi_c(), True)]
        if following == '"' and not quoted:
            self.pos += 1
            return self.read_double()
        if following == "(":
            if self.peek(2) == "(":
                self.pos += 3
                self.read_arithmetic()
                return [Arith(quoted)]
            self.pos += 2
            return [Sub(tuple(self.parse_script(closing=True)), quoted)]
        if following == "{":
            self.pos += 2
            return [self.read_braced(quoted)]
        if following in ("=", "~", "^") and (
            self.peek(2) == "{" or NAME_RE.match(self.peek(2)) is not None
        ):
            raise Unparsable(f"${following} is zsh-only")
        match = NAME_RE.match(self.text, self.pos + 1)
        if match:
            self.pos = match.end()
            if self.peek() == "[":
                raise Unparsable("$NAME[ is a subscript in zsh")
            return [Param(match.group(), "", None, quoted)]
        if following and (following in SPECIAL_UNKNOWN or following in SPECIAL_KNOWN):
            self.pos += 2
            return [Param(following, "", None, quoted)]
        self.pos += 1
        return "$"

    def read_ansi_c(self) -> str:
        out: list[str] = []
        while True:
            char = self.peek()
            if char == "":
                raise Unparsable("a $' does not close")
            self.pos += 1
            if char == "'":
                return "".join(out)
            if char != "\\":
                out.append(char)
                continue
            following = self.peek()
            self.pos += 1
            if following in ANSI_C:
                out.append(ANSI_C[following])
            elif following in "01234567" and following:
                out.append(self.read_code(following, "01234567", 3, 8))
            elif following == "x":
                out.append(self.read_code("", "0123456789abcdefABCDEF", 2, 16, "\\x"))
            elif following in ("u", "U", "c", ""):
                raise Unparsable(f"\\{following} in $'…' differs between the shells")
            else:
                out.append("\\" + following)

    def read_code(self, first: str, digits: str, limit: int, radix: int, empty: str = "") -> str:
        code = first
        while len(code) < limit and self.peek() and self.peek() in digits:
            code += self.peek()
            self.pos += 1
        if not code:
            return empty
        value = int(code, radix)
        if value == 0:
            raise Unparsable("a NUL in $'…' differs between the shells")
        return chr(value)

    def read_arithmetic(self) -> frozenset[str]:
        """The text of ``$(( ))`` or ``(( ))`` up to its closing ``))``; returns the names."""
        start = self.pos
        depth = 0
        while True:
            char = self.peek()
            if char == "":
                raise Unparsable("an arithmetic expression does not close")
            if char == "`" or (char == "$" and self.peek(1) == "(" and self.peek(2) != "("):
                raise Unparsable("a command inside an arithmetic expression")
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    if self.peek(1) != ")":
                        raise Unparsable("an arithmetic expression does not close")
                    names = frozenset(NAME_RE.findall(self.text[start : self.pos]))
                    self.pos += 2
                    return names
                depth -= 1
            self.pos += 1

    def read_braced(self, quoted: bool) -> Param:
        """``${…}`` after the ``${``."""
        char = self.peek()
        if char == "" or char in "(~=^+!":
            raise Unparsable("${" + char + " is not read")
        if char == "#" and self.peek(1) != "}":
            self.pos += 1
            return Param("#", "?", self.read_param_word(quoted), quoted)
        match = NAME_RE.match(self.text, self.pos)
        if match:
            name = match.group()
            self.pos = match.end()
        elif char.isdigit() or char in SPECIAL_UNKNOWN or char in SPECIAL_KNOWN:
            name = char
            self.pos += 1
        else:
            raise Unparsable("${" + char + " is not read")
        char, following = self.peek(), self.peek(1)
        if char == "}":
            self.pos += 1
            return Param(name, "", None, quoted)
        if char == ":" and following in ("-", "="):
            self.pos += 2
            return Param(name, ":" + following, self.read_param_word(quoted), quoted)
        if char in ("-", "="):
            self.pos += 1
            return Param(name, char, self.read_param_word(quoted), quoted)
        if char == ":" and following.isalpha():
            raise Unparsable("a zsh modifier")
        if char in ":[+?#%/":
            self.pos += 1
            return Param(name, "?", self.read_param_word(quoted), quoted)
        raise Unparsable("${" + name + char + " is not read")

    def read_param_word(self, quoted: bool) -> RawWord:
        """The word inside ``${NAME op word}``, up to the closing brace."""
        segs: list[Seg] = []
        buffer: list[str] = []

        def flush() -> None:
            if buffer:
                segs.append(Lit("".join(buffer), quoted))
                buffer.clear()

        while True:
            char = self.peek()
            if char == "":
                raise Unparsable("a ${ does not close")
            if char == "}":
                self.pos += 1
                flush()
                return tuple(segs)
            if char == "\\":
                following = self.peek(1)
                self.pos += 2
                if following != "\n":
                    flush()
                    segs.append(Lit(following, True))
            elif char == "'" and not quoted:
                flush()
                segs.append(Lit(self.read_single(), True))
            elif char == '"':
                flush()
                segs.extend(self.read_double())
            elif char == "$":
                result = self.read_dollar(quoted)
                if isinstance(result, str):
                    buffer.append(result)
                else:
                    flush()
                    segs.extend(result)
            elif char == "`":
                flush()
                segs.append(self.read_backtick(quoted))
            else:
                buffer.append(char)
                self.pos += 1


# Brace expansion over the characters of a word


@dataclass(frozen=True)
class _Char:
    char: str
    active: bool


_QUOTE_MARK = Lit("", True)
Atom = _Char | Seg


def _atoms(word: RawWord) -> list[Atom]:
    atoms: list[Atom] = []
    for seg in word:
        if isinstance(seg, Lit):
            if seg.quoted and not seg.text:
                atoms.append(_QUOTE_MARK)
            atoms.extend(_Char(c, not seg.quoted) for c in seg.text)
        else:
            atoms.append(seg)
    return atoms


def _active(atom: Atom, char: str) -> bool:
    return isinstance(atom, _Char) and atom.active and atom.char == char


def brace_expand(atoms: list[Atom], budget: Budget) -> list[list[Atom]]:
    pending = [atoms]
    done: list[list[Atom]] = []
    while pending:
        current = pending.pop(0)
        budget.spend(len(current))
        expanded = _expand_first_brace(current)
        if expanded is None:
            done.append(current)
        else:
            pending[:0] = expanded
        if len(done) + len(pending) > MAX_RESULTS:
            raise Unparsable("brace expansion gives more than 256 words")
    if len(done) > 1 and any(not result for result in done):
        raise Unparsable("an empty brace result differs between the shells")
    return done


def _expand_first_brace(atoms: list[Atom]) -> list[list[Atom]] | None:
    pairs, unclosed = _brace_pairs(atoms)
    if unclosed and len(atoms) > 1:
        raise Unparsable("a { does not close")
    for start in sorted(pairs):
        end, commas = pairs[start]
        if commas:
            if len(commas) + 1 > MAX_RESULTS:
                raise Unparsable("brace expansion gives more than 256 words")
            prefix, suffix = atoms[:start], atoms[end + 1 :]
            edges = [start, *commas, end]
            return [
                prefix + atoms[edges[i] + 1 : edges[i + 1]] + suffix for i in range(len(edges) - 1)
            ]
        items = _range_items(atoms[start + 1 : end]) if end - start <= MAX_RANGE_TEXT else None
        if items is not None:
            prefix, suffix = atoms[:start], atoms[end + 1 :]
            return [prefix + [_Char(c, True) for c in item] + suffix for item in items]
    return None


def _brace_pairs(atoms: list[Atom]) -> tuple[dict[int, tuple[int, list[int]]], list[int]]:
    """Each ``{`` with its matching ``}`` and its top-level commas, in one pass."""
    stack: list[tuple[int, list[int]]] = []
    pairs: dict[int, tuple[int, list[int]]] = {}
    for index, atom in enumerate(atoms):
        if _active(atom, "{"):
            stack.append((index, []))
        elif _active(atom, "}") and stack:
            start, commas = stack.pop()
            pairs[start] = (index, commas)
        elif _active(atom, ",") and stack:
            stack[-1][1].append(index)
    return pairs, [start for start, _commas in stack]


def _range_items(inner: list[Atom]) -> list[str] | None:
    if not all(isinstance(a, _Char) and a.active for a in inner):
        return None
    text = "".join(a.char for a in inner if isinstance(a, _Char))
    if STEPPED_RANGE_RE.fullmatch(text):
        raise Unparsable("a stepped brace range differs between the shells")
    match = RANGE_RE.fullmatch(text)
    if match is None:
        if ".." in text and re.fullmatch(r"[^.]+\.\.[^.]+", text):
            raise Unparsable("a brace range the shells read differently")
        return None
    if match.group(1) is not None:
        first, last = match.group(1), match.group(2)
        if any(len(n.lstrip("-")) > 1 and n.lstrip("-").startswith("0") for n in (first, last)):
            raise Unparsable("a zero-padded brace range differs between the shells")
        low, high = int(first), int(last)
        step = 1 if high >= low else -1
        if abs(high - low) + 1 > MAX_RESULTS:
            raise Unparsable("brace expansion gives more than 256 words")
        return [str(n) for n in range(low, high + step, step)]
    first, last = match.group(3), match.group(4)
    if first.islower() != last.islower():
        raise Unparsable("a brace range across letter cases differs between the shells")
    step = 1 if ord(last) >= ord(first) else -1
    return [chr(n) for n in range(ord(first), ord(last) + step, step)]


# The walker: expansion in order


class _Walker:
    def __init__(self, state: ShellState, depth: int, out: list[Command] | None = None) -> None:
        self.state = state
        self.depth = depth
        self.out: list[Command] = [] if out is None else out
        self.expanded: dict[int, Command] = {}

    def walk(self, nodes: Iterable[Node]) -> None:
        saved: list[ShellState] = []
        for node in nodes:
            if isinstance(node, Group):
                if node.opening:
                    saved.append(self.state)
                    self.state = self.state.copy()
                else:
                    self.state = saved.pop()
            elif isinstance(node, ArithCmd):
                for name in node.names:
                    self.state.variables[name] = UNKNOWN_VALUE
            elif isinstance(node, ForCmd):
                self.walk_for(node)
            else:
                self.walk_simple(node)

    def walk_for(self, node: ForCmd) -> None:
        if node.words is None:
            self.state.variables[node.name] = UNKNOWN_VALUE
            return
        values: list[Alternative] = []
        for raw in node.words:
            values.extend(word.pieces for word in self.expand_word(raw, "word"))
        if len(values) > MAX_RESULTS:
            raise Unparsable("a for list with more than 256 values")
        self.state.variables[node.name] = tuple(values) or ((),)

    def walk_simple(self, node: SimpleCmd) -> None:
        names = sorted(n for n in _names_used(node) if len(self._value_of(n)) > 1)
        if not names:
            self.emit(node)
            return
        originals = {n: self.state.variables[n] for n in names}
        choices = [originals[n] or ((),) for n in names]
        count = 1
        for choice in choices:
            count *= len(choice)
        if count > MAX_RESULTS:
            raise Unparsable("more than 256 combinations of loop values")
        for combination in itertools.product(*choices):
            for name, alternative in zip(names, combination, strict=True):
                self.state.variables[name] = (alternative,)
            self.emit(node)
        for name in names:
            current = self.state.variables.get(name)
            if current is not None and len(current) == 1 and current[0] in (originals[name] or ()):
                self.state.variables[name] = originals[name]

    def _value_of(self, name: str) -> Value:
        value = self.state.variables.get(name)
        return value if value is not None else ()

    def emit(self, node: SimpleCmd) -> None:
        state = self.state
        snapshot = state.copy()
        index = 0
        assignments: list[tuple[str, Word]] = []
        appends: set[str] = set()
        while index < len(node.words) and not node.cond:
            split = _split_assignment(node.words[index])
            if split is None:
                break
            name, append, value = split
            assignments.append((name, self.expand_value(value)))
            if append:
                appends.add(name)
            index += 1
        words: list[Word] = []
        program = _plain_text(node.words[index]) if index < len(node.words) else None
        mode = "cond" if node.cond else "word"
        for raw in node.words[index:]:
            split = _split_assignment(raw) if program in DECLARATIONS else None
            if split is not None:
                name, _append, value = split
                expanded = self.expand_value(value)
                assignments.append((name, expanded))
                words.append(_word((Piece(name + "=", True), *expanded.pieces)))
            else:
                words.extend(self.expand_word(raw, mode))
        redirects = [self.expand_redirect(r) for r in node.redirects]
        command = Command(
            words=words,
            assignments=assignments,
            redirects=redirects,
            base=state.base,
            depth=self.depth,
            state=snapshot,
            piped_from=None if node.pipe_prev is None else self.expanded.get(id(node.pipe_prev)),
            cond=node.cond,
        )
        self.expanded[id(node)] = command
        self.out.append(command)
        if index == len(node.words) or program in DECLARATIONS:
            for assigned, expanded_value in assignments:
                self.assign(assigned, expanded_value, assigned in appends)
        if words:
            self.apply_effects(words)

    def assign(self, name: str, value: Word, append: bool) -> None:
        if append:
            current = self.state.lookup(name)
            if isinstance(current, _Unset):
                current = ((),)
            if len(current) != 1:
                self.state.variables[name] = UNKNOWN_VALUE
                return
            value = _word((*current[0], *value.pieces))
        self.state.variables[name] = (value.pieces,)

    def apply_effects(self, words: list[Word]) -> None:
        program = words[0].render() if words[0].known else None
        args = words[1:]
        if program in ("cd", "pushd", "popd", "chdir"):
            self.change_folder(program, args)
        elif program == "unset":
            for word in args:
                if word.known and _is_name(word.render()):
                    self.state.variables[word.render()] = None
        elif program in ("read", "mapfile", "readarray", "getopts", "printf"):
            names = [w.render() for w in args if w.known and _is_name(w.render())]
            if program == "printf":
                names = names[:1] if args and args[0].render() == "-v" else []
            for name in names or (["REPLY", "MAPFILE"] if program != "printf" else []):
                self.state.variables[name] = UNKNOWN_VALUE
        elif program in TAINTING:
            # A sourced file or evaluated text can set any variable and change folder.
            self.state.tainted = True
            self.state.variables.clear()
            self.state.base = None

    def change_folder(self, program: str, args: list[Word]) -> None:
        state = self.state
        operands = [w for w in args if not (w.known and w.render() in ("-L", "-P", "-e", "-@"))]
        if operands and operands[0].known and operands[0].render() == "--":
            operands = operands[1:]
        previous = state.lookup("PWD")
        new: str | None = None
        if program != "popd" and len(operands) == 1 and operands[0].known:
            target = operands[0].render()
            if target.startswith("/"):
                new = posixpath.normpath(target)
            elif target != "-" and state.base is not None:
                new = posixpath.normpath(posixpath.join(state.base, target))
        state.base = new
        state.variables["OLDPWD"] = UNKNOWN_VALUE if isinstance(previous, _Unset) else previous
        state.set_known("PWD", new)

    # Expansion

    def expand_redirect(self, redirect: RawRedirect) -> Redirect:
        if redirect.heredoc is not None:
            text = self.expand_word(redirect.heredoc.body, "heredoc")[0]
            return Redirect(redirect.op, None, HereText(text, redirect.heredoc.quoted))
        if redirect.target is None:
            return Redirect(redirect.op, None)
        targets = self.expand_word(redirect.target, "word")
        if len(targets) != 1:
            raise Unparsable("a redirect target that is not one word")
        return Redirect(redirect.op, targets[0])

    def expand_value(self, raw: RawWord) -> Word:
        atoms = _atoms(raw)
        atoms = self.expand_tilde(atoms, assignment=True)
        alternatives = self.expand_atoms(atoms, quoted_context=True)
        if len(alternatives) != 1:
            return Word((UNKNOWN_PIECE,))
        return _word(alternatives[0])

    def expand_word(self, raw: RawWord, mode: str) -> list[Word]:
        atoms = _atoms(raw)
        if mode == "heredoc":
            alternatives = self.expand_atoms(atoms, quoted_context=True)
            return [_word(alternatives[0]) if len(alternatives) == 1 else Word((UNKNOWN_PIECE,))]
        results = [atoms] if mode == "cond" else brace_expand(atoms, self.state.budget)
        words: list[Word] = []
        for result in results:
            if mode == "word":
                _check_zsh_forms(result)
            had_quotes = any(
                (isinstance(a, _Char) and not a.active) or a is _QUOTE_MARK for a in result
            )
            expanded = self.expand_tilde(result, assignment=False)
            for pieces in self.expand_atoms(expanded, quoted_context=mode == "cond"):
                word = _word(pieces)
                if (
                    not had_quotes
                    and word.known
                    and not word.render()
                    and pieces_from_expansion(result)
                ):
                    continue
                words.append(word)
        return words

    def expand_tilde(self, atoms: list[Atom], assignment: bool) -> list[Atom]:
        """A leading ``~``, ``~/…``, ``~+``, ``~-`` or ``~user``. In an assignment, a ``~``
        after ``:`` is refused: the shells expand it differently."""
        if assignment and any(
            _active(atoms[i], "~") and _active(atoms[i - 1], ":") for i in range(1, len(atoms))
        ):
            raise Unparsable("~ after : in an assignment differs between the shells")
        if not atoms or not _active(atoms[0], "~"):
            return atoms
        end = 1
        prefix: list[str] = []
        while end < len(atoms):
            atom = atoms[end]
            if not isinstance(atom, _Char) or not atom.active or atom.char == "/":
                break
            prefix.append(atom.char)
            end += 1
        if end < len(atoms) and not _active(atoms[end], "/"):
            return atoms
        return [self.tilde_value("".join(prefix)), *atoms[end:]]

    def tilde_value(self, prefix: str) -> Atom:
        state = self.state
        if prefix == "":
            value = state.lookup("HOME")
            if isinstance(value, _Unset):
                return Lit(state.home, True)
        elif prefix == "+":
            value = state.lookup("PWD")
        elif prefix == "-":
            value = state.lookup("OLDPWD")
        elif prefix in _user_names(state):
            return Lit(state.home, True)
        else:
            return Arith(True)
        if isinstance(value, _Unset) or len(value) != 1 or any(p.unknown for p in value[0]):
            return Arith(True)
        return Lit("".join(p.text for p in value[0]), True)

    def expand_atoms(self, atoms: list[Atom], quoted_context: bool) -> list[list[Piece]]:
        self.state.budget.spend(len(atoms))
        alternatives: list[list[Piece]] = [[]]
        for atom in atoms:
            if isinstance(atom, _Char):
                piece = Piece(atom.char, not atom.active or quoted_context)
                for alternative in alternatives:
                    alternative.append(piece)
            elif isinstance(atom, Lit):
                for alternative in alternatives:
                    alternative.append(Piece(atom.text, True))
            elif isinstance(atom, Param):
                options = self.expand_param(atom, quoted_context)
                alternatives = [a + list(option) for a in alternatives for option in options]
                if len(alternatives) > MAX_RESULTS:
                    raise Unparsable("more than 256 combinations of values")
                self.state.budget.spend(len(alternatives) * len(atoms))
            elif isinstance(atom, Sub):
                _Walker(self.state.copy(), self.depth + 1, self.out).walk(atom.body)
                for alternative in alternatives:
                    alternative.append(UNKNOWN_PIECE)
            else:
                for alternative in alternatives:
                    alternative.append(UNKNOWN_PIECE)
        return alternatives

    def expand_param(self, param: Param, quoted_context: bool) -> Value:
        if param.op == "?":
            self.walk_nested(param.arg)
            return UNKNOWN_VALUE
        if param.name in SPECIAL_KNOWN:
            return (known_word(SPECIAL_KNOWN[param.name]).pieces,)
        value = UNKNOWN_VALUE if param.name in SPECIAL_UNKNOWN else self.state.lookup(param.name)
        if param.op:
            value = self.default_value(param, value, quoted_context)
        elif isinstance(value, _Unset):
            value = UNKNOWN_VALUE if value.anywhere else ((),)
        if not (param.quoted or quoted_context):
            _check_unquoted(value)
        return value

    def default_value(self, param: Param, value: Value | _Unset, quoted_context: bool) -> Value:
        if not isinstance(value, _Unset) and any(p.unknown for a in value for p in a):
            self.walk_nested(param.arg)
            return UNKNOWN_VALUE
        empty = not isinstance(value, _Unset) and all(not "".join(p.text for p in a) for a in value)
        if isinstance(value, _Unset) or (param.op.startswith(":") and empty):
            atoms = self.expand_tilde(_atoms(param.arg or ()), assignment=False)
            options = self.expand_atoms(atoms, quoted_context=param.quoted or quoted_context)
            result: Value = tuple(_word(option).pieces for option in options)
            if param.op.endswith("="):
                self.state.variables[param.name] = result
            return result
        return value

    def walk_nested(self, segs: tuple[Seg, ...] | None) -> None:
        for seg in segs or ():
            if isinstance(seg, Sub):
                _Walker(self.state.copy(), self.depth + 1, self.out).walk(seg.body)
            elif isinstance(seg, Param):
                self.walk_nested(seg.arg)


def pieces_from_expansion(atoms: list[Atom]) -> bool:
    return any(isinstance(atom, Param | Sub | Arith) for atom in atoms) or not atoms


def _check_unquoted(value: Value) -> None:
    for alternative in value:
        for piece in alternative:
            if piece.unknown:
                continue
            if any(c.isspace() for c in piece.text) or (
                piece.quoted and any(c in GLOB_CHARS for c in piece.text)
            ):
                raise Unparsable("an unquoted value with spaces or wildcards splits differently")


def _check_zsh_forms(atoms: list[Atom]) -> None:
    active = [(i, a.char) for i, a in enumerate(atoms) if isinstance(a, _Char) and a.active]
    chars = [c for _, c in active]
    if chars and active[0][0] == 0 and chars[0] == "=" and len(atoms) > 1:
        raise Unparsable("=word is expanded by zsh")
    glob = "*" in chars or "?" in chars or ("[" in chars and "]" in chars)
    if glob and any(c in "^#" or (c == "~" and i > 0) for i, c in active):
        raise Unparsable("an extended wildcard is zsh-only")
    text = "".join(a.char if isinstance(a, _Char) else "\0" for a in atoms)
    match = ASSIGNMENT_RE.match(text)
    if match and re.search(r"[=:]~", text[match.end() - 1 :]):
        tilde_at = [i for i, c in active if c == "~"]
        if tilde_at:
            raise Unparsable("~ after = in an argument differs between the shells")


def _split_assignment(raw: RawWord) -> tuple[str, bool, RawWord] | None:
    if not raw or not isinstance(raw[0], Lit) or raw[0].quoted:
        return None
    match = ASSIGNMENT_RE.match(raw[0].text)
    if match is None:
        return None
    rest = raw[0].text[match.end() :]
    value: RawWord = ((Lit(rest, False),) if rest else ()) + raw[1:]
    return match.group(1), bool(match.group(2)), value


def _plain_text(raw: RawWord) -> str | None:
    if all(isinstance(seg, Lit) for seg in raw):
        return "".join(seg.text for seg in raw if isinstance(seg, Lit))
    return None


def _names_used(node: SimpleCmd) -> Iterator[str]:
    raws: list[RawWord] = list(node.words)
    for redirect in node.redirects:
        if redirect.target is not None:
            raws.append(redirect.target)
        if redirect.heredoc is not None:
            raws.append(redirect.heredoc.body)
    for raw in raws:
        yield from _param_names(raw)


def _param_names(segs: tuple[Seg, ...]) -> Iterator[str]:
    for seg in segs:
        if isinstance(seg, Param):
            yield seg.name
            yield from _param_names(seg.arg or ())


def _user_names(state: ShellState) -> set[str]:
    names = {posixpath.basename(state.home.rstrip("/"))}
    for key in ("USER", "LOGNAME"):
        value = state.lookup(key)
        if not isinstance(value, _Unset) and len(value) == 1:
            names.add("".join(p.text for p in value[0] if not p.unknown))
    return names
