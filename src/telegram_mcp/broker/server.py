"""The ConnectionBroker (`telegram-mcp broker`): the one process that holds the Telegram
connection, the approvals and the activity log.

Every tool call and approval check runs its checks in one fixed order: authenticated
connection, client allowed for writes, vault and session, kind, arguments and digest, the
Telegram connection, then either the chat lookup and the question (approval.check) or, for a
write, the room in the write line, the single use of a confirmed approval and the send.

A write's approval is used when the write enters the write line. Every refusal before that
keeps the approval; whatever happens after it (dropped unsent, flood wait, timeout, sent), the
approval is spent and the write is never sent again. Reads, checks, answers and status never
wait for the write line.

The broker stops by itself after ten minutes with no request, never while a request runs, and
replaces its secret every hour.

What a tool does is its registered action (actions.py); the order, the question, the
confirmation and the log belong to the broker alone (U2's BR10 to BR15)."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import os
import re
import signal
import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from telegram_mcp import __version__
from telegram_mcp.actions import ActionRegistry, ConsentAction, default_registry
from telegram_mcp.activity_log import (
    ActivityLog,
    DecisionKind,
    Diagnostics,
    EventKind,
    LogEntry,
    Redactor,
    utc_now,
)
from telegram_mcp.broker import jsonrpc
from telegram_mcp.broker.link import (
    DISCONNECT_LIMIT_SECONDS,
    SessionRefused,
    TelegramLink,
    Unreachable,
)
from telegram_mcp.broker.lock import PROTOCOL_VERSION, BrokerLock, LockState
from telegram_mcp.broker.scheduler import (
    DropReason,
    LineFull,
    ReadPlaces,
    WriteDropped,
    WriteItem,
    WriteLine,
)
from telegram_mcp.broker.secret import new_secret, remove_leftovers, write_secret
from telegram_mcp.consent import (
    ActionKind,
    Allow,
    ApprovalStatus,
    Ask,
    ConsentGate,
    Decision,
    DigestError,
    PendingApproval,
    Refuse,
    canonical_digest,
)
from telegram_mcp.gateway.models import (
    ChatNotFound,
    ChatRef,
    FloodWait,
    GatewayError,
    GatewayTimeout,
    NotAuthorized,
    OutcomeUnknown,
    TelegramError,
    TopicNotFound,
    UnreadState,
    account_label,
)
from telegram_mcp.gateway.protocol import GatewayFactory, TelegramGateway
from telegram_mcp.paths import FixedPaths
from telegram_mcp.prompts import NOT_ALLOWED_MESSAGE, PromptError, PromptFields, TopicRef
from telegram_mcp.settings import Settings, SettingsError
from telegram_mcp.tools import ASK_AGAIN, InvalidArguments
from telegram_mcp.vault import (
    SessionVault,
    StoredSession,
    VaultDeleteResult,
    VaultOutcome,
    VaultReadResult,
)

CHECK_BUDGET_SECONDS = 10.0
SWEEP_INTERVAL_SECONDS = 5.0
CALL_LIMIT_SECONDS = 30.0
STATUS_BUDGET_SECONDS = 15.0
LOGOUT_BUDGET_SECONDS = 30.0
IDLE_LIMIT_SECONDS = 600.0
SECRET_ROTATION_SECONDS = 3600.0
READ_ANSWER_LIMIT_SECONDS = 70.0
LONGEST_READ_FLOOD_WAIT_SECONDS = 30
STREAM_LIMIT = 2**20
CLIENT_KINDS = ("mcp-server", "approval-hook", "cli")
SESSION_CLIENT = "mcp-server"
CLAUDE_CODE = "claude-code"
PRIVATE_FILE = 0o600
PRIVATE_FOLDER = 0o700
FAKE_GATEWAY_ENV = "TELEGRAM_MCP_TEST_FAKE_GATEWAY"
REMEMBERED = ("declined", "timed_out")
_GIVEN_ID = re.compile(r"^\[tg-([^\]\s]{1,32})\]")

MESSAGES = {
    "no_session": "Not logged in: run `telegram-mcp login` in a terminal.",
    "keychain_locked": (
        "The macOS Keychain is locked (or refused access); unlock it and try again."
    ),
    "needs_confirmation": (
        "This action needs the person's confirmation; call it again so the confirmation "
        "hook can ask."
    ),
    "unsupported_client": (
        "This client cannot show confirmation prompts; this action is not supported here."
    ),
    "telegram_timeout": "Telegram did not answer in time; try again in a moment.",
    "outcome_unknown": (
        "The message may or may not have been sent; look at the chat before sending it again."
    ),
    "mark_unknown": (
        "The chat may or may not have been marked as read; look at it before marking it again."
    ),
    "not_found_chat": "This account cannot see a chat with that id; tg_chats_list shows the ids.",
    "not_found_topic": "This forum has no topic with that id; tg_topics_list lists its topics.",
    "not_found_tool": "This version has no tool with that name.",
    "log_failed": "The message was not sent: could not write the activity log.",
    "not_sent": (
        "Not sent: other sends were still running; ask again (a new confirmation is needed)."
    ),
    "line_full": "Not sent: too many sends waiting; try again in a moment.",
    "prompt_log_failed": (
        "Nothing was prepared: the confirmation question could not be written to the activity log."
    ),
    "forum_needs_topic": (
        "This chat is a forum group: name one topic with topic_id. tg_topics_list lists its "
        "topics and marks nothing."
    ),
    "topic_needs_forum": "topic_id is only for forum groups, and this chat is not one.",
    "unread_failed": "The unread check failed, so nothing was marked.",
    "unread_timeout": "The unread check timed out, so nothing was marked.",
    "unread_malformed": "The unread check gave a malformed answer, so nothing was marked.",
    "describe_failed": "The confirmation question could not be built; nothing was prepared.",
}
MARK_NOTES = {
    "declined": "not marked: the person declined",
    "timed_out": "not marked: the person did not answer in time",
    "client": "marking is not supported in this client",
    "empty": "not marked: no messages were returned",
}
ASK_AGAIN_CONTENT = {
    "ask_again": "ask again",
    "ask_again_refused": "ask again refused: asked too often",
    "ask_again_unneeded": "ask again: nothing to reopen",
}
UNUSED_CONTENT = "allowed but not used within 60 s"

Sleep = Callable[[float], Awaitable[None]]


class BrokerStartError(Exception):
    """The broker could not start: another broker or a login holds the lock, or a file did
    not get its private mode."""


class ToolFailure(Exception):
    def __init__(self, code: str, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after
        self.approval: PendingApproval | None = None
        """The approval this call had already used when it failed, for the log."""

    @classmethod
    def fixed(cls, code: str, key: str | None = None) -> ToolFailure:
        return cls(code, MESSAGES[key or code])

    @classmethod
    def flood(cls, seconds: int) -> ToolFailure:
        message = f"Telegram asked to wait {seconds} s; try again after that."
        return cls("flood_wait", message, retry_after=seconds)

    def as_result(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.retry_after is not None:
            error["retry_after"] = self.retry_after
        return {"ok": False, "error": error}

    def as_refusal(self) -> dict[str, Any]:
        return _refusal(self.code, self.message, self.retry_after)


@dataclass(eq=False)
class Connection:
    authenticated: bool = False
    client: str | None = None
    package_version: str | None = None
    client_name: str | None = None
    client_version: str | None = None
    reader: asyncio.StreamReader | None = None
    closed: bool = False
    arrived_at: datetime | None = None
    """When the request being served reached the broker."""
    activity_before: datetime | None = None
    """The broker's last activity before that request arrived."""

    @property
    def label(self) -> str:
        if self.client == "mcp-server":
            return f"mcp-server:{self.client_name or 'unknown'}"
        return self.client or "unknown"

    def is_open(self) -> bool:
        """False once the caller has closed its side, so its answer can no longer arrive."""
        if self.closed:
            return False
        return self.reader is None or not self.reader.at_eof()

    @property
    def is_claude_code(self) -> bool:
        return self.client_name == CLAUDE_CODE


