from __future__ import annotations

import hashlib
import shutil
import socket
import ssl
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from threading import Thread
from typing import cast

import pytest

from tests.support import ftps_conformance_client as client_module
from tests.support.ftps_conformance_client import (
    FtpsConformanceClient,
    FtpsConformanceError,
    FtpsConformanceResult,
    generate_access_code,
)

Handler = Callable[[socket.socket, ssl.SSLContext], None]


class ReplySocket:
    def __init__(self, value: bytes) -> None:
        self._value = value

    def recv(self, _size: int) -> bytes:
        if not self._value:
            return b""
        value, self._value = self._value[:1], self._value[1:]
        return value


class AddressSocket:
    def __init__(self, address: object) -> None:
        self._address = address

    def getpeername(self) -> object:
        return self._address

    def getsockname(self) -> object:
        return self._address


class BrokenChunks:
    def __iter__(self) -> Iterator[bytes]:
        raise RuntimeError("secret must not escape")


@contextmanager
def running_server(
    certificate: Path,
    private_key: Path,
    handler: Handler,
    *,
    tls: bool = True,
) -> Iterator[int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    context = server_context(certificate, private_key)
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            raw, _peer = listener.accept()
            with raw:
                if tls:
                    with context.wrap_socket(raw, server_side=True) as control:
                        handler(control, context)
                else:
                    handler(raw, context)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=serve)
    thread.start()
    try:
        yield int(listener.getsockname()[1])
    finally:
        listener.close()
        thread.join(timeout=2)
        if thread.is_alive():
            pytest.fail("test FTPS server did not stop")
        if errors:
            raise errors[0]


