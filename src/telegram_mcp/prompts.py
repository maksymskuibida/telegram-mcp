"""The confirmation question: the closed table of write kinds, the fields a question is built
from, and the one function that turns those fields into question text (rules BR10.1, BR11.1 to
BR11.7 and BR11.9).

Tools never write question text. A tool's describer fills ``PromptFields``; only
:func:`render_question` turns them into the question and the two option descriptions, so every
value the person reads comes from a typed field, sanitised or made visible on the way."""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

from telegram_mcp.gateway.models import SAVED_MESSAGES, ChatRef

QUESTION_LIMIT = 20_000
MESSAGE_LIMIT = 20
CHAT_NAME_LIMIT = 64
MARK = "mark"
_HIDDEN_CATEGORIES = {"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"}
_KEPT = {"\n", "\t"}
_KIB = 1024
_MIB = 1024 * 1024

NOT_ALLOWED_MESSAGE = "This action has no confirmation rule, so it cannot run."
TOO_MANY_MESSAGES = "At most 20 messages per call; split the call."
TOO_LONG = "The confirmation question would be too long to read; split the call into smaller ones."


class PromptError(Exception):
    """The question cannot be built; the check refuses with ``code`` and prepares nothing."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class AdminRight(StrEnum):
    """The closed set of rights an admin change sets (BR11.9)."""

    CHANGE_INFO = "change_info"
    POST_MESSAGES = "post_messages"
    EDIT_MESSAGES = "edit_messages"
    DELETE_MESSAGES = "delete_messages"
    BAN_USERS = "ban_users"
    INVITE_USERS = "invite_users"
    PIN_MESSAGES = "pin_messages"
    ADD_ADMINS = "add_admins"
    MANAGE_CALL = "manage_call"
    ANONYMOUS = "anonymous"
    MANAGE_TOPICS = "manage_topics"


@dataclass(frozen=True)
class Person:
    """A user a question names: a chat member or the target of an admin change."""

    id: int
    name: str


@dataclass(frozen=True)
class MessageExcerpt:
    """One existing message as a question shows it (BR11.3)."""

    id: int
    sender: str
    date: datetime
    text: str
    media: str | None = None


@dataclass(frozen=True)
class FileFields:
    """A local file a write would send, after the file sandbox admitted it (BR11.5)."""

    path: str
    name: str
    size_bytes: int


@dataclass(frozen=True)
class TopicRef:
    """A forum topic a send or a mark targets."""

    topic_id: int
    title: str


@dataclass(frozen=True)
class FieldChange:
    """One changed field of a chat or of the profile, shown as ``field: before → after``."""

    field: str
    before: str | None
    after: str


@dataclass(frozen=True)
class HeadlineDetails:
    """The values a question names besides the chat and the content (BR11.1). Which ones a kind
    needs is checked when the question is rendered."""

    message_id: int | None = None
    emoji: str | None = None
    title: str | None = None
    new_chat_type: Literal["group", "channel"] | None = None
    members: tuple[Person, ...] | None = None
    user: Person | None = None
    link: str | None = None
    for_everyone: bool | None = None
    expire_seconds: int | None = None
    usage_limit: int | None = None
    rights: Mapping[str, Any] | None = None
    current_rights: Mapping[str, Any] | None = None
    changes: tuple[FieldChange, ...] = ()


@dataclass(frozen=True)
class PromptFields:
    """What one question shows (C2 PromptFields). ``action`` is the write kind, or ``mark``."""

    action: str
    tool: str
    chat: ChatRef | None = None
    content: str = ""
    previous: str | None = None
    source_chat: ChatRef | None = None
    messages: tuple[MessageExcerpt, ...] = ()
    file: FileFields | None = None
    topic: TopicRef | None = None
    unread: int | None = None
    details: HeadlineDetails = field(default_factory=HeadlineDetails)


@dataclass(frozen=True)
class KindSpec:
    """One row of the kind table (C2 write_kinds, plus mark)."""

    write_kind: str
    tools: tuple[str, ...]
    headline: str
    needs_chat: bool
    needs_messages: bool
    may_have_file: bool
    option_descriptions: tuple[str, str]
    tool_headlines: Mapping[str, str] = field(default_factory=dict)
    tool_descriptions: Mapping[str, tuple[str, str]] = field(default_factory=dict)

    def descriptions_for(self, tool: str) -> tuple[str, str]:
        """The descriptions under Allow and Deny for this tool (BR11.6)."""
        return self.tool_descriptions.get(tool, self.option_descriptions)


def _spec(
    kind: str,
    tools: tuple[str, ...],
    headline: str,
    descriptions: tuple[str, str],
    *,
    needs_chat: bool = True,
    needs_messages: bool = False,
    may_have_file: bool = False,
    tool_headlines: Mapping[str, str] | None = None,
    tool_descriptions: Mapping[str, tuple[str, str]] | None = None,
) -> KindSpec:
    return KindSpec(
        write_kind=kind,
        tools=tools,
        headline=headline,
        needs_chat=needs_chat,
        needs_messages=needs_messages,
        may_have_file=may_have_file,
        option_descriptions=descriptions,
        tool_headlines=dict(tool_headlines or {}),
        tool_descriptions=dict(tool_descriptions or {}),
    )


READ_AND_MARK_TOOL = "tg_messages_read"
UNPIN_TOOL = "tg_messages_unpin"
READ_AND_MARK_NOTE = (
    "Telegram marks everything up to that message, so unread messages older than the ones "
    "Claude reads are marked read too."
)

KINDS: dict[str, KindSpec] = {
    spec.write_kind: spec
    for spec in (
        _spec(
            "send",
            ("tg_messages_send",),
            "Send this message to <chat><topic>?",
            ("Send it", "Do not send"),
        ),
        _spec(
            "file_send",
            ("tg_files_send",),
            "Send this file to <chat><topic>?",
            ("Send the file", "Do not send"),
            may_have_file=True,
        ),
        _spec(
            "edit",
            ("tg_messages_edit",),
            "Edit your message <n> in <chat>?",
            ("Save the edit", "Keep the message as it is"),
        ),
        _spec(
            "delete",
            ("tg_messages_delete",),
            "Delete <k> message(s) in <chat> for everyone? / only for you?",
            ("Delete them", "Do not delete"),
            needs_messages=True,
        ),
        _spec(
            "forward",
            ("tg_messages_forward",),
            "Forward <k> message(s) from <source chat> to <chat>?",
            ("Forward them", "Do not forward"),
            needs_messages=True,
        ),
        _spec(
            "react",
            ("tg_messages_react",),
            "React <emoji> to message <n> in <chat>?",
            ("Add the reaction", "Do not react"),
            needs_messages=True,
        ),
        _spec(
            "pin",
            ("tg_messages_pin", UNPIN_TOOL),
            "Pin message <n> in <chat>?",
            ("Pin it", "Do not pin"),
            needs_messages=True,
            tool_headlines={UNPIN_TOOL: "Unpin message <n> in <chat>?"},
            tool_descriptions={UNPIN_TOOL: ("Unpin it", "Keep it pinned")},
        ),
        _spec(
            "topic_create",
            ("tg_topics_create",),
            "Create the topic «<title>» in <chat>?",
            ("Create the topic", "Do not create"),
        ),
        _spec(
            "chat_create",
            ("tg_chats_create",),
            "Create a new <new_chat_type> «<title>»?",
            ("Create it", "Do not create"),
            needs_chat=False,
        ),
        _spec(
            "join",
            ("tg_chats_join",),
            "Join <chat>? / Join the chat at <link>?",
            ("Join", "Do not join"),
            needs_chat=False,
        ),
        _spec("leave", ("tg_chats_leave",), "Leave <chat>?", ("Leave", "Stay")),
        _spec(
            "chat_edit",
            ("tg_chats_edit",),
            "Change <chat>?",
            ("Save the changes", "Do not change"),
            may_have_file=True,
        ),
        _spec(
            "invite",
            ("tg_invites_create",),
            "Create an invite link for <chat>?",
            ("Create the link", "Do not create"),
        ),
        _spec(
            "admin",
            ("tg_admins_set",),
            "Change the admin rights of «<user name>» (id <user id>) in <chat>?",
            ("Change the rights", "Do not change"),
        ),
        _spec(
            "profile_edit",
            ("tg_profile_edit",),
            "Change your own profile?",
            ("Save the changes", "Do not change"),
            needs_chat=False,
            may_have_file=True,
        ),
        _spec(
            MARK,
            ("tg_chats_mark_read", READ_AND_MARK_TOOL),
            "Mark <k> unread message(s) in <chat><topic> as read?",
            ("Mark as read", "Leave unread"),
            tool_headlines={
                READ_AND_MARK_TOOL: "After reading, mark <chat><topic> as read up to the newest "
                "message Claude reads (<k> unread now)?"
            },
        ),
    )
}


# Showing values ----------------------------------------------------------------------------------


def visible(text: str) -> str:
    """Text as the person must see it (BR11.2): line breaks and tabs are kept; every other
    control, format (bidirectional overrides and isolates, zero-width characters), separator,
    surrogate, private or unassigned character is shown as ``[U+XXXX]``."""
    return "".join(
        f"[U+{ord(char):04X}]"
        if char not in _KEPT and unicodedata.category(char) in _HIDDEN_CATEGORIES
        else char
        for char in text
    )


def sanitize_chat_name(name: str) -> str:
    """Show a name so a hostile title cannot pose as another chat or as the tool's own text
    (U1 BR3.4, NFR4.2): control, format and separator characters become spaces, « and »
    become quotes, and the result is cut to 64 characters."""
    shown = "".join(
        " " if unicodedata.category(char) in _HIDDEN_CATEGORIES else char for char in name
    )
    shown = shown.replace("«", '"').replace("»", '"')
    if len(shown) > CHAT_NAME_LIMIT:
        shown = shown[: CHAT_NAME_LIMIT - 1] + "…"
    return shown


def chat_label(chat: ChatRef) -> str:
    """A chat by sanitised name, type and id; the account's own chat as your Saved Messages."""
    if chat.saved_messages:
        return f"your {SAVED_MESSAGES} (id {chat.id})"
    return f"«{sanitize_chat_name(chat.name)}» ({chat.type}, id {chat.id})"