Handler = Callable[[Connection, dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class _Ended:
    """What Telegram said to a logout: ``reason`` None means it confirmed."""

    account: str | None
    reason: str | None
    retry_after: int | None = None
    error_type: str | None = None


def _logout_reason(error: Exception) -> tuple[str, int | None]:
    if isinstance(error, FloodWait):
        return "flood_wait", error.seconds
    if isinstance(error, NotAuthorized | SessionRefused):
        return "no_session", None
    if isinstance(error, GatewayTimeout | TimeoutError):
        return "telegram_timeout", None
    return "telegram_error", None


class Broker:
    def __init__(
        self,
        paths: FixedPaths,
        *,
        gateway_factory: GatewayFactory,
        vault: SessionVault | None = None,
        registry: ActionRegistry | None = None,
        clock: Callable[[], datetime] = utc_now,
        check_budget: float = CHECK_BUDGET_SECONDS,
        sweep_interval: float = SWEEP_INTERVAL_SECONDS,
        call_limit: float = CALL_LIMIT_SECONDS,
        idle_limit: float = IDLE_LIMIT_SECONDS,
        secret_rotation: float = SECRET_ROTATION_SECONDS,
        status_budget: float = STATUS_BUDGET_SECONDS,
        logout_budget: float = LOGOUT_BUDGET_SECONDS,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.paths = paths
        self.check_budget = check_budget
        self.call_limit = call_limit
        self.idle_limit = idle_limit
        self.secret_rotation = secret_rotation
        self.status_budget = status_budget
        self.logout_budget = logout_budget
        self.registry = registry if registry is not None else default_registry()
        self.gate = ConsentGate(self.registry.decls(), clock=clock)
        self.redactor = Redactor()
        self.diagnostics = Diagnostics(paths.diagnostics, self.redactor)
        self.log = ActivityLog(
            paths.log, self.redactor, on_rotation_failure=self._log_rotation_failed
        )
        self.settings_refusal: SettingsError | None = None
        self.link = TelegramLink(
            gateway_factory, clock=clock, connect_limit=call_limit, report=self.diagnostics.write
        )
        self.writes = WriteLine(clock=clock, on_drop=self._write_dropped)
        self.reads = ReadPlaces()
        self._clock = clock
        self._sleep = sleep
        self._vault = vault or SessionVault(paths.keychain_service)
        self._sweep_interval = sweep_interval
        self._lock = BrokerLock(paths.lock)
        self._secret = ""
        self._secret_written_at = clock()
        self._server: asyncio.Server | None = None
        self._sweeper: asyncio.Task[None] | None = None
        self._stopper: asyncio.Task[None] | None = None
        self._vault_result: VaultReadResult | None = None
        self._connections: dict[Connection, asyncio.StreamWriter] = {}
        self._logging_out = False
        self._logout_answered = asyncio.Event()
        self._logout_answer: dict[str, Any] | None = None
        self._stopped = asyncio.Event()
        self._finished = asyncio.Event()
        self._exit_when_idle = False
        self._last_activity = clock()
        self.running_requests = 0
        self._handlers: dict[str, Handler] = {
            "tool.call": self._tool_call,
            "approval.check": self._approval_check,
            "approval.answer": self._approval_answer,
            "broker.status": self._status,
            "broker.stop": self._stop_request,
            "session.logout": self._logout,
        }

    # Start and stop -----------------------------------------------------------------------

    async def start(self) -> None:
        """Hold the lock, then create the secret and the socket with private modes, read the
        vault once and check the settings."""
        _prepare_data_dir(self.paths)
        if not self._lock.try_hold(LockState.RUNNING):
            raise BrokerStartError("another broker or a login holds the lock")
        try:
            self.diagnostics.start()
            self.diagnostics.write(f"broker {__version__} starting, pid {os.getpid()}")
            await self._open_endpoint()
            _require_private_modes(self.paths)
        except BaseException:
            await self._close_endpoint()
            raise
        await self._read_vault()
        self._check_settings()
        self._last_activity = self._clock()
        self.writes.start()
        self._sweeper = asyncio.create_task(self._sweep_forever())

    async def _open_endpoint(self) -> None:
        for leftover in (self.paths.socket, self.paths.secret):
            leftover.unlink(missing_ok=True)
        remove_leftovers(self.paths.data_dir)
        self._secret = new_secret()
        write_secret(self.paths.secret, self._secret)
        self._secret_written_at = self._clock()
        previous = os.umask(0o077)
        try:
            self._server = await asyncio.start_unix_server(
                self._serve_connection, path=str(self.paths.socket), limit=STREAM_LIMIT
            )
        finally:
            os.umask(previous)
        os.chmod(self.paths.socket, PRIVATE_FILE)

    async def stop(self, reason: str | None = None) -> None:
        """Clean exit, in this order: stop accepting and delete the socket and the secret,
        close every client connection, disconnect from Telegram (at most 2 s), then write
        ``released`` to the lock and drop it. The lock is held until Telegram is
        disconnected, so a new broker is never connected while this one still is."""
        if self._stopped.is_set():
            await self._finished.wait()
            return
        self._stopped.set()
        try:
            await self._shut_down(reason)
        finally:
            self._finished.set()

    async def _shut_down(self, reason: str | None) -> None:
        if self._sweeper is not None and self._sweeper is not asyncio.current_task():
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
        if reason:
            self.diagnostics.write(reason)
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for path in (self.paths.socket, self.paths.secret):
            path.unlink(missing_ok=True)
        for connection, writer in list(self._connections.items()):
            connection.closed = True
            writer.close()
        if server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), DISCONNECT_LIMIT_SECONDS)
        await self.writes.close()
        await self.link.close(DISCONNECT_LIMIT_SECONDS)
        self._lock.release()
        self.diagnostics.write("broker stopped")

    async def _close_endpoint(self) -> None:
        """Undo a start that did not finish."""
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)
            self._server = None
        for path in (self.paths.socket, self.paths.secret):
            path.unlink(missing_ok=True)
        self._lock.release()

    async def wait_stopped(self) -> None:
        """Return once the broker has stopped and removed its socket and secret."""
        await self._finished.wait()

    # Connections ----------------------------------------------------------------------------

    async def _serve_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        connection = Connection(reader=reader)
        self._connections[connection] = writer
        try:
            while not self._stopped.is_set():
                try:
                    line = await reader.readline()
                except (ValueError, ConnectionError):
                    break
                if not line or self._stopped.is_set():
                    break  # a request that arrives while the broker stops is not served
                reply, close = await self._handle_line(connection, line)
                if reply:
                    writer.write(reply)
                    await writer.drain()
                if close:
                    break
        except ConnectionError:
            pass
        finally:
            connection.closed = True
            self._connections.pop(connection, None)
            writer.close()
            if self._exit_when_idle and not self._connections:
                self._schedule_stop(None)

    async def _handle_line(self, connection: Connection, line: bytes) -> tuple[bytes, bool]:
        try:
            request = jsonrpc.parse_request(line)
        except jsonrpc.RpcError as error:
            reply = jsonrpc.error_message(error.request_id, error.code, error.message)
            return reply, not connection.authenticated
        connection.arrived_at = self._clock()
        connection.activity_before = self._last_activity
        self._last_activity = connection.arrived_at
        self.running_requests += 1
        try:
            return await self._dispatch(connection, request)
        finally:
            self.running_requests -= 1
            self._last_activity = self._clock()

    async def _dispatch(
        self, connection: Connection, request: jsonrpc.Request
    ) -> tuple[bytes, bool]:
        if not connection.authenticated:
            return self._hello(connection, request)
        handler = self._handlers.get(request.method)
        if handler is None:
            message = f"unknown method {request.method}"
            return jsonrpc.error_message(request.id, jsonrpc.METHOD_NOT_FOUND, message), False
        try:
            result = await handler(connection, request.params)
        except Exception as error:  # every failure is answered, never left hanging
            self.diagnostics.exception(f"{request.method} failed", error)
            result = _failure_for(request.method)
        return jsonrpc.result_message(request.id, result), False

    def _hello(self, connection: Connection, request: jsonrpc.Request) -> tuple[bytes, bool]:
        params = request.params
        if request.method != "hello":
            message = "the first call must be hello"
            return jsonrpc.error_message(request.id, jsonrpc.INVALID_REQUEST, message), True
        if params.get("protocol") != PROTOCOL_VERSION:
            self._exit_when_idle = True
            reply = jsonrpc.error_message(request.id, jsonrpc.VERSION_MISMATCH, "version_mismatch")
            return reply, True
        secret = params.get("secret")
        if not isinstance(secret, str) or not hmac.compare_digest(
            secret.encode("utf-8"), self._secret.encode("ascii")
        ):
            return jsonrpc.error_message(request.id, jsonrpc.BAD_SECRET, "bad_secret"), True
        client = params.get("client")
        if client not in CLIENT_KINDS:
            message = "unknown client kind"
            return jsonrpc.error_message(request.id, jsonrpc.INVALID_REQUEST, message), True
        connection.authenticated = True
        connection.client = str(client)
        connection.package_version = _text_or_none(params.get("package_version"))
        connection.client_name = _text_or_none(params.get("client_name"))
        connection.client_version = _text_or_none(params.get("client_version"))
        result = {"broker_version": __version__, "pid": os.getpid()}
        return jsonrpc.result_message(request.id, result), False

    def _arrival(self, connection: Connection | None) -> datetime:
        if connection is not None and connection.arrived_at is not None:
            return connection.arrived_at
        return self._clock()

    def _seconds_since(self, moment: datetime) -> float:
        return (self._clock() - moment).total_seconds()

    # Session and the Telegram connection -----------------------------------------------------

    async def _read_vault(self) -> VaultReadResult:
        result = await asyncio.to_thread(self._vault.load)
        self._vault_result = result
        if result.session is not None:
            self.redactor.set_secrets([result.session.session, result.session.api_hash])
        return result

    async def _session(self) -> StoredSession:
        """no_session or keychain_locked before any Telegram call."""
        result = self._vault_result
        if result is None or result.outcome is not VaultOutcome.LOADED:
            result = await self._read_vault()
        if result.outcome is VaultOutcome.KEYCHAIN_LOCKED:
            raise ToolFailure.fixed("keychain_locked")
        if result.session is None:
            raise ToolFailure.fixed("no_session")
        return result.session

    async def _open_gateway(self, stored: StoredSession) -> TelegramGateway:
        """The one Telegram connection, opened if needed. During a back-off this answers at
        once, so nothing waits on a connection that just failed."""
        try:
            return await self.link.open(stored)
        except Unreachable as unreachable:
            seconds = unreachable.retry_after
            message = f"Telegram could not be reached; trying again in {seconds} s."
            raise ToolFailure("telegram_error", message, retry_after=seconds) from unreachable
        except SessionRefused as refused:
            self._vault_result = None
            raise ToolFailure.fixed("no_session") from refused

    def _forget_session(self) -> None:
        """The session is no longer valid: re-read the vault and reconnect on the next call."""
        self._vault_result = None
        self.link.mark_dropped()

    # approval.check -------------------------------------------------------------------------

    async def _approval_check(self, connection: Connection, params: dict[str, Any]) -> Any:
        if self._logging_out:
            return _refusal("no_session", MESSAGES["no_session"])
        try:
            async with asyncio.timeout(self.check_budget):
                return await self._decide(connection, params)
        except TimeoutError:
            return _refusal("telegram_timeout", MESSAGES["telegram_timeout"])
        except ToolFailure as failure:
            return failure.as_refusal()

    async def _decide(self, connection: Connection, params: dict[str, Any]) -> dict[str, Any]:
        arrived = self._arrival(connection)
        stored = await self._session()
        tool = params.get("tool")
        action = self.registry.get(tool) if isinstance(tool, str) else None
        if action is None:
            raise ToolFailure.fixed("not_found", "not_found_tool")
        decl = action.decl
        if decl.kind is ActionKind.READ and not decl.marks_read_option:
            return {"decision": "allow"}
        raw, ask_again = _split_ask_again(params.get("args"))
        args, digest = _validated(action, raw)
        kind = self.gate.effective_kind(action.tool, args)
        if kind is ActionKind.READ:
            return {"decision": "allow"}
        refusal = self.gate.row_refusal(action.tool)
        if refusal is not None:
            return _refusal(refusal.code, refusal.message)
        gateway = await self._open_gateway(stored)
        decision: Decision
        if kind is ActionKind.MARK:
            decision = await self._decide_mark(
                connection, action, gateway, args, digest, ask_again, arrived
            )
        else:
            found = self._gate_lookup(connection, action.tool, digest, ask_again)
            if found is None:
                fields = await self._describe(action, gateway, args, arrived)
                found = self.gate.check(action.tool, digest, fields)
            decision = found
        return self._answer_decision(connection, action.tool, digest, decision)

    def _gate_lookup(
        self, connection: Connection, tool: str, digest: str, ask_again: bool
    ) -> Decision | None:
        decision = self.gate.lookup(tool, digest, ask_again=ask_again)
        self._log_gate_events(connection)
        return decision

    async def _describe(
        self,
        action: ConsentAction,
        gateway: TelegramGateway,
        args: dict[str, Any],
        arrived: datetime,
    ) -> PromptFields:
        """The tool's describer, inside the check budget; any failure refuses (BR11.7). A
        flood wait is waited out once, and only when it fits in what is left of the budget."""
        try:
            return await self._describe_once(action, gateway, args)
        except ToolFailure as failure:
            wait = failure.retry_after
            if failure.code != "flood_wait" or wait is None:
                raise
            if wait >= self.check_budget - self._seconds_since(arrived):
                raise
        await self._sleep(wait)
        return await self._describe_once(action, gateway, args)

    async def _describe_once(
        self, action: ConsentAction, gateway: TelegramGateway, args: dict[str, Any]
    ) -> PromptFields:
        if action.describe is None:
            raise ToolFailure("not_allowed", NOT_ALLOWED_MESSAGE)
        try:
            return await action.describe(gateway, args)
        except PromptError as error:
            raise ToolFailure(error.code, error.message) from error
        except InvalidArguments as error:
            raise ToolFailure("invalid_arguments", str(error)) from error
        except GatewayError as error:
            raise self._lookup_failure(error) from error
        except Exception as error:
            self.diagnostics.exception("describer", error)
            raise ToolFailure.fixed("telegram_error", "describe_failed") from error

    def _lookup_failure(self, error: GatewayError) -> ToolFailure:
        """A Telegram read made to build a question or check unread state. A flood wait is
        returned, never slept, inside the check budget (NFR5.1)."""
        if isinstance(error, ChatNotFound):
            return ToolFailure.fixed("not_found", "not_found_chat")
        if isinstance(error, TopicNotFound):
            return ToolFailure.fixed("not_found", "not_found_topic")
        if isinstance(error, FloodWait):
            return ToolFailure.flood(error.seconds)
        if isinstance(error, NotAuthorized):
            self._forget_session()
            return ToolFailure.fixed("no_session")
        if isinstance(error, GatewayTimeout):
            self.link.mark_dropped()
            return ToolFailure.fixed("telegram_timeout")
        if isinstance(error, TelegramError):
            reason = self.redactor.redact(error.reason)
            return ToolFailure("telegram_error", f"Telegram refused a lookup: {reason}.")
        return ToolFailure.fixed("telegram_error", "describe_failed")

    async def _decide_mark(
        self,
        connection: Connection,
        action: ConsentAction,
        gateway: TelegramGateway,
        args: dict[str, Any],
        digest: str,
        ask_again: bool,
        arrived: datetime,
    ) -> Decision:
        """BR13.2, BR13.4, BR13.5, BR13.7: one forum topic at a time, the unread check, then
        allow with nothing unread or one question."""
        chat = await self._lookup(gateway, int(args["chat"]), arrived)
        topic_id = args.get("topic_id")
        _check_forum(chat, topic_id)
        decision = self._gate_lookup(connection, action.tool, digest, ask_again)
        if (
            isinstance(decision, Refuse)
            and decision.code in REMEMBERED
            and action.decl.marks_read_option
        ):
            return Allow()  # the read runs without marking (BR13.7)
        if decision is not None:
            return decision
        state = await self._unread(gateway, chat.id, topic_id)
        topic = TopicRef(topic_id, state.topic_title or "") if topic_id is not None else None
        fields = PromptFields(
            action="mark", tool=action.tool, chat=chat, unread=state.unread, topic=topic
        )
        return self.gate.mark_check(action.tool, digest, fields, state.newest_id)

    async def _unread(
        self, gateway: TelegramGateway, chat_id: int, topic_id: int | None, *, bounded: bool = False
    ) -> UnreadState:
        """The unread check; any failure refuses and marks nothing (BR13.5). ``bounded`` puts
        it under the 30 s Telegram timeout (at tool.call; approval.check has its budget)."""
        try:
            if bounded:
                async with asyncio.timeout(self.call_limit):
                    state = await gateway.unread_state(chat_id, topic_id)
            else:
                state = await gateway.unread_state(chat_id, topic_id)
        except (GatewayTimeout, TimeoutError) as error:
            raise ToolFailure.fixed("telegram_timeout", "unread_timeout") from error
        except (ChatNotFound, TopicNotFound, FloodWait, NotAuthorized) as error:
            raise self._lookup_failure(error) from error
        except Exception as error:
            self.diagnostics.exception("unread check", error)
            raise ToolFailure.fixed("telegram_error", "unread_failed") from error
        if not _well_formed(state):
            raise ToolFailure.fixed("telegram_error", "unread_malformed")
        return state

    def _answer_decision(
        self, connection: Connection, tool: str, digest: str, decision: Decision
    ) -> dict[str, Any]:
        if isinstance(decision, Allow):
            return {"decision": "allow"}
        if isinstance(decision, Ask):
            approval = decision.approval
            try:
                self._log_approval(connection, "prompt", approval, None, strict=True)
            except OSError:
                # BR15.1: a question that cannot be logged is never asked.
                self.gate.withdraw(approval.id)
                return _refusal("telegram_error", MESSAGES["prompt_log_failed"])
            return {
                "decision": "ask",
                "approval_id": approval.id,
                "question": approval.question,
                "options": list(approval.options),
                "option_descriptions": list(approval.option_descriptions),
                "multi_select": False,
            }
        if decision.code in REMEMBERED:
            remembered = self.gate.recent_outcome(digest)
            self._log_entry(
                connection.label,
                "result",
                tool,
                decision.code,
                approval_id=remembered.approval_id if remembered is not None else None,
            )
        return _refusal(decision.code, decision.message)

    async def _lookup(self, gateway: TelegramGateway, chat_id: int, arrived: datetime) -> ChatRef:
        """The chat lookup of a check. A flood wait is waited out once, and only when it fits
        in what is left of the check's budget; otherwise the check refuses."""
        try:
            return await self._resolve(gateway, chat_id)
        except ToolFailure as failure:
            wait = failure.retry_after
            if failure.code != "flood_wait" or wait is None:
                raise
            if wait >= self.check_budget - self._seconds_since(arrived):
                raise
        await self._sleep(wait)
        return await self._resolve(gateway, chat_id)

    async def _resolve(self, gateway: TelegramGateway, chat_id: int) -> ChatRef:
        try:
            return await gateway.resolve_chat(chat_id)
        except ChatNotFound as error:
            raise ToolFailure.fixed("not_found", "not_found_chat") from error
        except NotAuthorized as error:
            self._forget_session()
            raise ToolFailure.fixed("no_session") from error
        except GatewayTimeout as error:
            self.link.mark_dropped()
            raise ToolFailure.fixed("telegram_timeout") from error
        except FloodWait as error:
            raise _flood_failure(error.seconds) from error
        except TelegramError as error:
            raise ToolFailure("telegram_error", "Telegram refused the chat lookup.") from error

    # approval.answer ------------------------------------------------------------------------

    async def _approval_answer(self, connection: Connection, params: dict[str, Any]) -> Any:
        if self._logging_out:
            return {"outcome": "unknown"}
        question, pick = params.get("question"), params.get("pick")
        options = _text_list(params.get("options"))
        descriptions = _text_list(params.get("option_descriptions"))
        multi_select = params.get("multi_select")
        if (
            not isinstance(question, str)
            or not isinstance(pick, str)
            or options is None
            or not isinstance(multi_select, bool | None)
        ):
            return {"outcome": "unknown"}
        answer = self.gate.answer(
            question,
            options,
            pick,
            option_descriptions=descriptions,
            multi_select=multi_select,
            from_hook=connection.client == "approval-hook",
        )
        if answer.approval is not None:
            decision: DecisionKind = "allowed" if answer.outcome == "confirmed" else "declined"
            self._log_approval(connection, "decision", answer.approval, decision)
        else:
            # BR12.3, BR15.1: an unknown pick confirms nothing and is logged with the id given.
            given = _GIVEN_ID.match(question)
            self._log_entry(
                connection.label,
                "decision",
                "approval.answer",
                "not_found",
                approval_id=given[1] if given else None,
            )
        return {"outcome": str(answer.outcome)}

    # tool.call -------------------------------------------------------------------------------

    async def _tool_call(self, connection: Connection, params: dict[str, Any]) -> Any:
        if self._logging_out:
            return ToolFailure.fixed("no_session").as_result()
        tool = params.get("tool")
        if not isinstance(tool, str):
            return ToolFailure("invalid_arguments", "tool must be a tool name").as_result()
        raw = params.get("args")
        try:
            if self.gate.kind_of(tool) is not ActionKind.READ and not connection.is_claude_code:
                raise ToolFailure.fixed("unsupported_client")
            # The vault is read (and the redactor given its secrets) before any entry holding
            # the caller's text is written, also after a start with no session.
            stored = await self._session()
            self._log_call(connection, tool, raw)
            action = self.registry.get(tool)
            if action is None:
                raise ToolFailure.fixed("not_found", "not_found_tool")
            data, approval = await self._call_action(connection, action, stored, raw)
        except ToolFailure as failure:
            self._log_result(connection, tool, failure.code, failure.approval, raw)
            return failure.as_result()
        self._log_result(connection, tool, "ok", approval, raw)
        return {"ok": True, "data": data}

    async def _call_action(
        self, connection: Connection, action: ConsentAction, stored: StoredSession, raw: object
    ) -> tuple[Any, PendingApproval | None]:
        decl = action.decl
        if decl.kind is ActionKind.READ and not decl.marks_read_option:
            args = _validated_read(action, raw)
            gateway = await self._open_gateway(stored)
            return await self._run_read(connection, action, gateway, args), None
        raw, _ = _split_ask_again(raw)  # ask_again never changes what runs (BR12.5)
        args, digest = _validated(action, raw)
        kind = self.gate.effective_kind(action.tool, args)
        if kind is ActionKind.READ:
            gateway = await self._open_gateway(stored)
            return await self._run_read(connection, action, gateway, args), None
        refusal = self.gate.row_refusal(action.tool)
        if refusal is not None:
            raise ToolFailure(refusal.code, refusal.message)
        gateway = await self._open_gateway(stored)
        if decl.marks_read_option:
            return await self._read_and_mark(connection, action, gateway, args, digest)
        if kind is ActionKind.MARK:
            return await self._mark(connection, action, gateway, args, digest)
        if self.writes.is_full:
            raise ToolFailure("telegram_error", MESSAGES["line_full"])
        approval = self._consume(action.tool, digest)  # used here: the write enters the line
        try:
            data = await self._queue_write(
                connection, approval, lambda: self._run_write(action, gateway, approval, args)
            )
        except ToolFailure as failure:
            failure.approval = approval
            raise
        return data, approval

    def _consume(self, tool: str, digest: str) -> PendingApproval:
        """The confirmed approval, or why there is none: a remembered Deny or timeout is told
        as such (BR12.4); otherwise the action needs confirmation."""
        approval = self.gate.consume(tool, digest)
        if approval is not None:
            return approval
        remembered = self.gate.recent_outcome(digest)
        if remembered is not None and remembered.outcome is not None:
            refusal = self.gate.lookup(tool, digest)
            if isinstance(refusal, Refuse):
                raise ToolFailure(refusal.code, refusal.message)
        raise ToolFailure.fixed("needs_confirmation")

    async def _run_read(
        self,
        connection: Connection,
        action: ConsentAction,
        gateway: TelegramGateway,
        args: dict[str, Any],
    ) -> Any:
        """A read takes one of the read places, never the write line. A flood wait of at most
        30 s is waited out once, when the second try still answers inside 70 s of arrival."""
        if action.run is None:
            raise ToolFailure("not_allowed", NOT_ALLOWED_MESSAGE)
        arrived = self._arrival(connection)
        try:
            return await self._read_once(action, gateway, args)
        except FloodWait as flood:
            if not self._may_wait_out(flood.seconds, arrived):
                raise self._read_failure(flood) from flood
            wait = flood.seconds
        except Exception as error:
            raise self._read_failure(error) from error
        await self._sleep(wait)
        try:
            return await self._read_once(action, gateway, args)
        except Exception as error:
            raise self._read_failure(error) from error

    async def _read_once(
        self, action: ConsentAction, gateway: TelegramGateway, args: dict[str, Any]
    ) -> Any:
        if action.run is None:
            raise ToolFailure("not_allowed", NOT_ALLOWED_MESSAGE)
        # The wait for a place counts in the call's own time limit.
        async with asyncio.timeout(self.call_limit), self.reads.place():
            return await action.run(gateway, None, args)

    def _may_wait_out(self, seconds: int, arrived: datetime) -> bool:
        if seconds > LONGEST_READ_FLOOD_WAIT_SECONDS:
            return False
        answered_by = self._seconds_since(arrived) + seconds + self.call_limit
        return answered_by <= READ_ANSWER_LIMIT_SECONDS

    async def _queue_write(
        self,
        connection: Connection,
        approval: PendingApproval,
        run: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Run one write from the write line. Its approval is already used, so whatever
        happens from here on, it is never sent again."""
        item = WriteItem(
            tool=approval.tool,
            approval_id=approval.id,
            arrived_at=self._arrival(connection),
            is_open=connection.is_open,
            run=run,
        )
        try:
            return await self.writes.submit(item)
        except WriteDropped as dropped:
            raise ToolFailure.fixed("telegram_timeout", "not_sent") from dropped
        except LineFull as full:
            raise ToolFailure("telegram_error", MESSAGES["line_full"]) from full
        except ToolFailure:
            raise
        except Exception as error:
            raise self._write_failure(error) from error

    async def _run_write(
        self,
        action: ConsentAction,
        gateway: TelegramGateway,
        approval: PendingApproval,
        args: dict[str, Any],
    ) -> Any:
        """Run exactly what was approved (BR3.9), from the write line."""
        if action.run is None:
            raise ToolFailure("not_allowed", NOT_ALLOWED_MESSAGE)
        if self._logging_out:  # a logout started while this write waited: the session is gone
            raise ToolFailure.fixed("no_session")
        try:
            async with asyncio.timeout(self.call_limit):
                return await action.run(gateway, approval, args)
        except Exception as error:
            raise self._write_failure(error) from error

    async def _mark(
        self,
        connection: Connection,
        action: ConsentAction,
        gateway: TelegramGateway,
        args: dict[str, Any],
        digest: str,
    ) -> tuple[Any, PendingApproval | None]:
        """BR13.3, BR13.6: mark up to what was counted; with no approval, check again and
        never mark what is unread."""
        chat_id, topic_id = int(args["chat"]), args.get("topic_id")
        try:
            approval = self._consume(action.tool, digest)
        except ToolFailure as failure:
            if failure.code != "needs_confirmation":
                raise
            state = await self._unread(gateway, chat_id, topic_id, bounded=True)
            if state.unread:
                raise
            return {"chat_id": chat_id, "topic_id": topic_id, "marked": 0, "up_to": None}, None
        upto = approval.mark_upto or 0
        try:
            await self._mark_read(
                connection, gateway, approval, approval.chat_id or chat_id, upto, approval.topic_id
            )
        except ToolFailure as failure:
            failure.approval = approval
            raise
        result = {
            "chat_id": approval.chat_id,
            "topic_id": approval.topic_id,
            "marked": approval.prompt.unread,
            "up_to": upto,
        }
        return result, approval

    async def _read_and_mark(
        self,
        connection: Connection,
        action: ConsentAction,
        gateway: TelegramGateway,
        args: dict[str, Any],
        digest: str,
    ) -> tuple[Any, PendingApproval | None]:
        """The read tool asked to mark (BR13.3, BR13.7): the messages are always returned when
        the read runs; the chat is marked only with a confirmed approval, and never past the
        newest message returned or the newest one counted."""
        if not connection.is_claude_code:
            data = await self._run_read(connection, action, gateway, args)
            return _with_mark(data, MARK_NOTES["client"]), None
        approval = self.gate.consume(action.tool, digest)
        if approval is None:
            remembered = self.gate.recent_outcome(digest)
            if remembered is not None and remembered.outcome is not None:
                data = await self._run_read(connection, action, gateway, args)
                return _with_mark(data, MARK_NOTES[remembered.outcome]), None
            topic_id = args.get("topic_id")
            state = await self._unread(gateway, int(args["chat"]), topic_id, bounded=True)
            if state.unread:
                raise ToolFailure.fixed("needs_confirmation")
            data = await self._run_read(connection, action, gateway, args)
            return _with_mark(data, None), None
        data = await self._run_read(connection, action, gateway, args)
        newest = action.newest_returned(data) if action.newest_returned is not None else None
        if newest is None:
            return _with_mark(data, MARK_NOTES["empty"]), approval
        upto = min(newest, approval.mark_upto or 0)
        try:
            await self._mark_read(
                connection, gateway, approval, approval.chat_id or 0, upto, approval.topic_id
            )
        except ToolFailure as failure:
            return _with_mark(data, f"not marked: {failure.message}"), approval
        return _with_mark(data, None, marked=True), approval

    async def _mark_read(
        self,
        connection: Connection,
        gateway: TelegramGateway,
        approval: PendingApproval,
        chat_id: int,
        max_id: int,
        topic_id: int | None,
    ) -> None:
        """mark_read from the write line, under the call limit. A flood wait of 30 s or less
        is waited out once, outside the line (NFR5); a timeout is outcome_unknown and is never
        resent (NFR6.1)."""

        async def run() -> None:
            if self._logging_out:
                raise ToolFailure.fixed("no_session")
            async with asyncio.timeout(self.call_limit):
                await gateway.mark_read(chat_id, max_id, topic_id)

        for attempt in (1, 2):
            try:
                await self._queue_write(connection, approval, run)
                return
            except FloodWait as error:
                if attempt == 1 and error.seconds <= LONGEST_READ_FLOOD_WAIT_SECONDS:
                    await self._sleep(error.seconds)
                    continue
                raise _flood_failure(error.seconds) from error
            except ToolFailure as failure:
                if failure.code == "flood_wait" and failure.retry_after is not None:
                    if attempt == 1 and failure.retry_after <= LONGEST_READ_FLOOD_WAIT_SECONDS:
                        await self._sleep(failure.retry_after)
                        continue
                    raise _flood_failure(failure.retry_after) from failure
                if failure.code == "outcome_unknown":
                    raise ToolFailure.fixed("outcome_unknown", "mark_unknown") from failure
                raise

    def _read_failure(self, error: Exception) -> ToolFailure:
        if isinstance(error, ToolFailure):
            return error
        if isinstance(error, GatewayTimeout | TimeoutError):
            self.link.mark_dropped()
            return ToolFailure.fixed("telegram_timeout")
        return self._common_failure(error) or self._unexpected(error, "telegram_error")

    def _write_failure(self, error: Exception) -> ToolFailure:
        if isinstance(error, ToolFailure):
            return error
        if isinstance(error, FloodWait):
            return _flood_failure(error.seconds, sent_nothing=True)
        if isinstance(error, OutcomeUnknown | GatewayTimeout | TimeoutError | ConnectionError):
            if not isinstance(error, OutcomeUnknown):
                self.link.mark_dropped()
            return ToolFailure.fixed("outcome_unknown")
        return self._common_failure(error) or self._unexpected(error, "outcome_unknown")

    def _common_failure(self, error: Exception) -> ToolFailure | None:
        if isinstance(error, FloodWait):
            return _flood_failure(error.seconds)
        if isinstance(error, NotAuthorized):
            self._forget_session()
            return ToolFailure.fixed("no_session")
        if isinstance(error, TelegramError):
            reason = self.redactor.redact(error.reason)
            return ToolFailure("telegram_error", f"Telegram refused the request: {reason}.")
        if isinstance(error, ChatNotFound):
            return ToolFailure.fixed("not_found", "not_found_chat")
        return None

    def _unexpected(self, error: Exception, code: str) -> ToolFailure:
        self.diagnostics.exception("unexpected gateway error", error)
        if code == "outcome_unknown":
            return ToolFailure.fixed("outcome_unknown")
        return ToolFailure("telegram_error", "Telegram could not be reached; try again.")

    def _write_dropped(self, item: WriteItem, reason: DropReason) -> None:
        self.diagnostics.write(
            f"send not started ({reason}): {item.tool}, approval {item.approval_id}"
        )

    # Status, stop, the sweep -------------------------------------------------------------------

    async def _status(self, connection: Connection | None, params: dict[str, Any]) -> Any:
        """The vault, then (with a session) Telegram, within 15 s. Writes no log entry.
        ``clients`` and ``mcp_clients`` count the Claude Code sessions connected;
        ``idle_seconds`` is how long the broker had been idle before this request arrived."""
        before = self._last_activity
        if connection is not None and connection.activity_before is not None:
            before = connection.activity_before
        state, account, cause = await self._session_state()
        sessions = self.session_count()
        return {
            "logged_in": state == "logged_in",
            "account": account,
            "clients": sessions,
            "idle_seconds": max(0, int(self._seconds_since(before))),
            "session_state": state,
            "cause": cause,
            "pid": os.getpid(),
            "mcp_clients": sessions,
        }

    async def _session_state(self) -> tuple[str, str | None, str | None]:
        """(session_state, account, cause): logged_in, no_session, ended, keychain_locked or
        unreachable."""
        result = await self._read_vault()
        if result.outcome is VaultOutcome.KEYCHAIN_LOCKED:
            return "keychain_locked", None, result.cause
        if result.session is None:
            return "no_session", None, None
        try:
            async with asyncio.timeout(self.status_budget):
                gateway = await self.link.open(result.session)
                return "logged_in", account_label(await gateway.me()), None
        except (SessionRefused, NotAuthorized):
            self._forget_session()
            return "ended", None, None
        except Exception as error:  # any failure reaching Telegram is "unreachable"
            self.diagnostics.write(f"status check failed: {type(error).__name__}")
            return "unreachable", None, None

    def session_count(self) -> int:
        return sum(
            1
            for connection in self._connections
            if connection.authenticated
            and connection.client == SESSION_CLIENT
            and connection.is_open()
        )

    async def _stop_request(self, connection: Connection, params: dict[str, Any]) -> Any:
        if self._logging_out:  # a login starting now never cuts a logout in half
            await self._logout_answered.wait()
        asyncio.get_running_loop().call_soon(self._schedule_stop, None)
        return {"ok": True}

    def _schedule_stop(self, reason: str | None) -> None:
        if self._stopper is None and not self._stopped.is_set():
            self._stopper = asyncio.get_running_loop().create_task(self.stop(reason))

    def sweep(self) -> None:
        """One pass of the broker's timers: expire approvals, drop sends that can no longer
        start, replace the secret when it is due, and stop when idle."""
        self.sweep_expired()
        self.writes.drop_overdue()
        self.rotate_secret_if_due()
        self.stop_if_idle()

    # session.logout ---------------------------------------------------------------------------

    async def _logout(self, connection: Connection, params: dict[str, Any]) -> Any:
        """Refuse everything new, let a running send finish (the ones still waiting in the line
        are refused), end the session on Telegram, clear the Keychain whatever Telegram
        answered, log once, answer, then stop. A second logout that arrives meanwhile waits for
        the first and gets the same answer."""
        if self._logging_out:
            await self._logout_answered.wait()
            if self._logout_answer is not None:
                return self._logout_answer
            return ToolFailure.fixed("no_session").as_result()
        self._logging_out = True
        try:
            await self.writes.wait_idle()
            self.gate.drop_open()
            ended = await self._end_session()
            deleted = await asyncio.to_thread(self._vault.delete)
            self.sweep_expired()
            self._log_logout(connection, ended, deleted)
            self._forget_session()
            self._logout_answer = {
                "ok": deleted.cleared,
                "account": ended.account,
                "telegram_confirmed": ended.reason is None,
                "telegram_reason": ended.reason,
                "retry_after": ended.retry_after,
                "keychain_cleared": deleted.cleared,
                "keychain_cause": deleted.cause,
            }
        finally:
            self._logout_answered.set()
            asyncio.get_running_loop().call_soon(self._schedule_stop, None)
        return self._logout_answer

    async def _end_session(self) -> _Ended:
        result = await self._read_vault()
        if result.outcome is VaultOutcome.KEYCHAIN_LOCKED:
            return _Ended(None, "telegram_error", error_type="KeychainLocked")
        if result.session is None:
            return _Ended(None, "no_session")
        account: str | None = None
        try:
            async with asyncio.timeout(self.status_budget):
                gateway = await self.link.open(result.session)
                account = account_label(await gateway.me())
        except (SessionRefused, NotAuthorized) as refused:
            return _Ended(None, "no_session", error_type=type(refused).__name__)
        except Exception as error:  # the name is not essential: carry on with the logout
            self.diagnostics.write(f"logout: account lookup failed: {type(error).__name__}")
        try:
            async with asyncio.timeout(self.logout_budget):
                confirmed = await self.link.open(result.session)
                if await confirmed.log_out():
                    return _Ended(account, None)
                return _Ended(account, "telegram_error", error_type="NotConfirmed")
        except Exception as error:  # every failure is a reason, never an unanswered call
            reason, retry_after = _logout_reason(error)
            return _Ended(account, reason, retry_after, type(error).__name__)

    def _log_logout(
        self, connection: Connection, ended: _Ended, deleted: VaultDeleteResult
    ) -> None:
        if not deleted.cleared:
            result = "keychain_locked"
            if ended.reason is not None:
                self.diagnostics.write(
                    f"logout: Telegram did not confirm ({ended.reason}, {ended.error_type}) "
                    f"and the Keychain could not delete the session ({deleted.cause})"
                )
        else:
            result = ended.reason or "ok"
        self._log_entry(connection.label, "result", "logout", result, content=ended.account)

    def sweep_expired(self) -> None:
        """Expire approvals that are due and log each one: an unanswered question as a
        timed_out decision, an Allow not used in 60 s as a timed_out result (BR3.7, BR12)."""
        for approval, previous in self.gate.sweep():
            if previous is ApprovalStatus.PENDING:
                self._log_entry(
                    "broker",
                    "decision",
                    approval.tool,
                    "timed_out",
                    approval_id=approval.id,
                    chat_id=approval.chat_id,
                    decision="timed_out",
                )
            else:
                self._log_entry(
                    "broker",
                    "result",
                    approval.tool,
                    "timed_out",
                    approval_id=approval.id,
                    chat_id=approval.chat_id,
                    content=UNUSED_CONTENT,
                )

    def stop_if_idle(self) -> None:
        """Stop after ten minutes with no request, never while one runs. Open but silent
        connections, the sweep and the secret rotation are not activity."""
        if self._stopped.is_set() or self.running_requests or self.writes.busy:
            return
        idle = self._seconds_since(self._last_activity)
        if idle >= self.idle_limit:
            self._schedule_stop(f"idle stop after {int(idle // 60)} minutes with no request")

    def rotate_secret_if_due(self) -> None:
        """Replace the secret every hour. Connections already open stay accepted; a failed
        swap keeps the old secret in force and is tried again an hour later."""
        if self._seconds_since(self._secret_written_at) < self.secret_rotation:
            return
        self._secret_written_at = self._clock()
        value = new_secret()
        try:
            write_secret(self.paths.secret, value)
        except OSError as error:
            self.diagnostics.write(
                f"secret rotation failed ({type(error).__name__}); the old secret stays in force"
            )
            return
        self._secret = value
        self.diagnostics.write("secret rotated")

    async def _sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(self._sweep_interval)
            try:
                self.sweep()
            except Exception as error:  # a failed pass must not end the timers
                self.diagnostics.exception("sweep", error)

    def _check_settings(self) -> None:
        try:
            Settings.load(self.paths)
        except SettingsError as error:
            self.settings_refusal = error
            self.diagnostics.write(f"settings refused: {error}")
            with contextlib.suppress(OSError):
                self.log.append(
                    LogEntry("broker", "result", "settings", "not_allowed", content=str(error))
                )

    # Logging ---------------------------------------------------------------------------------

    def _log_rotation_failed(self, error: OSError) -> None:
        self.diagnostics.write(
            f"activity log rotation failed ({type(error).__name__}); the entry was kept in "
            "activity.log and the next entry tries again"
        )

    def _log_call(self, connection: Connection, tool: str, raw: object) -> None:
        """A write whose call entry cannot be written is refused."""
        chat_id, content = _peek(raw)
        try:
            self.log.append(
                LogEntry(connection.label, "call", tool, "ok", chat_id=chat_id, content=content)
            )
        except OSError as error:
            self.diagnostics.exception("activity log", error)
            if self.gate.kind_of(tool) is not ActionKind.READ:
                raise ToolFailure("telegram_error", MESSAGES["log_failed"]) from error

    def _log_result(
        self,
        connection: Connection,
        tool: str,
        result: str,
        approval: PendingApproval | None,
        raw: object,
    ) -> None:
        chat_id = approval.chat_id if approval is not None else _peek(raw)[0]
        self._log_entry(
            connection.label,
            "result",
            tool,
            result,
            approval_id=approval.id if approval is not None else None,
            chat_id=chat_id,
        )

    def _log_gate_events(self, connection: Connection) -> None:
        """BR12.5, BR15.1: every ask_again, honoured or not."""
        for event in self.gate.drain_events():
            self._log_entry(
                connection.label,
                "result",
                event.tool,
                event.code or "ok",
                approval_id=event.approval_id,
                content=ASK_AGAIN_CONTENT[event.kind],
            )

    def _log_approval(
        self,
        connection: Connection,
        event: EventKind,
        approval: PendingApproval,
        decision: DecisionKind | None,
        *,
        strict: bool = False,
    ) -> None:
        content = approval.question if event == "prompt" else None
        self._log_entry(
            connection.label,
            event,
            approval.tool,
            "ok",
            approval_id=approval.id,
            chat_id=approval.chat_id,
            decision=decision,
            content=content,
            strict=strict,
        )

    def _log_entry(
        self,
        client: str,
        event: EventKind,
        tool: str,
        result: str,
        *,
        approval_id: str | None = None,
        chat_id: int | None = None,
        decision: DecisionKind | None = None,
        content: str | None = None,
        strict: bool = False,
    ) -> None:
        """Append one entry. ``strict`` raises OSError when it cannot be written."""
        entry = LogEntry(
            client=client,
            event=event,
            tool=tool,
            result=result,
            chat_id=chat_id,
            approval_id=approval_id,
            content=content,
            decision=decision,
        )
        try:
            self.log.append(entry)
        except OSError as error:
            self.diagnostics.exception("activity log", error)
            if strict:
                raise


# Helpers --------------------------------------------------------------------------------------


def _flood_failure(seconds: int, *, sent_nothing: bool = False) -> ToolFailure:
    if sent_nothing:
        message = f"Telegram asked to wait {seconds} s; nothing was sent. Ask again after that."
    else:
        message = f"Telegram asked to wait {seconds} s; try again after that."
    return ToolFailure("flood_wait", message, retry_after=seconds)


def _split_ask_again(raw: object) -> tuple[object, bool]:
    """Take the reserved ``ask_again`` out before validation and the digest (BR12.5)."""
    if not isinstance(raw, dict) or ASK_AGAIN not in raw:
        return raw, False
    value = raw[ASK_AGAIN]
    if not isinstance(value, bool):
        raise ToolFailure("invalid_arguments", f"{ASK_AGAIN} must be true or false")
    return {k: v for k, v in raw.items() if k != ASK_AGAIN}, value


def _validated(action: ConsentAction, raw: object) -> tuple[dict[str, Any], str]:
    try:
        args = action.validate(raw)
        return args, canonical_digest(action.tool, args)
    except (InvalidArguments, DigestError) as error:
        raise ToolFailure("invalid_arguments", str(error)) from error


def _validated_read(action: ConsentAction, raw: object) -> dict[str, Any]:
    try:
        return action.validate(raw if raw is not None else {})
    except InvalidArguments as error:
        raise ToolFailure("invalid_arguments", str(error)) from error


def _check_forum(chat: ChatRef, topic_id: int | None) -> None:
    """BR13.4: a forum is marked one topic at a time; a topic needs a forum."""
    if chat.type == "forum" and topic_id is None:
        raise ToolFailure.fixed("invalid_arguments", "forum_needs_topic")
    if topic_id is not None and chat.type != "forum":
        raise ToolFailure.fixed("invalid_arguments", "topic_needs_forum")


def _well_formed(state: object) -> bool:
    if not isinstance(state, UnreadState):
        return False
    counts = (state.unread, state.newest_id)
    return all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in counts)


def _with_mark(data: Any, note: str | None, *, marked: bool = False) -> dict[str, Any]:
    result = dict(data) if isinstance(data, dict) else {"data": data}
    result["marked"] = marked
    result["mark_note"] = note
    return result


def _refusal(code: str, message: str, retry_after: int | None = None) -> dict[str, Any]:
    refusal: dict[str, Any] = {"decision": "refuse", "code": code, "message": message}
    if retry_after is not None:
        refusal["retry_after"] = retry_after
    return refusal


def _failure_for(method: str) -> Any:
    if method == "approval.check":
        return _refusal("telegram_error", "The confirmation check failed; nothing was prepared.")
    if method == "approval.answer":
        return {"outcome": "unknown"}
    return ToolFailure("telegram_error", "The request failed inside the broker.").as_result()


def _peek(raw: object) -> tuple[int | None, str | None]:
    """The chat id and text of raw arguments, for the log only, never for a decision."""
    if not isinstance(raw, dict):
        return None, None
    chat, text = raw.get("chat"), raw.get("text")
    chat_id = chat if isinstance(chat, int) and not isinstance(chat, bool) else None
    if isinstance(chat, str) and chat.lstrip("-").isdigit():
        chat_id = int(chat)
    return chat_id, text if isinstance(text, str) else None


def _text_list(value: object) -> list[str] | None:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return [str(item) for item in value]
    return None


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _prepare_data_dir(paths: FixedPaths) -> None:
    paths.data_dir.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileExistsError):
        paths.data_dir.mkdir(mode=PRIVATE_FOLDER)
    os.chmod(paths.data_dir, PRIVATE_FOLDER)


def _require_private_modes(paths: FixedPaths) -> None:
    """Refuse to serve when a file or the socket does not have its private mode."""
    expected = {
        paths.data_dir: PRIVATE_FOLDER,
        paths.secret: PRIVATE_FILE,
        paths.socket: PRIVATE_FILE,
        paths.lock: PRIVATE_FILE,
        paths.diagnostics: PRIVATE_FILE,
    }
    for path, mode in expected.items():
        actual = stat.S_IMODE(os.lstat(path).st_mode)
        if actual != mode:
            raise BrokerStartError(f"{path.name} has mode {actual:o}, not {mode:o}")


def default_gateway_factory(paths: FixedPaths) -> GatewayFactory:
    """The real gateway, or, only for a test run whose home folder is under /tmp, a fake one
    described by ``fake-gateway.json`` in that home folder."""
    if os.environ.get(FAKE_GATEWAY_ENV) == "1" and os.path.realpath(paths.home).startswith(
        "/private/tmp/"  # test runs only: the resolved home must be a temporary folder
    ):
        from telegram_mcp.gateway.fake import FAKE_CONFIG_NAME, FakeGateway

        fake = FakeGateway.from_file(paths.home / FAKE_CONFIG_NAME)
        return lambda session, api_id, api_hash: fake
    from telegram_mcp.gateway.telethon_gateway import TelethonGateway

    return lambda session, api_id, api_hash: TelethonGateway(session, api_id, api_hash)


async def serve(paths: FixedPaths) -> int:
    """Run a broker until it is stopped. Exit 0 also when another broker already won."""
    broker = Broker(paths, gateway_factory=default_gateway_factory(paths))
    try:
        await broker.start()
    except BrokerStartError as error:
        return 0 if "holds the lock" in str(error) else 1
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signal_number, lambda: asyncio.ensure_future(broker.stop()))
    await broker.wait_stopped()
    return 0


def run_broker(paths: FixedPaths) -> int:
    return asyncio.run(serve(paths))
