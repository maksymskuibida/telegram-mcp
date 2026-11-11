"""The ConnectionBroker (`telegram-mcp broker`): the one process that holds the Telegram
connection, the approvals and the activity log (C1; rules BR2.x, BR3.6, BR3.10, BR3.11).

Every tool call and approval check runs its checks in one fixed order (BR3.11):
authenticated connection, client allowed for writes, vault and session, kind, arguments and
digest, then either the chat lookup and the question (approval.check) or the single use of a
confirmed approval and the Telegram call (tool.call)."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import os
import secrets
import signal
import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from telegram_mcp import __version__
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
from telegram_mcp.broker.lock import PROTOCOL_VERSION, BrokerLock, LockState
from telegram_mcp.consent import (
    OPTION_DESCRIPTIONS,
    ActionKind,
    Allow,
    ApprovalStatus,
    Ask,
    ConsentGate,
    DigestError,
    PendingApproval,
    canonical_digest,
)
from telegram_mcp.gateway.models import (
    ChatNotFound,
    ChatRef,
    FloodWait,
    GatewayTimeout,
    NotAuthorized,
    OutcomeUnknown,
    TelegramError,
)
from telegram_mcp.gateway.protocol import GatewayFactory, TelegramGateway
from telegram_mcp.paths import FixedPaths
from telegram_mcp.settings import Settings, SettingsError
from telegram_mcp.tools import (
    DECLS,
    InvalidArguments,
    list_result,
    send_result,
    validate_args,
)
from telegram_mcp.vault import SessionVault, StoredSession, VaultOutcome, VaultReadResult

CHECK_BUDGET_SECONDS = 10.0
SWEEP_INTERVAL_SECONDS = 10.0
STREAM_LIMIT = 2**20
SECRET_BYTES = 32
CLIENT_KINDS = ("mcp-server", "approval-hook", "cli")
CLAUDE_CODE = "claude-code"
PRIVATE_FILE = 0o600
PRIVATE_FOLDER = 0o700
FAKE_GATEWAY_ENV = "TELEGRAM_MCP_TEST_FAKE_GATEWAY"

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
    "not_found_chat": "This account cannot see a chat with that id; tg_chats_list shows the ids.",
    "not_found_tool": "This version has no tool with that name.",
    "log_failed": "The message was not sent: could not write the activity log.",
}


class BrokerStartError(Exception):
    """The broker could not start: another broker or a login holds the lock, or a file did
    not get its private mode."""


class ToolFailure(Exception):
    def __init__(self, code: str, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after

    @classmethod
    def fixed(cls, code: str, key: str | None = None) -> ToolFailure:
        return cls(code, MESSAGES[key or code])

    def as_result(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.retry_after is not None:
            error["retry_after"] = self.retry_after
        return {"ok": False, "error": error}


@dataclass
class Connection:
    authenticated: bool = False
    client: str | None = None
    package_version: str | None = None
    client_name: str | None = None
    client_version: str | None = None

    @property
    def label(self) -> str:
        if self.client == "mcp-server":
            return f"mcp-server:{self.client_name or 'unknown'}"
        return self.client or "unknown"


Handler = Callable[[Connection, dict[str, Any]], Awaitable[Any]]


class Broker:
    def __init__(
        self,
        paths: FixedPaths,
        *,
        gateway_factory: GatewayFactory,
        vault: SessionVault | None = None,
        clock: Callable[[], datetime] = utc_now,
        check_budget: float = CHECK_BUDGET_SECONDS,
        sweep_interval: float = SWEEP_INTERVAL_SECONDS,
    ) -> None:
        self.paths = paths
        self.check_budget = check_budget
        self.gate = ConsentGate(DECLS, clock=clock)
        self.redactor = Redactor()
        self.log = ActivityLog(paths.log, self.redactor)
        self.diagnostics = Diagnostics(paths.diagnostics, self.redactor)
        self.settings_refusal: SettingsError | None = None
        self._clock = clock
        self._gateway_factory = gateway_factory
        self._vault = vault or SessionVault(paths.keychain_service)
        self._sweep_interval = sweep_interval
        self._lock = BrokerLock(paths.lock)
        self._secret = ""
        self._server: asyncio.Server | None = None
        self._sweeper: asyncio.Task[None] | None = None
        self._vault_result: VaultReadResult | None = None
        self._gateway: TelegramGateway | None = None
        self._write_lock = asyncio.Lock()
        self._writers: set[asyncio.StreamWriter] = set()
        self._stopped = asyncio.Event()
        self._finished = asyncio.Event()
        self._exit_when_idle = False
        self._last_request = clock()
        self._handlers: dict[str, Handler] = {
            "tool.call": self._tool_call,
            "approval.check": self._approval_check,
            "approval.answer": self._approval_answer,
            "broker.status": self._status,
            "broker.stop": self._stop_request,
        }

    # Start and stop -----------------------------------------------------------------------

    async def start(self) -> None:
        """Hold the lock, then create the secret and the socket with private modes (BR2.2,
        BR2.3), read the vault once and check the settings (BR7.5)."""
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
        self._sweeper = asyncio.create_task(self._sweep_forever())

    async def _open_endpoint(self) -> None:
        for leftover in (self.paths.socket, self.paths.secret):
            leftover.unlink(missing_ok=True)
        self._secret = secrets.token_hex(SECRET_BYTES)
        descriptor = os.open(
            self.paths.secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, PRIVATE_FILE
        )
        try:
            os.write(descriptor, self._secret.encode("ascii"))
        finally:
            os.close(descriptor)
        previous = os.umask(0o077)
        try:
            self._server = await asyncio.start_unix_server(
                self._serve_connection, path=str(self.paths.socket), limit=STREAM_LIMIT
            )
        finally:
            os.umask(previous)
        os.chmod(self.paths.socket, PRIVATE_FILE)

    async def stop(self) -> None:
        """Clean exit: stop serving, rewrite the lock to released, delete socket and secret."""
        if self._stopped.is_set():
            await self._finished.wait()
            return
        self._stopped.set()
        try:
            await self._shut_down()
        finally:
            self._finished.set()

    async def _shut_down(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
        for writer in list(self._writers):
            writer.close()
        if self._gateway is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._gateway.disconnect(), 5)
        await self._close_endpoint()
        self.diagnostics.write("broker stopped")

    async def _close_endpoint(self) -> None:
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
        connection = Connection()
        self._writers.add(writer)
        try:
            while not self._stopped.is_set():
                try:
                    line = await reader.readline()
                except (ValueError, ConnectionError):
                    break
                if not line:
                    break
                reply, close = await self._handle_line(connection, line)
                if reply:
                    writer.write(reply)
                    await writer.drain()
                if close:
                    break
        except ConnectionError:
            pass
        finally:
            self._writers.discard(writer)
            writer.close()
            if self._exit_when_idle and not self._writers:
                asyncio.get_running_loop().create_task(self.stop())

    async def _handle_line(self, connection: Connection, line: bytes) -> tuple[bytes, bool]:
        try:
            request = jsonrpc.parse_request(line)
        except jsonrpc.RpcError as error:
            reply = jsonrpc.error_message(error.request_id, error.code, error.message)
            return reply, not connection.authenticated
        self._last_request = self._clock()
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

    # Session ----------------------------------------------------------------------------------

    async def _read_vault(self) -> VaultReadResult:
        result = await asyncio.to_thread(self._vault.load)
        self._vault_result = result
        if result.session is not None:
            self.redactor.set_secrets([result.session.session, result.session.api_hash])
        return result

    async def _session(self) -> StoredSession:
        """Step 3 of BR3.11: no_session or keychain_locked before any Telegram call."""
        result = self._vault_result
        if result is None or result.outcome is not VaultOutcome.LOADED:
            result = await self._read_vault()
        if result.outcome is VaultOutcome.KEYCHAIN_LOCKED:
            raise ToolFailure.fixed("keychain_locked")
        if result.session is None:
            raise ToolFailure.fixed("no_session")
        return result.session

    async def _authorized_gateway(self, stored: StoredSession) -> TelegramGateway:
        if self._gateway is None:
            self._gateway = self._gateway_factory(stored.session, stored.api_id, stored.api_hash)
            await self._gateway.connect()
        if not await self._gateway.is_authorized():
            self._forget_session()
            raise ToolFailure.fixed("no_session")
        return self._gateway

    def _forget_session(self) -> None:
        """The session is no longer valid: re-read the vault on the next call (BR1.6)."""
        self._vault_result = None
        self._gateway = None

    # approval.check -------------------------------------------------------------------------

    async def _approval_check(self, connection: Connection, params: dict[str, Any]) -> Any:
        try:
            async with asyncio.timeout(self.check_budget):
                return await self._decide(connection, params)
        except TimeoutError:
            return _refusal("telegram_timeout", MESSAGES["telegram_timeout"])
        except ToolFailure as failure:
            return _refusal(failure.code, failure.message)

    async def _decide(self, connection: Connection, params: dict[str, Any]) -> dict[str, Any]:
        stored = await self._session()
        tool = params.get("tool")
        if not isinstance(tool, str) or (tool not in DECLS and self._is_write(tool)):
            raise ToolFailure.fixed("not_found", "not_found_tool")
        if not self._is_write(tool):
            return {"decision": "allow"}
        args, digest = _validated(tool, params.get("args"))
        gateway = await self._authorized_gateway(stored)
        chat = await _resolve(gateway, int(args["chat"]))
        decision = self.gate.check(tool, digest, chat, args)
        if isinstance(decision, Allow):
            return {"decision": "allow"}
        if isinstance(decision, Ask):
            self._log_approval(connection, "prompt", decision.approval, None)
            return {
                "decision": "ask",
                "approval_id": decision.approval.id,
                "question": decision.approval.question,
                "options": list(decision.approval.options),
                "option_descriptions": list(OPTION_DESCRIPTIONS),
                "multi_select": False,
            }
        return _refusal(decision.code, decision.message)

    def _is_write(self, tool: str) -> bool:
        return self.gate.kind_of(tool) is not ActionKind.READ

    # approval.answer ------------------------------------------------------------------------

    async def _approval_answer(self, connection: Connection, params: dict[str, Any]) -> Any:
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
        return {"outcome": str(answer.outcome)}

    # tool.call -------------------------------------------------------------------------------

    async def _tool_call(self, connection: Connection, params: dict[str, Any]) -> Any:
        tool = params.get("tool")
        if not isinstance(tool, str):
            return ToolFailure("invalid_arguments", "tool must be a tool name").as_result()
        raw = params.get("args")
        approval: PendingApproval | None = None
        try:
            if self._is_write(tool) and connection.client_name != CLAUDE_CODE:
                raise ToolFailure.fixed("unsupported_client")
            # BR8.2: the vault is read (and the redactor given its secrets) before any entry
            # holding the caller's text is written, also after a start with no session.
            stored = await self._session()
            self._log_call(connection, tool, raw)
            if tool not in DECLS:
                raise ToolFailure.fixed("not_found", "not_found_tool")
            if not self._is_write(tool):
                data = await self._run_read(tool, stored, raw)
                self._log_result(connection, tool, "ok", None, None)
                return {"ok": True, "data": data}
            _, digest = _validated(tool, raw)
            gateway = await self._authorized_gateway(stored)
            approval = self.gate.consume(tool, digest)
            if approval is None:
                raise ToolFailure.fixed("needs_confirmation")
            data = await self._run_send(gateway, approval)
        except ToolFailure as failure:
            self._log_result(connection, tool, failure.code, approval, raw)
            return failure.as_result()
        self._log_result(connection, tool, "ok", approval, raw)
        return {"ok": True, "data": data}

    async def _run_read(self, tool: str, stored: StoredSession, raw: object) -> dict[str, Any]:
        args = _validated_read(tool, raw)
        gateway = await self._authorized_gateway(stored)
        try:
            chats, next_offset = await gateway.list_chats(
                args["folder_id"], args["limit"], args["offset"]
            )
        except Exception as error:
            raise self._read_failure(error) from error
        return list_result(chats, next_offset)

    async def _run_send(self, gateway: TelegramGateway, approval: PendingApproval) -> Any:
        """Send exactly what was approved, to the chat stored at check time (BR3.9). The
        approval is already used, so nothing here is ever resent."""
        async with self._write_lock:
            try:
                sent = await gateway.send_text(
                    approval.chat_id, approval.content, approval.reply_to, approval.topic_id
                )
            except Exception as error:
                raise self._write_failure(error) from error
        return send_result(sent)

    def _read_failure(self, error: Exception) -> ToolFailure:
        if isinstance(error, GatewayTimeout | TimeoutError):
            return ToolFailure.fixed("telegram_timeout")
        return self._common_failure(error) or self._unexpected(error, "telegram_error")

    def _write_failure(self, error: Exception) -> ToolFailure:
        if isinstance(error, OutcomeUnknown | GatewayTimeout | TimeoutError | ConnectionError):
            return ToolFailure.fixed("outcome_unknown")
        return self._common_failure(error) or self._unexpected(error, "outcome_unknown")

    def _common_failure(self, error: Exception) -> ToolFailure | None:
        if isinstance(error, FloodWait):
            message = f"Telegram asked to wait {error.seconds} s; try again after that."
            return ToolFailure("flood_wait", message, retry_after=error.seconds)
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

    # Status, stop, expiry ----------------------------------------------------------------------

    async def _status(self, connection: Connection, params: dict[str, Any]) -> Any:
        idle = (self._clock() - self._last_request).total_seconds()
        loaded = self._vault_result is not None and self._vault_result.session is not None
        return {
            "logged_in": loaded,
            "account": None,
            "clients": len(self._writers),
            "idle_seconds": max(0, int(idle)),
        }

    async def _stop_request(self, connection: Connection, params: dict[str, Any]) -> Any:
        asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self.stop()))
        return {"ok": True}

    def sweep_expired(self) -> None:
        """Expire approvals that are due and log each one (BR3.7)."""
        for approval, previous in self.gate.sweep():
            if previous is ApprovalStatus.PENDING:
                self._log_entry(
                    "broker", "decision", approval.tool, "timed_out", approval, decision="timed_out"
                )
            else:
                self._log_entry("broker", "result", approval.tool, "timed_out", approval)

    async def _sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(self._sweep_interval)
            self.sweep_expired()

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

    def _log_call(self, connection: Connection, tool: str, raw: object) -> None:
        """BR8.1: a write whose call entry cannot be written is refused."""
        chat_id, content = _peek(raw)
        try:
            self.log.append(
                LogEntry(connection.label, "call", tool, "ok", chat_id=chat_id, content=content)
            )
        except OSError as error:
            self.diagnostics.exception("activity log", error)
            if self._is_write(tool):
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
        self._log_entry(connection.label, "result", tool, result, approval, chat_id=chat_id)

    def _log_approval(
        self,
        connection: Connection,
        event: EventKind,
        approval: PendingApproval,
        decision: DecisionKind | None,
    ) -> None:
        content = approval.question if event == "prompt" else None
        self._log_entry(
            connection.label,
            event,
            approval.tool,
            "ok",
            approval,
            decision=decision,
            content=content,
        )

    def _log_entry(
        self,
        client: str,
        event: EventKind,
        tool: str,
        result: str,
        approval: PendingApproval | None,
        *,
        decision: DecisionKind | None = None,
        content: str | None = None,
        chat_id: int | None = None,
    ) -> None:
        entry = LogEntry(
            client=client,
            event=event,
            tool=tool,
            result=result,
            chat_id=chat_id if chat_id is not None else (approval.chat_id if approval else None),
            approval_id=approval.id if approval is not None else None,
            content=content,
            decision=decision,
        )
        try:
            self.log.append(entry)
        except OSError as error:
            self.diagnostics.exception("activity log", error)


# Helpers --------------------------------------------------------------------------------------


def _validated(tool: str, raw: object) -> tuple[dict[str, Any], str]:
    try:
        args = validate_args(tool, raw)
        return args, canonical_digest(tool, args)
    except (InvalidArguments, DigestError) as error:
        raise ToolFailure("invalid_arguments", str(error)) from error


def _validated_read(tool: str, raw: object) -> dict[str, Any]:
    try:
        return validate_args(tool, raw if raw is not None else {})
    except InvalidArguments as error:
        raise ToolFailure("invalid_arguments", str(error)) from error


async def _resolve(gateway: TelegramGateway, chat_id: int) -> ChatRef:
    try:
        return await gateway.resolve_chat(chat_id)
    except ChatNotFound as error:
        raise ToolFailure.fixed("not_found", "not_found_chat") from error
    except NotAuthorized as error:
        raise ToolFailure.fixed("no_session") from error
    except GatewayTimeout as error:
        raise ToolFailure.fixed("telegram_timeout") from error
    except FloodWait as error:
        message = f"Telegram asked to wait {error.seconds} s; try again after that."
        raise ToolFailure("flood_wait", message, retry_after=error.seconds) from error
    except TelegramError as error:
        raise ToolFailure("telegram_error", "Telegram refused the chat lookup.") from error


def _refusal(code: str, message: str) -> dict[str, Any]:
    return {"decision": "refuse", "code": code, "message": message}


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
    """NFR7.3: refuse to serve when a file or the socket does not have its private mode."""
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
