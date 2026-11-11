"""BrokerClient: how the MCP server, the hooks and login reach the broker (C1; BR2.2, BR2.4,
BR2.5).

A caller never holds the lock. When it cannot connect, it tests the lock: a login means
``login_in_progress``; a running broker means wait for its socket; a free lock means start a
broker in the background. It retries once, and only when its request was never written. A
write whose request was written and then lost is ``outcome_unknown`` and is never resent."""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from itertools import count
from typing import Any

from telegram_mcp import __version__
from telegram_mcp.broker import jsonrpc
from telegram_mcp.broker.lock import PROTOCOL_VERSION, LockProbe, probe_lock
from telegram_mcp.consent import ActionKind
from telegram_mcp.paths import FixedPaths
from telegram_mcp.tools import DECLS

STREAM_LIMIT = 2**20
POLL_SECONDS = 0.05

LOGIN_IN_PROGRESS = "A login is running in a terminal; try again when it has finished."
NOT_STARTED = "The background connection did not start; try again."
LOST = "The connection to the background process was lost; try again."
OUTCOME_UNKNOWN = (
    "The message may or may not have been sent; look at the chat before sending it again."
)


@dataclass(frozen=True)
class Timeouts:
    hello: float = 5.0
    answer: float = 5.0
    check: float = 12.0
    call: float = 75.0
    start: float = 5.0


class BrokerUnavailable(Exception):
    """Nothing was sent: the broker could not be reached or started."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RequestLost(Exception):
    """The request was written, then the answer did not come (dropped or timed out)."""

    def __init__(self, *, timed_out: bool) -> None:
        super().__init__("timed out" if timed_out else "connection lost")
        self.timed_out = timed_out


class BrokerRefused(Exception):
    """The broker answered with a JSON-RPC error."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class _NotReachable(Exception):
    pass