def _topic_label(topic: TopicRef | None) -> str:
    if topic is None:
        return ""
    return f" in topic «{sanitize_chat_name(topic.title)}» (id {topic.topic_id})"


def _person(person: Person) -> str:
    return f"«{sanitize_chat_name(person.name)}» (id {person.id})"


def format_size(size_bytes: int) -> str:
    """A file size in KB below one megabyte, otherwise in MB, with one decimal (BR11.5)."""
    if size_bytes >= _MIB:
        return f"{size_bytes / _MIB:.1f} MB"
    return f"{size_bytes / _KIB:.1f} KB"


def _file_lines(file: FileFields) -> str:
    return (
        f"File: {visible(file.path)}\n"
        f"Name: {visible(file.name)}, {format_size(file.size_bytes)} ({file.size_bytes} bytes)"
    )


def _check_message_list(messages: Sequence[MessageExcerpt]) -> None:
    if len(messages) > MESSAGE_LIMIT:
        raise PromptError("invalid_arguments", TOO_MANY_MESSAGES)
    ids = [message.id for message in messages]
    if len(set(ids)) != len(ids):
        raise PromptError("invalid_arguments", "Each message may be named only once per call.")


def format_messages(messages: Sequence[MessageExcerpt]) -> str:
    """Every message in full, oldest first (BR11.3)."""
    _check_message_list(messages)
    blocks = []
    for message in sorted(messages, key=lambda m: m.id):
        when = message.date.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
        lines = [f"#{message.id} {sanitize_chat_name(message.sender)}, {when} UTC:"]
        if message.text:
            lines.append(visible(message.text))
        if message.media:
            lines.append(f"[{visible(message.media)}]")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def check_message_ids(
    requested: Sequence[int], found: Iterable[MessageExcerpt]
) -> tuple[MessageExcerpt, ...]:
    """The messages a call names, oldest first, for a describer to put in PromptFields: at most
    20, each once, and every one found in the chat (BR11.3)."""
    if len(requested) > MESSAGE_LIMIT:
        raise PromptError("invalid_arguments", TOO_MANY_MESSAGES)
    if len(set(requested)) != len(requested):
        raise PromptError("invalid_arguments", "Each message may be named only once per call.")
    by_id = {message.id: message for message in found}
    missing = [number for number in requested if number not in by_id]
    if missing:
        named = ", ".join(str(number) for number in missing)
        raise PromptError("not_found", f"These messages were not found in this chat: {named}.")
    return tuple(sorted((by_id[number] for number in requested), key=lambda m: m.id))


