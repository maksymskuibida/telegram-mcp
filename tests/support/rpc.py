"""A bare JSON-RPC client over the broker socket, for tests that check the broker itself."""

from __future__ import annotations

import asyncio
import contextlib
import json
from itertools import count
from typing import Any

from telegram_mcp.paths import FixedPaths


class RawClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self._ids = count(1)

    @classmethod
    async def open(cls, paths: FixedPaths) -> RawClient:
        reader, writer = await asyncio.open_unix_connection(str(paths.socket), limit=2**24)
        return cls(reader, writer)

    @classmethod
    async def connect(
        cls,
        paths: FixedPaths,
        client: str = "mcp-server",
        client_name: str | None = "claude-code",
    ) -> RawClient:
        raw = await cls.open(paths)
        reply = await raw.call(
            "hello",
            {
                "protocol": 1,
                "secret": paths.secret.read_text().strip(),
                "client": client,
                "package_version": "test",
                "client_name": client_name,
                "client_version": "1.0",
            },
        )
        assert "result" in reply, reply
        return raw

    async def send_line(self, line: bytes) -> dict[str, Any] | None:
        self.writer.write(line)
        await self.writer.drain()
        answer = await asyncio.wait_for(self.reader.readline(), 5)
        return json.loads(answer) if answer else None

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params is not None:
            request["params"] = params
        reply = await self.send_line(json.dumps(request).encode() + b"\n")
        assert reply is not None, "the broker closed the connection"
        return reply

    async def result(self, method: str, params: dict[str, Any] | None = None) -> Any:
        reply = await self.call(method, params)
        assert "result" in reply, reply
        return reply["result"]

    async def close(self) -> None:
        self.writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await self.writer.wait_closed()
