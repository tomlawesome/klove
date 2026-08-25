"""Loopback-only black-box client for the accepted ADR-0010 FTPS profile."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
import select
import socket
import ssl
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal

_ACCESS_CODE_ALPHABET: Final = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
_ACCESS_CODE = re.compile(r"[A-Za-z0-9_-]{20}", re.ASCII)
_PATH = re.compile(r"/[A-Za-z0-9][A-Za-z0-9._-]*\.3mf", re.ASCII)
_PASV = re.compile(r"[^()]*\(([^()]*)\)[^()]*", re.ASCII)
_TLS_VERSION: Final = "TLSv1.3"


class FtpsConformanceError(RuntimeError):
    """A bounded FTPS conformance exchange failed without exposing peer data."""


@dataclass(frozen=True, slots=True)
class FtpsConformanceResult:
    """Sanitized evidence from one complete cleanup or upload exchange."""

    operation: Literal["cleanup", "upload"]
    remote_path: str
    reply_codes: tuple[int, ...]
    passive_endpoint: tuple[str, int] | None
    data_peer: str | None
    data_tls_version: str | None
    byte_count: int
    sha256: str | None


def generate_access_code() -> str:
    """Generate one ephemeral access code accepted by the observed protocol."""
    return "".join(secrets.choice(_ACCESS_CODE_ALPHABET) for _ in range(20))


class FtpsConformanceClient:
    """Drive only the exact loopback ADR-0010 cleanup and upload sessions."""

    def __init__(  # noqa: PLR0913 -- these bounds are the helper's explicit test contract.
        self,
        host: str,
        port: int,
        access_code: str,
        *,
        remote_path: str = "/observation.3mf",
        timeout: float = 2.0,
        max_reply_bytes: int = 512,
        max_transfer_bytes: int = 64 * 1024,
    ) -> None:
        if host not in {"127.0.0.1", "::1"}:
            raise FtpsConformanceError("loopback host required")
        if type(port) is not int or not 1 <= port <= 65535:
            raise FtpsConformanceError("invalid control port")
        if type(access_code) is not str or _ACCESS_CODE.fullmatch(access_code) is None:
            raise FtpsConformanceError("invalid access code")
        if type(remote_path) is not str or _PATH.fullmatch(remote_path) is None:
            raise FtpsConformanceError("invalid remote path")
        if type(timeout) is not float or timeout <= 0:
            raise FtpsConformanceError("invalid timeout")
        if type(max_reply_bytes) is not int or max_reply_bytes < 8:
            raise FtpsConformanceError("invalid reply bound")
        if type(max_transfer_bytes) is not int or max_transfer_bytes < 1:
            raise FtpsConformanceError("invalid transfer bound")
        self._host = host
        self._port = port
        self._access_code = access_code
        self._remote_path = remote_path
        self._timeout = timeout
        self._max_reply_bytes = max_reply_bytes
        self._max_transfer_bytes = max_transfer_bytes

    def cleanup(self) -> FtpsConformanceResult:
        """Run the exact login, protected delete, and disconnect session."""
        return self._exchange("cleanup", None)

    def upload(self, chunks: Iterable[bytes]) -> FtpsConformanceResult:
        """Run the exact login, passive protected upload, and disconnect session."""
        return self._exchange("upload", chunks)

    def _exchange(  # noqa: PLR0915 -- the transcript is intentionally visible in one audit path.
        self, operation: Literal["cleanup", "upload"], chunks: Iterable[bytes] | None
    ) -> FtpsConformanceResult:
        try:
            context = _client_context()
            reply_codes: list[int] = []
            passive_endpoint: tuple[str, int] | None = None
            data_peer: str | None = None
            data_tls_version: str | None = None
            byte_count = 0
            sha256: str | None = None
            with (
                socket.create_connection((self._host, self._port), self._timeout) as raw_control,
                context.wrap_socket(raw_control, server_hostname="localhost") as control,
            ):
                control.settimeout(self._timeout)
                if control.version() != _TLS_VERSION:
                    raise FtpsConformanceError("control TLS version rejected")
                control_peer = _peer_ip(control)
                control_local = _peer_ip(control, local=True)
                reply_codes.append(_expect(control, 220, self._max_reply_bytes).code)
                _send(control, "USER", "bblp")
                reply_codes.append(_expect(control, 331, self._max_reply_bytes).code)
                _send(control, "PASS", self._access_code)
                reply_codes.append(_expect(control, 230, self._max_reply_bytes).code)
                _send(control, "PBSZ", "0")
                reply_codes.append(_expect(control, 200, self._max_reply_bytes).code)
                _send(control, "PROT", "P")
                reply_codes.append(_expect(control, 200, self._max_reply_bytes).code)
                if operation == "cleanup":
                    _send(control, "DELE", self._remote_path)
                    reply_codes.append(_expect(control, 250, self._max_reply_bytes).code)
                else:
                    _send(control, "PASV")
                    pasv_reply = _expect(control, 227, self._max_reply_bytes)
                    reply_codes.append(pasv_reply.code)
                    passive_endpoint = _parse_pasv(pasv_reply.text)
                    if passive_endpoint[0] != control_local:
                        raise FtpsConformanceError("passive address rejected")
                    _send(control, "STOR", self._remote_path)
                    reply_codes.append(_expect(control, 150, self._max_reply_bytes).code)
                    with (
                        socket.create_connection(passive_endpoint, self._timeout) as raw_data,
                        context.wrap_socket(raw_data, server_hostname="localhost") as data,
                    ):
                        data.settimeout(self._timeout)
                        data_peer = _peer_ip(data)
                        if data_peer != control_peer:
                            raise FtpsConformanceError("data peer rejected")
                        data_tls_version = data.version()
                        if data_tls_version != _TLS_VERSION:
                            raise FtpsConformanceError("data TLS version rejected")
                        byte_count, sha256 = _send_chunks(data, chunks, self._max_transfer_bytes)
                        data.unwrap().close()
                    reply_codes.append(_expect(control, 226, self._max_reply_bytes).code)
                _send(control, "QUIT")
                reply_codes.append(_expect(control, 221, self._max_reply_bytes).code)
                _reject_extra_reply(control, self._timeout)
            return FtpsConformanceResult(
                operation=operation,
                remote_path=self._remote_path,
                reply_codes=tuple(reply_codes),
                passive_endpoint=passive_endpoint,
                data_peer=data_peer,
                data_tls_version=data_tls_version,
                byte_count=byte_count,
                sha256=sha256,
            )
        except FtpsConformanceError:
            raise
        except Exception:
            raise FtpsConformanceError("FTPS conformance exchange failed") from None


@dataclass(frozen=True, slots=True)
class _Reply:
    code: int
    text: str


def _client_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _send(connection: ssl.SSLSocket, command: str, argument: str | None = None) -> None:
    line = command if argument is None else f"{command} {argument}"
    connection.sendall(line.encode("ascii") + b"\r\n")


def _expect(connection: ssl.SSLSocket, expected: int, max_bytes: int) -> _Reply:
    reply = _read_reply(connection, max_bytes)
    if reply.code != expected:
        raise FtpsConformanceError("unexpected FTP reply code")
    return reply


def _read_reply(connection: ssl.SSLSocket, max_bytes: int) -> _Reply:
    line = bytearray()
    while not line.endswith(b"\r\n"):
        chunk = connection.recv(1)
        if not chunk:
            raise FtpsConformanceError("FTP reply ended early")
        line.extend(chunk)
        if len(line) > max_bytes:
            raise FtpsConformanceError("FTP reply exceeded bound")
    if len(line) < 5 or not line[:3].isdigit() or line[3:4] != b" ":
        raise FtpsConformanceError("malformed FTP reply")
    try:
        text = line[4:-2].decode("ascii")
    except UnicodeDecodeError:
        raise FtpsConformanceError("malformed FTP reply") from None
    return _Reply(int(line[:3]), text)


def _parse_pasv(text: str) -> tuple[str, int]:
    match = _PASV.fullmatch(text)
    if match is None:
        raise FtpsConformanceError("malformed PASV reply")
    fields = match.group(1).split(",")
    if len(fields) != 6:
        raise FtpsConformanceError("malformed PASV reply")
    try:
        values = [int(field) for field in fields]
    except ValueError:
        raise FtpsConformanceError("malformed PASV reply") from None
    if any(value < 0 or value > 255 for value in values):
        raise FtpsConformanceError("malformed PASV reply")
    address = ".".join(str(value) for value in values[:4])
    port = values[4] * 256 + values[5]
    parsed_address = ipaddress.ip_address(address)
    if not parsed_address.is_loopback or port == 0:
        raise FtpsConformanceError("passive endpoint rejected")
    return str(parsed_address), port


def _peer_ip(connection: socket.socket | ssl.SSLSocket, *, local: bool = False) -> str:
    address = connection.getsockname() if local else connection.getpeername()
    if not isinstance(address, tuple) or not address or not isinstance(address[0], str):
        raise FtpsConformanceError("socket address rejected")
    try:
        parsed_address = ipaddress.ip_address(address[0])
    except ValueError:
        raise FtpsConformanceError("socket address rejected") from None
    if not parsed_address.is_loopback:
        raise FtpsConformanceError("non-loopback peer rejected")
    return str(parsed_address)


def _send_chunks(
    connection: ssl.SSLSocket, chunks: Iterable[bytes] | None, max_bytes: int
) -> tuple[int, str]:
    if chunks is None:
        raise FtpsConformanceError("upload data missing")
    digest = hashlib.sha256()
    byte_count = 0
    try:
        for chunk in chunks:
            if type(chunk) is not bytes:
                raise FtpsConformanceError("upload chunk rejected")
            next_count = byte_count + len(chunk)
            if next_count > max_bytes:
                raise FtpsConformanceError("upload exceeded bound")
            if chunk:
                connection.sendall(chunk)
            digest.update(chunk)
            byte_count = next_count
    except FtpsConformanceError:
        raise
    except Exception:
        raise FtpsConformanceError("upload failed") from None
    return byte_count, f"sha256:{digest.hexdigest()}"


def _reject_extra_reply(connection: ssl.SSLSocket, timeout: float) -> None:
    ready, _, _ = select.select([connection], [], [], timeout)
    if not ready:
        raise FtpsConformanceError("FTP close timed out")
    if connection.recv(1):
        raise FtpsConformanceError("excess FTP replies")