def normalize_rights(raw: object) -> dict[str, bool]:
    """Every AdminRight, in table order, with omitted rights off (BR11.9). An unknown right or a
    value that is not a boolean is refused."""
    if not isinstance(raw, Mapping):
        raise PromptError("invalid_arguments", "rights must map each admin right to true or false")
    known = {right.value for right in AdminRight}
    for name, value in raw.items():
        if name not in known:
            raise PromptError("invalid_arguments", f"{name} is not an admin right")
        if not isinstance(value, bool):
            raise PromptError("invalid_arguments", f"rights.{name} must be true or false")
    return {right.value: bool(raw.get(right.value, False)) for right in AdminRight}


# Rendering ---------------------------------------------------------------------------------------


def _missing(name: str) -> PromptError:
    return PromptError(
        "invalid_arguments",
        f"The confirmation question needs {name}, which is missing; nothing was prepared.",
    )


def _need[T](value: T | None, name: str) -> T:
    if value is None:
        raise _missing(name)
    return value


def _chat(fields: PromptFields) -> str:
    return chat_label(_need(fields.chat, "chat"))


def _messages(fields: PromptFields) -> str:
    if not fields.messages:
        raise _missing("messages")
    return format_messages(fields.messages)


def _changes(fields: PromptFields) -> str:
    lines = [
        f"{visible(change.field)}: "
        f"{visible(change.before) if change.before is not None else '(none)'} → "
        f"{visible(change.after)}"
        for change in fields.details.changes
    ]
    if fields.file is not None:
        lines.append(_file_lines(fields.file))
    if not lines:
        raise _missing("changes")
    return "\n".join(lines)


