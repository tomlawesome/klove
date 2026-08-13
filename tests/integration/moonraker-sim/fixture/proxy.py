from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any

TARGET_HOST = "printer-host"
TARGET_PORT = 7125
MAX_HEADER_BYTES = 64 * 1024
MAX_BODY_BYTES = 64 * 1024
CONTROL_METHODS = {
    "pause": "printer.print.pause",
    "resume": "printer.print.resume",
    "cancel": "printer.print.cancel",
}


@dataclass(slots=True)
class ProxyState:
    armed_method: str | None = None
    counts: dict[str, int] = field(
        default_factory=lambda: {method: 0 for method in CONTROL_METHODS.values()}
    )


STATE = ProxyState()


async def _read_head(reader: asyncio.StreamReader) -> bytes:
    head = await reader.readuntil(b"\r\n\r\n")
    if len(head) > MAX_HEADER_BYTES:
        raise ValueError("HTTP header is too large")
    return head


def _parse_head(head: bytes) -> tuple[str, dict[str, str]]:
    lines = head.decode("ascii", errors="strict").split("\r\n")
    request_line = lines[0]
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, separator, value = line.partition(":")
        if not separator or name.casefold() in headers:
            raise ValueError("invalid HTTP header")
        headers[name.casefold()] = value.strip()
    return request_line, headers


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(16 * 1024):
            writer.write(chunk)
            await writer.drain()
    finally:
        writer.close()


async def _read_response(reader: asyncio.StreamReader) -> bytes:
    head = await _read_head(reader)
    _status_line, headers = _parse_head(head)
    content_length = int(headers.get("content-length", "0"))
    if content_length < 0 or content_length > MAX_BODY_BYTES:
        raise ValueError("HTTP response is too large")
    return head + await reader.readexactly(content_length)


def _connection_close(head: bytes) -> bytes:
    lines = head.decode("ascii", errors="strict").split("\r\n")
    kept = [line for line in lines[:-2] if not line.casefold().startswith("connection:")]
    return ("\r\n".join([*kept, "Connection: close", "", ""])).encode("ascii")


def _jsonrpc_method(request_line: str, body: bytes) -> str | None:
    if not request_line.startswith("POST /server/jsonrpc "):
        return None
    document: Any = json.loads(body)
    if not isinstance(document, dict):
        return None
    method = document.get("method")
    return method if isinstance(method, str) else None


async def _proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    remote_writer: asyncio.StreamWriter | None = None
    try:
        head = await _read_head(reader)
        request_line, headers = _parse_head(head)
        content_length = int(headers.get("content-length", "0"))
        if content_length < 0 or content_length > MAX_BODY_BYTES:
            raise ValueError("HTTP body is too large")
        body = await reader.readexactly(content_length)
        remote_reader, remote_writer = await asyncio.open_connection(TARGET_HOST, TARGET_PORT)
        if headers.get("upgrade", "").casefold() == "websocket":
            remote_writer.write(head + body)
            await remote_writer.drain()
            await asyncio.gather(_pipe(reader, remote_writer), _pipe(remote_reader, writer))
            return

        method = _jsonrpc_method(request_line, body)
        drop_response = False
        if method in STATE.counts:
            STATE.counts[method] += 1
            if STATE.armed_method == method:
                STATE.armed_method = None
                drop_response = True

        remote_writer.write(_connection_close(head) + body)
        await remote_writer.drain()
        response = await _read_response(remote_reader)
        if not drop_response:
            writer.write(response)
            await writer.drain()
    except (
        asyncio.IncompleteReadError,
        ConnectionError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
    ):
        pass
    finally:
        if remote_writer is not None:
            remote_writer.close()
        writer.close()


def _http_response(status: HTTPStatus, document: dict[str, object]) -> bytes:
    body = json.dumps(document, separators=(",", ":")).encode("utf-8")
    return (
        f"HTTP/1.1 {status.value} {status.phrase}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii") + body


async def _control(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        head = await _read_head(reader)
        request_line, _headers = _parse_head(head)
        method, target, _version = request_line.split(" ", 2)
        if method == "GET" and target == "/health":
            response = _http_response(HTTPStatus.OK, {"status": "ok"})
        elif method == "GET" and target == "/stats":
            response = _http_response(HTTPStatus.OK, {"counts": dict(STATE.counts)})
        elif method == "POST" and target.startswith("/arm/"):
            operation = target.removeprefix("/arm/")
            armed = CONTROL_METHODS.get(operation)
            if armed is None or STATE.armed_method is not None:
                response = _http_response(HTTPStatus.CONFLICT, {"error": "invalid_state"})
            else:
                STATE.armed_method = armed
                response = _http_response(HTTPStatus.OK, {"status": "armed"})
        else:
            response = _http_response(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        writer.write(response)
        await writer.drain()
    except (asyncio.IncompleteReadError, UnicodeError, ValueError):
        pass
    finally:
        writer.close()


async def _main() -> None:
    proxy = await asyncio.start_server(_proxy, host="0.0.0.0", port=7125)  # noqa: S104
    control = await asyncio.start_server(_control, host="0.0.0.0", port=9126)  # noqa: S104
    async with proxy, control:
        await asyncio.gather(proxy.serve_forever(), control.serve_forever())


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        sys.exit(0)