def server_context(certificate: Path, private_key: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(certificate, private_key)
    return context


def generate_certificate(tmp_path: Path) -> tuple[Path, Path]:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("OpenSSL CLI is required for the loopback FTPS contract test")
    certificate = tmp_path / "cert.pem"
    private_key = tmp_path / "key.pem"
    subprocess.run(  # noqa: S603 -- fixed local test-only certificate generation.
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return certificate, private_key


def send_reply(connection: socket.socket, code: int, text: str = "ready") -> None:
    connection.sendall(f"{code:03d} {text}\r\n".encode("ascii"))


def send_raw_reply(connection: socket.socket, value: bytes) -> None:
    connection.sendall(value)


def read_command(connection: socket.socket, expected: str) -> None:
    line = bytearray()
    while not line.endswith(b"\r\n"):
        chunk = connection.recv(1)
        if not chunk:
            raise AssertionError("client closed before command")
        line.extend(chunk)
    assert line == expected.encode("ascii") + b"\r\n"


def scripted_handler(responses: list[bytes]) -> Handler:
    def handle(control: socket.socket, _context: ssl.SSLContext) -> None:
        send_raw_reply(control, responses[0])
        for response in responses[1:]:
            control.recv(128)
            send_raw_reply(control, response)

    return handle


ACCESS_CODE = "A" * 20


def exact_cleanup_handler(control: socket.socket, _context: ssl.SSLContext) -> None:
    send_reply(control, 220, "service ready")
    read_command(control, "USER bblp")
    send_reply(control, 331, "password required")
    read_command(control, "PASS " + ACCESS_CODE)
    send_reply(control, 230, "logged in")
    read_command(control, "PBSZ 0")
    send_reply(control, 200)
    read_command(control, "PROT P")
    send_reply(control, 200)
    read_command(control, "DELE /observation.3mf")
    send_reply(control, 250, "deleted")
    read_command(control, "QUIT")
    send_reply(control, 221, "bye")


def exact_upload_handler(
    received: list[bytes],
    access_code: str = ACCESS_CODE,
    expected_payload: bytes | None = None,
) -> Handler:
    def handle(control: socket.socket, context: ssl.SSLContext) -> None:
        send_reply(control, 220, "service ready")
        read_command(control, "USER bblp")
        send_reply(control, 331, "password required")
        read_command(control, "PASS " + access_code)
        send_reply(control, 230, "logged in")
        read_command(control, "PBSZ 0")
        send_reply(control, 200)
        read_command(control, "PROT P")
        send_reply(control, 200)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as passive:
            passive.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            passive.bind(("127.0.0.1", 0))
            passive.listen(1)
            passive_port = int(passive.getsockname()[1])
            p1, p2 = divmod(passive_port, 256)
            read_command(control, "PASV")
            send_reply(control, 227, f"Entering Passive Mode (127,0,0,1,{p1},{p2}).")
            read_command(control, "STOR /observation.3mf")
            send_reply(control, 150, "opening data")
            raw_data, _peer = passive.accept()
            with raw_data, context.wrap_socket(raw_data, server_side=True) as data:
                while expected_payload is None or sum(map(len, received)) < len(expected_payload):
                    try:
                        chunk = data.recv(4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    received.append(chunk)
                if expected_payload is not None:
                    data.unwrap().close()
        send_reply(control, 226, "transfer complete")
        try:
            read_command(control, "QUIT")
            send_reply(control, 221, "bye")
        except (OSError, AssertionError):
            pass

    return handle


@pytest.fixture
def certificate_files(tmp_path: Path) -> tuple[Path, Path]:
    return generate_certificate(tmp_path)


def client(  # noqa: PLR0913 -- the test wrapper mirrors the helper's explicit bounds.
    port: int,
    access_code: str = ACCESS_CODE,
    *,
    remote_path: str = "/observation.3mf",
    timeout: float = 2.0,
    max_reply_bytes: int = 512,
    max_transfer_bytes: int = 64 * 1024,
) -> FtpsConformanceClient:
    return FtpsConformanceClient(
        "127.0.0.1",
        port,
        access_code,
        remote_path=remote_path,
        timeout=timeout,
        max_reply_bytes=max_reply_bytes,
        max_transfer_bytes=max_transfer_bytes,
    )


def test_cleanup_drives_exact_reply_sequence_without_secret_in_result(
    certificate_files: tuple[Path, Path],
) -> None:
    certificate, private_key = certificate_files
    with running_server(certificate, private_key, exact_cleanup_handler) as port:
        result = client(port).cleanup()

    assert result == FtpsConformanceResult(
        operation="cleanup",
        remote_path="/observation.3mf",
        reply_codes=(220, 331, 230, 200, 200, 250, 221),
        passive_endpoint=None,
        data_peer=None,
        data_tls_version=None,
        byte_count=0,
        sha256=None,
    )
    assert ACCESS_CODE not in repr(result)


def test_upload_drives_tls13_passive_session_and_hashes_bounded_chunks(
    certificate_files: tuple[Path, Path],
) -> None:
    certificate, private_key = certificate_files
    received: list[bytes] = []
    expected = b"first-second"
    payload = [b"", b"first", b"-", b"second"]
    with running_server(
        certificate, private_key, exact_upload_handler(received, expected_payload=expected)
    ) as port:
        result = client(port).upload(payload)

    assert b"".join(received) == expected
    assert result.operation == "upload"
    assert result.reply_codes == (220, 331, 230, 200, 200, 227, 150, 226, 221)
    assert result.passive_endpoint is not None
    assert result.passive_endpoint[0] == "127.0.0.1"
    assert result.passive_endpoint[1] > 0
    assert result.data_peer == "127.0.0.1"
    assert result.data_tls_version == "TLSv1.3"
    assert result.byte_count == len(expected)
    assert result.sha256 == "sha256:" + hashlib.sha256(expected).hexdigest()


def test_access_code_is_ephemeral_and_protocol_shaped() -> None:
    first = generate_access_code()
    second = generate_access_code()

    assert len(first) == 20
    assert client_module._ACCESS_CODE.fullmatch(first)
    assert first != second


def test_internal_reply_and_stream_failures_are_sanitized() -> None:
    with pytest.raises(FtpsConformanceError):
        client_module._expect(cast(ssl.SSLSocket, ReplySocket(b"421 denied\r\n")), 220, 64)
    with pytest.raises(FtpsConformanceError):
        client_module._read_reply(cast(ssl.SSLSocket, ReplySocket(b"220 \xff\r\n")), 64)
    with pytest.raises(FtpsConformanceError):
        client_module._send_chunks(cast(ssl.SSLSocket, None), None, 1)

    with pytest.raises(FtpsConformanceError) as denied:
        client_module._send_chunks(cast(ssl.SSLSocket, None), BrokenChunks(), 1)
    assert "secret" not in str(denied.value)


@pytest.mark.parametrize("address", [None, (), ("not-an-ip", 1), ("198.51.100.1", 1)])
def test_internal_socket_address_validation_is_fail_closed(address: object) -> None:
    with pytest.raises(FtpsConformanceError):
        client_module._peer_ip(cast(socket.socket, AddressSocket(address)))


def test_control_tls_version_mismatch_is_denied(
    certificate_files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    certificate, private_key = certificate_files
    monkeypatch.setattr(client_module, "_TLS_VERSION", "TLSv1.2")

    def initial_reply(control: socket.socket, _context: ssl.SSLContext) -> None:
        with suppress(OSError):
            send_raw_reply(control, b"220 ready\r\n")

    with (
        running_server(certificate, private_key, initial_reply) as port,
        pytest.raises(FtpsConformanceError),
    ):
        client(port).cleanup()


@pytest.mark.parametrize(
    ("host", "port", "access_code", "options"),
    [
        ("localhost", 1, ACCESS_CODE, {}),
        ("127.0.0.2", 1, ACCESS_CODE, {}),
        ("127.0.0.1", 0, ACCESS_CODE, {}),
        ("127.0.0.1", 65536, ACCESS_CODE, {}),
        ("127.0.0.1", True, ACCESS_CODE, {}),
        ("127.0.0.1", 1, "short", {}),
        ("127.0.0.1", 1, ACCESS_CODE, {"remote_path": "/../observation.3mf"}),
        ("127.0.0.1", 1, ACCESS_CODE, {"timeout": 0.0}),
        ("127.0.0.1", 1, ACCESS_CODE, {"timeout": 2}),
        ("127.0.0.1", 1, ACCESS_CODE, {"max_reply_bytes": 7}),
        ("127.0.0.1", 1, ACCESS_CODE, {"max_transfer_bytes": 0}),
    ],
)
def test_configuration_is_fail_closed(
    host: str, port: int, access_code: str, options: dict[str, object]
) -> None:
    with pytest.raises(FtpsConformanceError):
        FtpsConformanceClient(host, port, access_code, **options)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "raw_reply",
    [
        b"421 unavailable\r\n",
        b"220-continued\r\n",
        b"xx ready\r\n",
        b"220\xff\r\n",
        b"220 " + b"x" * 20 + b"\r\n",
        b"220",
    ],
)
def test_alternate_malformed_and_truncated_replies_are_denied(
    certificate_files: tuple[Path, Path], raw_reply: bytes
) -> None:
    certificate, private_key = certificate_files
    handler = scripted_handler([raw_reply])
    with (
        running_server(certificate, private_key, handler) as port,
        pytest.raises(FtpsConformanceError) as denied,
    ):
        client(port, "S" * 20, timeout=0.1, max_reply_bytes=16).cleanup()

    assert "SSSS" not in str(denied.value)


def test_plaintext_server_and_explicit_tls_upgrade_are_not_accepted(
    certificate_files: tuple[Path, Path],
) -> None:
    certificate, private_key = certificate_files

    def plaintext(control: socket.socket, _context: ssl.SSLContext) -> None:
        control.sendall(b"220 service ready\r\n")

    with (
        running_server(certificate, private_key, plaintext, tls=False) as port,
        pytest.raises(FtpsConformanceError),
    ):
        client(port).cleanup()

    def explicit_upgrade(control: socket.socket, _context: ssl.SSLContext) -> None:
        control.recv(128)
        control.sendall(b"220 service ready\r\n")

    with (
        running_server(certificate, private_key, explicit_upgrade, tls=False) as port,
        pytest.raises(FtpsConformanceError),
    ):
        client(port).cleanup()


@pytest.mark.parametrize(
    "pasv_text",
    [
        "Entering Passive Mode.",
        "Entering Passive Mode (127,0,0,1,1).",
        "Entering Passive Mode (127,0,0,1,x,1).",
        "Entering Passive Mode (127,0,0,1,256,1).",
        "Entering Passive Mode (127,0,0,1,0,0).",
        "Entering Passive Mode (8,8,8,8,1,1).",
        "Entering Passive Mode (127,0,0,2,1,1).",
    ],
)
def test_malformed_or_unsafe_pasv_endpoint_is_denied(
    certificate_files: tuple[Path, Path], pasv_text: str
) -> None:
    certificate, private_key = certificate_files
    responses = [
        b"220 ready\r\n",
        b"331 password\r\n",
        b"230 logged in\r\n",
        b"200 pbsz\r\n",
        b"200 prot\r\n",
        f"227 {pasv_text}\r\n".encode("ascii"),
    ]
    with (
        running_server(certificate, private_key, scripted_handler(responses)) as port,
        pytest.raises(FtpsConformanceError),
    ):
        client(port).upload([b"payload"])


def test_data_peer_mismatch_is_denied_and_does_not_expose_access_code(
    certificate_files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    certificate, private_key = certificate_files
    received: list[bytes] = []
    original = client_module._peer_ip
    calls = 0

    def mismatched_peer(connection: socket.socket | ssl.SSLSocket, *, local: bool = False) -> str:
        nonlocal calls
        calls += 1
        if calls == 3 and not local:
            return "127.0.0.2"
        return original(connection, local=local)

    monkeypatch.setattr(client_module, "_peer_ip", mismatched_peer)
    with (
        running_server(certificate, private_key, exact_upload_handler(received, "Z" * 20)) as port,
        pytest.raises(FtpsConformanceError) as denied,
    ):
        client(port, "Z" * 20).upload([b"payload"])

    assert "ZZZZ" not in str(denied.value)


def test_data_tls_version_mismatch_is_denied(
    certificate_files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    certificate, private_key = certificate_files
    received: list[bytes] = []
    original = client_module._peer_ip
    calls = 0

    def change_expected_version(
        connection: socket.socket | ssl.SSLSocket, *, local: bool = False
    ) -> str:
        nonlocal calls
        calls += 1
        result = original(connection, local=local)
        if calls == 1 and not local:
            monkeypatch.setattr(client_module, "_TLS_VERSION", "TLSv1.2")
        return result

    monkeypatch.setattr(client_module, "_peer_ip", change_expected_version)
    with (
        running_server(certificate, private_key, exact_upload_handler(received)) as port,
        pytest.raises(FtpsConformanceError),
    ):
        client(port).upload([b"payload"])


def test_excess_reply_is_denied(certificate_files: tuple[Path, Path]) -> None:
    certificate, private_key = certificate_files
    responses = [
        b"220 ready\r\n",
        b"331 password\r\n",
        b"230 logged in\r\n",
        b"200 pbsz\r\n",
        b"200 prot\r\n",
        b"250 deleted\r\n",
        b"221 bye\r\n999 extra\r\n",
    ]
    with (
        running_server(certificate, private_key, scripted_handler(responses)) as port,
        pytest.raises(FtpsConformanceError, match="excess"),
    ):
        client(port).cleanup()


def test_upload_bound_and_chunk_type_are_denied_without_secretful_errors(
    certificate_files: tuple[Path, Path],
) -> None:
    certificate, private_key = certificate_files
    received: list[bytes] = []
    with (
        running_server(certificate, private_key, exact_upload_handler(received, "K" * 20)) as port,
        pytest.raises(FtpsConformanceError) as oversized,
    ):
        client(port, "K" * 20, max_transfer_bytes=3).upload([b"12", b"34"])
    assert "KKKK" not in str(oversized.value)

    with (
        running_server(certificate, private_key, exact_upload_handler(received)) as port,
        pytest.raises(FtpsConformanceError),
    ):
        client(port).upload([bytearray(b"not bytes")])  # type: ignore[list-item]


def test_upload_data_disconnect_is_denied(certificate_files: tuple[Path, Path]) -> None:
    certificate, private_key = certificate_files

    def close_data(control: socket.socket, context: ssl.SSLContext) -> None:
        send_reply(control, 220)
        read_command(control, "USER bblp")
        send_reply(control, 331)
        read_command(control, "PASS " + ACCESS_CODE)
        send_reply(control, 230)
        read_command(control, "PBSZ 0")
        send_reply(control, 200)
        read_command(control, "PROT P")
        send_reply(control, 200)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as passive:
            passive.bind(("127.0.0.1", 0))
            passive.listen(1)
            p1, p2 = divmod(int(passive.getsockname()[1]), 256)
            read_command(control, "PASV")
            send_reply(control, 227, f"pasv (127,0,0,1,{p1},{p2})")
            read_command(control, "STOR /observation.3mf")
            send_reply(control, 150)
            raw_data, _peer = passive.accept()
            raw_data.close()

    with (
        running_server(certificate, private_key, close_data) as port,
        pytest.raises(FtpsConformanceError),
    ):
        client(port, timeout=0.1).upload([b"payload"])