Rendered = tuple[str, str]


def _send(fields: PromptFields, headline: str, now: datetime) -> Rendered:
    # U1 rule 3.4, unchanged: a reply and a topic known only by id are named after the "?".
    chat = _chat(fields)
    topic, suffix = fields.topic, ""
    if fields.details.message_id is not None:
        suffix += f" Reply to message {fields.details.message_id}."
    if topic is not None and not topic.title:
        suffix += f" In topic {topic.topic_id}."
        topic = None
    return f"Send this message to {chat}{_topic_label(topic)}?{suffix}", visible(fields.content)


def _file_send(fields: PromptFields, headline: str, now: datetime) -> Rendered:
    body = _file_lines(_need(fields.file, "file"))
    if fields.content:
        body += "\n" + visible(fields.content)
    return f"Send this file to {_chat(fields)}{_topic_label(fields.topic)}?", body


def _edit(fields: PromptFields, headline: str, now: datetime) -> Rendered:
    number = _need(fields.details.message_id, "message_id")
    previous = _need(fields.previous, "previous")
    body = f"Now:\n{visible(previous)}\nNew:\n{visible(fields.content)}"
    return f"Edit your message {number} in {_chat(fields)}?", body


def _delete(fields: PromptFields, headline: str, now: datetime) -> Rendered:
    everyone = _need(fields.details.for_everyone, "for_everyone")
    body = _messages(fields)
    who = "for everyone" if everyone else "only for you"
    return f"Delete {len(fields.messages)} message(s) in {_chat(fields)} {who}?", body


def _forward(fields: PromptFields, headline: str, now: datetime) -> Rendered:
    source = chat_label(_need(fields.source_chat, "source_chat"))
    body = _messages(fields)
    return f"Forward {len(fields.messages)} message(s) from {source} to {_chat(fields)}?", body


def _react(fields: PromptFields, headline: str, now: datetime) -> Rendered:
    emoji = visible(_need(fields.details.emoji, "emoji"))
    number = _need(fields.details.message_id, "message_id")
    return f"React {emoji} to message {number} in {_chat(fields)}?", _messages(fields)


def _pin(fields: PromptFields, headline: str, now: datetime) -> Rendered:
    number = _need(fields.details.message_id, "message_id")
    verb = "Unpin" if headline.startswith("Unpin") else "Pin"
    return f"{verb} message {number} in {_chat(fields)}?", _messages(fields)


def _topic_create(fields: PromptFields, headline: str, now: datetime) -> Rendered:
    title = sanitize_chat_name(_need(fields.details.title, "title"))
    return f"Create the topic «{title}» in {_chat(fields)}?", ""


def _chat_create(fields: PromptFields, headline: str, now: datetime) -> Rendered:
    details = fields.details
    kind = _need(details.new_chat_type, "new_chat_type")
    title = sanitize_chat_name(_need(details.title, "title"))
    members = _need(details.members, "members")
    if members:
        body = "Members:\n" + "\n".join(_person(member) for member in members)
    else:
        body = "Members: no other members"
    return f"Create a new {kind} «{title}»?", body


