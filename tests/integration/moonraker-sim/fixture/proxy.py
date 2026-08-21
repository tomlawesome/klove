from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import ssl
import sys
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

MAX_HEADER_BYTES = 64 * 1024
MAX_BODY_BYTES = 64 * 1024
MAX_WIRE_BODY_BYTES = MAX_HEADER_BYTES + MAX_BODY_BYTES
CONTROL_METHODS = {
    "pause": "printer.print.pause",
    "resume": "printer.print.resume",
    "cancel": "printer.print.cancel",
    "start": "printer.print.start",
}
OBSERVED_METHODS = frozenset(
    (
        *CONTROL_METHODS.values(),
        "server.files.upload",
    )
)
START_RECONCILIATION_METHODS = frozenset({"server.history.list", "printer.objects.query"})
START_RECONCILIATION_BLACKOUT_SECONDS = 2.0


def _environment_port(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if not raw.isascii() or not raw.isdecimal():
        raise ValueError(f"{name} must be a decimal port")
    port = int(raw)
    if not 1 <= port <= 65535:
        raise ValueError(f"{name} is outside the valid port range")
    return port


def _environment_listen_host(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError as error:
        raise ValueError(f"{name} must be an exact IP address") from error


def _environment_upstream(name: str, default: str) -> tuple[str, int, bool]:
    raw = os.environ.get(name)
    value = default if raw is None or raw == "" else raw
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{name} contains an invalid port") from error
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not parsed.hostname.isascii()
    ):
        raise ValueError(f"{name} must be an uncredentialed HTTP(S) origin")
    if any(character.isspace() or ord(character) < 32 for character in parsed.hostname):
        raise ValueError(f"{name} contains an invalid host")
    tls = parsed.scheme == "https"
    actual_port = port if port is not None else (443 if tls else 80)
    if not 1 <= actual_port <= 65535:
        raise ValueError(f"{name} contains an invalid port")
    return parsed.hostname, actual_port, tls


def _environment_host_header(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    if (
        len(raw) > 255
        or not raw.isascii()
        or any(character.isspace() or not 33 <= ord(character) <= 126 for character in raw)
    ):
        raise ValueError(f"{name} contains an invalid HTTP Host value")
    parsed = urlsplit(f"http://{raw}")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{name} contains an invalid HTTP Host value") from error
    if (
        parsed.netloc != raw
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError(f"{name} contains an invalid HTTP Host value")
    return raw


PROXY_LISTEN_HOST = _environment_listen_host(
    "KLOVE_TEST_PROXY_LISTEN_HOST",
    "0.0.0.0",  # noqa: S104 -- isolated fixture network.
)
PROXY_LISTEN_PORT = _environment_port("KLOVE_TEST_PROXY_LISTEN_PORT", 7125)
CONTROL_LISTEN_HOST = _environment_listen_host(
    "KLOVE_TEST_PROXY_CONTROL_LISTEN_HOST",
    "0.0.0.0",  # noqa: S104 -- fixture network.
)
CONTROL_LISTEN_PORT = _environment_port("KLOVE_TEST_PROXY_CONTROL_LISTEN_PORT", 9126)
TARGET_HOST, TARGET_PORT, TARGET_TLS = _environment_upstream(
    "KLOVE_TEST_PROXY_UPSTREAM_URL", "http://printer-host:7125"
)
TARGET_HOST_HEADER = _environment_host_header("KLOVE_TEST_MOONRAKER_HOST_HEADER")


@dataclass(slots=True)
class ProxyState:
    armed_method: str | None = None
    counts: dict[str, int] = field(
        default_factory=lambda: {method: 0 for method in OBSERVED_METHODS}
    )
    reconciliation_blackout_until: float = 0.0


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


async def _read_chunked_body(reader: asyncio.StreamReader) -> bytes:
    body = bytearray()
    decoded_bytes = 0
    while True:
        chunk_line = await reader.readuntil(b"\r\n")
        body.extend(chunk_line)
        if len(body) > MAX_WIRE_BODY_BYTES:
            raise ValueError("HTTP response is too large")
        size_token = chunk_line[:-2].partition(b";")[0]
        if not size_token or any(
            character not in b"0123456789abcdefABCDEF" for character in size_token
        ):
            raise ValueError("HTTP chunk size is malformed")
        try:
            chunk_size = int(size_token, 16)
        except ValueError as error:
            raise ValueError("HTTP chunk size is malformed") from error
        if chunk_size < 0 or decoded_bytes + chunk_size > MAX_BODY_BYTES:
            raise ValueError("HTTP response is too large")
        if chunk_size == 0:
            trailer_bytes = 0
            while True:
                trailer_line = await reader.readuntil(b"\r\n")
                body.extend(trailer_line)
                trailer_bytes += len(trailer_line)
                if trailer_bytes > MAX_HEADER_BYTES or len(body) > MAX_WIRE_BODY_BYTES:
                    raise ValueError("HTTP response trailer is too large")
                if trailer_line == b"\r\n":
                    return bytes(body)
        chunk = await reader.readexactly(chunk_size + 2)
        if not chunk.endswith(b"\r\n"):
            raise ValueError("HTTP chunk is malformed")
        body.extend(chunk)
        decoded_bytes += chunk_size
        if len(body) > MAX_WIRE_BODY_BYTES:
            raise ValueError("HTTP response is too large")


async def _read_response(reader: asyncio.StreamReader) -> bytes:
    head = await _read_head(reader)
    _status_line, headers = _parse_head(head)
    content_length_header = headers.get("content-length")
    transfer_encoding = headers.get("transfer-encoding")
    if content_length_header is not None and transfer_encoding is not None:
        raise ValueError("HTTP response framing is ambiguous")
    if transfer_encoding is not None:
        if transfer_encoding.casefold() != "chunked":
            raise ValueError("HTTP transfer encoding is unsupported")
        return head + await _read_chunked_body(reader)
    if content_length_header is None:
        body = bytearray()
        while chunk := await reader.read(16 * 1024):
            body.extend(chunk)
            if len(body) > MAX_BODY_BYTES:
                raise ValueError("HTTP response is too large")
        return head + bytes(body)
    try:
        content_length = int(content_length_header)
    except ValueError as error:
        raise ValueError("HTTP content length is malformed") from error
    if content_length < 0 or content_length > MAX_BODY_BYTES:
        raise ValueError("HTTP response is too large")
    return head + await reader.readexactly(content_length)


def _rewrite_head(head: bytes, *, close: bool) -> bytes:
    lines = head.decode("ascii", errors="strict").split("\r\n")
    rewritten = [lines[0]]
    host_replaced = False
    for line in lines[1:-2]:
        lowered = line.casefold()
        if close and lowered.startswith("connection:"):
            continue
        if lowered.startswith("host:") and TARGET_HOST_HEADER is not None:
            rewritten.append(f"Host: {TARGET_HOST_HEADER}")
            host_replaced = True
        else:
            rewritten.append(line)
    if TARGET_HOST_HEADER is not None and not host_replaced:
        rewritten.append(f"Host: {TARGET_HOST_HEADER}")
    if close:
        rewritten.append("Connection: close")
    return ("\r\n".join([*rewritten, "", ""])).encode("ascii")


async def _open_upstream() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    if TARGET_TLS:
        context = ssl.create_default_context()
        return await asyncio.open_connection(
            TARGET_HOST,
            TARGET_PORT,
            ssl=context,
            server_hostname=TARGET_HOST,
        )
    return await asyncio.open_connection(TARGET_HOST, TARGET_PORT)


def _jsonrpc_method(request_line: str, body: bytes) -> str | None:
    if not request_line.startswith("POST /server/jsonrpc "):
        return None
    document: Any = json.loads(body)
    if not isinstance(document, dict):
        return None
    method = document.get("method")
    return method if isinstance(method, str) else None


def _observed_method(request_line: str, body: bytes) -> str | None:
    """Return one fixture-observed narrow production transport method."""
    method = _jsonrpc_method(request_line, body)
    if method is not None:
        return method
    if request_line == "POST /server/files/upload HTTP/1.1":
        return "server.files.upload"
    return None


async def _read_request_body(reader: asyncio.StreamReader, headers: dict[str, str]) -> bytes:
    """Read one bounded request body without normalizing its wire framing."""
    content_length_header = headers.get("content-length")
    transfer_encoding = headers.get("transfer-encoding")
    if content_length_header is not None and transfer_encoding is not None:
        raise ValueError("HTTP request framing is ambiguous")
    if transfer_encoding is not None:
        if transfer_encoding.casefold() != "chunked":
            raise ValueError("HTTP transfer encoding is unsupported")
        return await _read_chunked_body(reader)
    if content_length_header is None:
        return b""
    content_length = int(content_length_header)
    if content_length < 0 or content_length > MAX_BODY_BYTES:
        raise ValueError("HTTP body is too large")
    return await reader.readexactly(content_length)


async def _proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    remote_writer: asyncio.StreamWriter | None = None
    try:
        head = await _read_head(reader)
        request_line, headers = _parse_head(head)
        body = await _read_request_body(reader, headers)
        remote_reader, remote_writer = await _open_upstream()
        if headers.get("upgrade", "").casefold() == "websocket":
            remote_writer.write(_rewrite_head(head, close=False) + body)
            await remote_writer.drain()
            await asyncio.gather(_pipe(reader, remote_writer), _pipe(remote_reader, writer))
            return

        method = _observed_method(request_line, body)
        drop_response = False
        if method in STATE.counts:
            STATE.counts[method] += 1
            if STATE.armed_method == method:
                STATE.armed_method = None
                drop_response = True
                if method == CONTROL_METHODS["start"]:
                    STATE.reconciliation_blackout_until = (
                        time.monotonic() + START_RECONCILIATION_BLACKOUT_SECONDS
                    )
        elif (
            method in START_RECONCILIATION_METHODS
            and time.monotonic() < STATE.reconciliation_blackout_until
        ):
            drop_response = True

        remote_writer.write(_rewrite_head(head, close=True) + body)
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
            response = _http_response(
                HTTPStatus.OK,
                {"counts": dict(STATE.counts)},
            )
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
    proxy = await asyncio.start_server(_proxy, host=PROXY_LISTEN_HOST, port=PROXY_LISTEN_PORT)
    control = await asyncio.start_server(
        _control, host=CONTROL_LISTEN_HOST, port=CONTROL_LISTEN_PORT
    )
    async with proxy, control:
        await asyncio.gather(proxy.serve_forever(), control.serve_forever())


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        sys.exit(0)