def spawn_broker() -> None:
    """Start ``telegram-mcp broker`` detached, with the same interpreter as this process."""
    subprocess.Popen(
        [sys.executable, "-m", "telegram_mcp.cli", "broker"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def is_write(tool: str) -> bool:
    decl = DECLS.get(tool)
    return decl is None or decl.kind is not ActionKind.READ


class BrokerClient:
    def __init__(
        self,
        paths: FixedPaths,
        client: str,
        *,
        client_name: str | None = None,
        client_version: str | None = None,
        spawn: Callable[[], None] = spawn_broker,
        timeouts: Timeouts | None = None,
    ) -> None:
        self._paths = paths
        self._client = client
        self.client_name = client_name
        self.client_version = client_version
        self._spawn = spawn
        self.timeouts = timeouts or Timeouts()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._ids = count(1)
        self._busy = asyncio.Lock()
        self.broker_pid: int | None = None

    # Public calls ----------------------------------------------------------------------------

    async def tool_call(self, tool: str, args: object) -> dict[str, Any]:
        """A ToolResult, never an exception: every failure becomes its error code."""
        try:
            result = await self.request(
                "tool.call", {"tool": tool, "args": args}, self.timeouts.call
            )
        except BrokerUnavailable as error:
            return _failure(error.code, error.message)
        except RequestLost as lost:
            if is_write(tool):
                return _failure("outcome_unknown", OUTCOME_UNKNOWN)
            if lost.timed_out:
                return _failure("telegram_timeout", "Telegram did not answer in time; try again.")
            return _failure("telegram_error", LOST)
        except BrokerRefused as refused:
            return _failure("telegram_error", f"The background process refused: {refused}.")
        if not isinstance(result, dict):
            return _failure("telegram_error", "The background process answered nonsense.")
        return result

    async def approval_check(self, tool: str, args: object) -> Any:
        return await self.request(
            "approval.check", {"tool": tool, "args": args}, self.timeouts.check
        )

    async def approval_answer(self, params: dict[str, Any]) -> Any:
        return await self.request("approval.answer", params, self.timeouts.answer)

    async def stop_broker(self) -> Any:
        return await self.request("broker.stop", {}, self.timeouts.answer)

    async def close(self) -> None:
        self._drop()

    # Requests ---------------------------------------------------------------------------------

    async def request(self, method: str, params: dict[str, Any], wait_seconds: float) -> Any:
        """Send one request. Raises BrokerUnavailable when nothing was sent, RequestLost when
        it was sent and no answer came, BrokerRefused on a JSON-RPC error."""
        async with self._busy:
            line = jsonrpc.request_message(next(self._ids), method, params)
            reader, writer = await self._connection()
            if not await self._write(writer, line):
                reader, writer = await self._connection()
                if not await self._write(writer, line):
                    raise BrokerUnavailable("telegram_error", LOST)
            return await self._answer(reader, wait_seconds)

    async def _write(self, writer: asyncio.StreamWriter, line: bytes) -> bool:
        """False when the write itself failed, so the request never reached the broker."""
        try:
            writer.write(line)
            await writer.drain()
        except (ConnectionError, OSError):
            self._drop()
            return False
        return True

    async def _answer(self, reader: asyncio.StreamReader, wait_seconds: float) -> Any:
        try:
            reply = await asyncio.wait_for(reader.readline(), wait_seconds)
        except TimeoutError:
            self._drop()
            raise RequestLost(timed_out=True) from None
        except (ConnectionError, OSError, ValueError):
            self._drop()
            raise RequestLost(timed_out=False) from None
        if not reply:
            self._drop()
            raise RequestLost(timed_out=False)
        message = json.loads(reply)
        if "error" in message:
            error = message["error"]
            raise BrokerRefused(int(error.get("code", 0)), str(error.get("message", "")))
        return message.get("result")

    async def _connection(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._reader is not None and self._writer is not None and not self._reader.at_eof():
            return self._reader, self._writer
        self._drop()
        try:
            return await self._open()
        except _NotReachable:
            await self._find_or_start()
        try:
            return await self._open()
        except _NotReachable:
            raise BrokerUnavailable("telegram_error", NOT_STARTED) from None

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            secret = self._paths.secret.read_text(encoding="ascii").strip()
            reader, writer = await asyncio.open_unix_connection(
                str(self._paths.socket), limit=STREAM_LIMIT
            )
        except (OSError, UnicodeDecodeError) as error:
            raise _NotReachable() from error
        hello = {
            "protocol": PROTOCOL_VERSION,
            "secret": secret,
            "client": self._client,
            "package_version": __version__,
            "client_name": self.client_name,
            "client_version": self.client_version,
        }
        self._reader, self._writer = reader, writer
        try:
            writer.write(jsonrpc.request_message(0, "hello", hello))
            await writer.drain()
            greeting = await self._answer(reader, self.timeouts.hello)
        except BrokerRefused as refused:
            self._drop()
            if refused.code == jsonrpc.VERSION_MISMATCH:
                raise BrokerUnavailable(
                    "version_mismatch",
                    "The background process is a different version; try again in a moment.",
                ) from None
            raise _NotReachable() from refused
        except (RequestLost, ConnectionError, OSError) as error:
            self._drop()
            raise _NotReachable() from error
        if isinstance(greeting, dict) and isinstance(greeting.get("pid"), int):
            self.broker_pid = greeting["pid"]
        return reader, writer

    async def _find_or_start(self) -> None:
        """BR2.2: login means wait for it; running means wait for the socket; free means start."""
        state = probe_lock(self._paths.lock)
        if state is LockProbe.LOGIN:
            raise BrokerUnavailable("login_in_progress", LOGIN_IN_PROGRESS)
        if state is LockProbe.FREE:
            self._spawn()
        deadline = time.monotonic() + self.timeouts.start
        while time.monotonic() < deadline:
            if self._paths.socket.exists() and self._paths.secret.exists():
                return
            if probe_lock(self._paths.lock) is LockProbe.LOGIN:
                raise BrokerUnavailable("login_in_progress", LOGIN_IN_PROGRESS)
            await asyncio.sleep(POLL_SECONDS)

    def _drop(self) -> None:
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
        self._reader = self._writer = None


def _failure(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}