def _join(fields: PromptFields, headline: str, now: datetime) -> Rendered:
    if fields.chat is not None:
        return f"Join {chat_label(fields.chat)}?", ""
    return f"Join the chat at {visible(_need(fields.details.link, 'link'))}?", ""


def _leave(fields: PromptFields, headline: str, now: datetime) -> Rendered:
    return f"Leave {_chat(fields)}?", ""


def _chat_edit(fields: PromptFields, headline: str, now: datetime) -> Rendered:
    return f"Change {_chat(fields)}?", _changes(fields)


def _invite(fields: PromptFields, headline: str, now: datetime) -> Rendered:
    details = fields.details
    if details.expire_seconds is None:
        expires = "never"
    else:
        moment = now.astimezone(UTC) + timedelta(seconds=details.expire_seconds)
        expires = moment.strftime("%Y-%m-%d %H:%M") + " UTC"
    limit = "none" if details.usage_limit is None else str(details.usage_limit)
    body = f"Expires: {expires}. Usage limit: {limit}."
    return f"Create an invite link for {_chat(fields)}?", body


def _admin(fields: PromptFields, headline: str, now: datetime) -> Rendered:
    details = fields.details
    user = _need(details.user, "user")
    rights = normalize_rights(_need(details.rights, "rights"))
    current = normalize_rights(_need(details.current_rights, "current_rights"))
    lines = [
        f"{name}: {'on' if value else 'off'}" + (" (changes)" if value != current[name] else "")
        for name, value in rights.items()
    ]
    return (
        f"Change the admin rights of {_person(user)} in {_chat(fields)}?",
        "\n".join(lines),
    )


def _profile_edit(fields: PromptFields, headline: str, now: datetime) -> Rendered:
    return "Change your own profile?", _changes(fields)


def _mark(fields: PromptFields, headline: str, now: datetime) -> Rendered:
    unread = fields.unread
    if unread is None or unread < 1:
        raise _missing("unread (a positive count)")
    where = f"{_chat(fields)}{_topic_label(fields.topic)}"
    if fields.tool == READ_AND_MARK_TOOL:
        line = (
            f"After reading, mark {where} as read up to the newest message Claude reads "
            f"({unread} unread now)?"
        )
        return line, READ_AND_MARK_NOTE
    return f"Mark {unread} unread message(s) in {where} as read?", ""


_RENDERERS: dict[str, Callable[[PromptFields, str, datetime], Rendered]] = {
    "send": _send,
    "file_send": _file_send,
    "edit": _edit,
    "delete": _delete,
    "forward": _forward,
    "react": _react,
    "pin": _pin,
    "topic_create": _topic_create,
    "chat_create": _chat_create,
    "join": _join,
    "leave": _leave,
    "chat_edit": _chat_edit,
    "invite": _invite,
    "admin": _admin,
    "profile_edit": _profile_edit,
    MARK: _mark,
}


def render_question(
    approval_id: str, fields: PromptFields, *, now: datetime | None = None
) -> tuple[str, tuple[str, str]]:
    """The exact question and the descriptions under Allow and Deny (BR11.1 to BR11.6). The
    question is ``[tg-<id>] `` + the kind's first line, then a line break and the body when
    there is one. Raises :class:`PromptError` when a rule is broken; nothing is cut."""
    spec = KINDS.get(fields.action)
    if spec is None:
        raise PromptError("not_allowed", NOT_ALLOWED_MESSAGE)
    if fields.file is not None and not spec.may_have_file:
        raise PromptError("invalid_arguments", f"A {spec.write_kind} cannot carry a file.")
    headline_template = spec.tool_headlines.get(fields.tool, spec.headline)
    headline, body = _RENDERERS[spec.write_kind](
        fields, headline_template, now if now is not None else datetime.now(UTC)
    )
    question = f"[tg-{approval_id}] {headline}"
    if body:
        question += "\n" + body
    if len(question) > QUESTION_LIMIT:
        raise PromptError("invalid_arguments", TOO_LONG)
    return question, spec.descriptions_for(fields.tool)


def build_send_question(
    approval_id: str, chat: ChatRef, text: str, reply_to: int | None, topic_id: int | None
) -> str:
    """U1's send question (BR3.4), through the same renderer as every other kind."""
    question, _ = render_question(
        approval_id,
        PromptFields(
            action="send",
            tool="tg_messages_send",
            chat=chat,
            content=text,
            topic=TopicRef(topic_id, "") if topic_id is not None else None,
            details=HeadlineDetails(message_id=reply_to),
        ),
    )
    return question
