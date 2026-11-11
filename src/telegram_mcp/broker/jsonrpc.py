"""JSON-RPC 2.0 framing for the broker socket: one UTF-8 JSON message per line (C1)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
VERSION_MISMATCH = -32001
BAD_SECRET = -32002


class RpcError(Exception):
    def __init__(self, code: int, message: str, request_id: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id


@dataclass(frozen=True)
class Request:
    id: Any
    method: str
    params: dict[str, Any]


def parse_request(line: bytes) -> Request:
    try:
        message = json.loads(line)
    except (ValueError, UnicodeDecodeError) as error:
        raise RpcError(PARSE_ERROR, "parse error") from error
    if not isinstance(message, dict):
        raise RpcError(INVALID_REQUEST, "a request must be a JSON object")
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params", {})
    if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
        raise RpcError(INVALID_REQUEST, "invalid request", request_id)
    if not isinstance(params, dict):
        raise RpcError(INVALID_REQUEST, "params must be an object", request_id)
    return Request(id=request_id, method=method, params=params)


def encode(message: dict[str, Any]) -> bytes:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def result_message(request_id: Any, result: Any) -> bytes:
    return encode({"jsonrpc": "2.0", "id": request_id, "result": result})


def error_message(request_id: Any, code: int, message: str) -> bytes:
    return encode({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def request_message(request_id: int, method: str, params: dict[str, Any]) -> bytes:
    return encode({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
