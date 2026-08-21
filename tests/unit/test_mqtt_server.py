from __future__ import annotations

import asyncio
import shutil
import ssl
import subprocess
from pathlib import Path
from typing import cast

import pytest

from klove.config import GroveBridgeConfig
from klove.domain.control import ControlOperation, ControlResult, ControlStatus
from klove.domain.translation import DecodedCommand
from klove.northbound.mqtt.codec import (
    MAX_REPORT_BYTES,
    _encode_remaining_length,
    encode_connack,
    encode_pingresp,
    encode_puback,
    encode_report,
    encode_suback,
)
from klove.northbound.mqtt.server import MqttTlsServer, _tls_context
from klove.security.compatibility import CompatibilityPrincipal

SERIAL = "KLOVE-01234567-89AB-CDEF-0123-456789ABCDEF"
OTHER_SERIAL = "KLOVE-FEDCBA98-7654-3210-FEDC-BA9876543210"
ACCESS_CODE = "X" * 20
REQUEST = f"device/{SERIAL}/request"
REPORT = f"device/{SERIAL}/report"
PRINCIPAL = CompatibilityPrincipal(
    printer_uuid="01234567-89ab-4def-8123-456789abcdef",
    proxy_serial=SERIAL,
    record_revision=1,
    control_enabled=True,
    dispatch_enabled=False,
)


def mqtt_string(value: str) -> bytes:
    encoded = value.encode()
    return len(encoded).to_bytes(2, "big") + encoded


def frame(first: int, body: bytes = b"") -> bytes:
    return bytes([first]) + _encode_remaining_length(len(body)) + body


def connect(*, serial: str = SERIAL, password: str = ACCESS_CODE) -> bytes:
    body = (
        mqtt_string("MQTT")
        + b"\x04\xc2\x00\x1e"
        + mqtt_string(f"bambuddy_{serial}_1_1")
        + mqtt_string("bblp")
        + mqtt_string(password)
    )
    return frame(0x10, body)


def subscribe(*, serial: str = SERIAL, packet_id: int = 2) -> bytes:
    body = (
        packet_id.to_bytes(2, "big")
        + mqtt_string(f"device/{serial}/report")
        + b"\x00"
        + mqtt_string(f"device/{serial}/request")
        + b"\x00"
    )
    return frame(0x82, body)


def publish(payload: bytes, *, serial: str = SERIAL, packet_id: int = 3) -> bytes:
    body = mqtt_string(f"device/{serial}/request") + packet_id.to_bytes(2, "big") + payload
    return frame(0x32, body)


def config(  # noqa: PLR0913 -- explicit runtime bounds under test.
    tmp_path: Path,
    *,
    port: int = 18883,
    max_sessions: int = 4,
    max_sessions_per_printer: int = 2,
    max_commands: int = 3,
    idle: float = 0.2,
    shutdown: float = 0.2,
) -> GroveBridgeConfig:
    return GroveBridgeConfig.model_construct(
        enabled=True,
        listen_host="127.0.0.1",
        mqtt_port=port,
        ftps_advertised_ipv4="127.0.0.1",
        tls_certificate_file=tmp_path / "cert.pem",
        tls_private_key_file=tmp_path / "key.pem",
        max_sessions=max_sessions,
        max_sessions_per_printer=max_sessions_per_printer,
        max_commands_per_session=max_commands,
        session_idle_seconds=idle,
        shutdown_timeout_seconds=shutdown,
    )


class Authenticator:
    def __init__(self) -> None:
        self.authenticated: CompatibilityPrincipal | None = PRINCIPAL
        self.valid = True
        self.authenticate_calls: list[tuple[str, str]] = []
        self.revalidate_calls: list[CompatibilityPrincipal] = []

    async def authenticate_mqtt(
        self, serial: str, access_code: str
    ) -> CompatibilityPrincipal | None:
        self.authenticate_calls.append((serial, access_code))
        return self.authenticated

    async def revalidate(self, principal: CompatibilityPrincipal) -> bool:
        self.revalidate_calls.append(principal)
        return self.valid


class Ingress:
    def __init__(self) -> None:
        self.commands: list[DecodedCommand] = []

    async def execute(
        self, _principal: CompatibilityPrincipal, command: DecodedCommand
    ) -> ControlResult:
        self.commands.append(command)
        return ControlResult(
            operation=ControlOperation.PAUSE,
            status=ControlStatus.DENIED,
            code="test_denial",
        )


class Reports:
    def __init__(self, payload: object = b'{"print":{"state":"unknown"}}') -> None:
        self.payload = payload
        self.calls: list[CompatibilityPrincipal] = []

    async def report(self, principal: CompatibilityPrincipal) -> bytes:
        self.calls.append(principal)
        if isinstance(self.payload, Exception):
            raise self.payload
        return cast(bytes, self.payload)


class Writer:
    def __init__(self, *, drain_error: Exception | None = None) -> None:
        self.output = bytearray()
        self.closed = False
        self.waited = False
        self.drain_error = drain_error

    def write(self, payload: bytes) -> None:
        self.output.extend(payload)

    async def drain(self) -> None:
        if self.drain_error is not None:
            raise self.drain_error

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited = True


def reader(raw: bytes, *, eof: bool = True) -> asyncio.StreamReader:
    value = asyncio.StreamReader()
    value.feed_data(raw)
    if eof:
        value.feed_eof()
    return value


def make_server(
    tmp_path: Path,
    *,
    auth: Authenticator | None = None,
    ingress: Ingress | None = None,
    reports: Reports | None = None,
    **options: object,
) -> tuple[MqttTlsServer, Authenticator, Ingress, Reports]:
    auth = auth or Authenticator()
    ingress = ingress or Ingress()
    reports = reports or Reports()
    server = MqttTlsServer(
        config(tmp_path, **options),  # type: ignore[arg-type]
        auth,  # type: ignore[arg-type]
        ingress,  # type: ignore[arg-type]
        reports,
    )
    return server, auth, ingress, reports


async def run_session(server: MqttTlsServer, raw: bytes) -> Writer:
    target = Writer()
    await server._accept(reader(raw), cast(asyncio.StreamWriter, target))
    return target


async def test_complete_session_authenticates_reports_controls_and_orders_ack(
    tmp_path: Path,
) -> None:
    server, auth, ingress, reports = make_server(tmp_path)
    valid = b'{"print":{"command":"pause","sequence_id":"7"}}'
    invalid = b'{"print":{"command":"project_file","sequence_id":"8"}}'

    target = await run_session(
        server,
        connect()
        + subscribe()
        + frame(0xC0)
        + publish(valid, packet_id=3)
        + publish(invalid, packet_id=4)
        + frame(0xE0),
    )

    report = encode_report(SERIAL, cast(bytes, reports.payload))
    assert bytes(target.output) == (
        encode_connack()
        + encode_suback(2)
        + report
        + encode_pingresp()
        + report
        + encode_puback(3)
        + encode_puback(4)
    )
    assert auth.authenticate_calls == [(SERIAL, ACCESS_CODE)]
    assert len(auth.revalidate_calls) == 5
    assert [command.sequence_id for command in ingress.commands] == ["7"]
    assert reports.calls == [PRINCIPAL, PRINCIPAL]
    assert target.closed and target.waited
    assert not server._sessions and not server._printer_sessions


@pytest.mark.parametrize(
    "raw",
    [
        frame(0xC0),
        connect() + connect(),
        connect() + frame(0xC0),
        connect() + subscribe(serial=OTHER_SERIAL),
        connect() + subscribe() + subscribe(),
        connect() + subscribe() + frame(0x40, b"\x00\x01"),
        connect() + subscribe() + publish(b"{}", serial=OTHER_SERIAL),
        b"\xff\x00",
    ],
)
async def test_wrong_order_topic_or_packet_closes_without_actuation(
    tmp_path: Path, raw: bytes
) -> None:
    server, _auth, ingress, _reports = make_server(tmp_path)

    target = await run_session(server, raw)

    assert target.closed
    assert ingress.commands == []
    assert not server._printer_sessions


async def test_authentication_denial_and_rotation_close_without_identity_response(
    tmp_path: Path,
) -> None:
    denied = Authenticator()
    denied.authenticated = None
    server, _auth, _ingress, _reports = make_server(tmp_path, auth=denied)
    target = await run_session(server, connect())
    assert target.output == b""

    rotated = Authenticator()
    rotated.valid = False
    server, _auth, _ingress, _reports = make_server(tmp_path, auth=rotated)
    target = await run_session(server, connect() + subscribe())
    assert bytes(target.output) == encode_connack()


async def test_command_limit_closes_after_exact_bound(tmp_path: Path) -> None:
    server, _auth, ingress, _reports = make_server(tmp_path, max_commands=1)
    command = b'{"print":{"command":"pause","sequence_id":"1"}}'

    target = await run_session(
        server,
        connect() + subscribe() + publish(command, packet_id=3) + publish(command, packet_id=4),
    )

    assert len(ingress.commands) == 1
    assert bytes(target.output).endswith(encode_puback(3))
    assert not bytes(target.output).endswith(encode_puback(4))


@pytest.mark.parametrize(
    "payload",
    [bytearray(b"{}"), b"x" * (MAX_REPORT_BYTES + 1), RuntimeError("report failed")],
)
async def test_invalid_or_failed_report_closes_session(tmp_path: Path, payload: object) -> None:
    server, _auth, _ingress, _reports = make_server(tmp_path, reports=Reports(payload))

    target = await run_session(server, connect() + subscribe())

    assert bytes(target.output) == encode_connack() + encode_suback(2)
    assert target.closed


async def test_read_timeout_and_write_failure_are_bounded(tmp_path: Path) -> None:
    server, _auth, _ingress, _reports = make_server(tmp_path, idle=0.01)
    target = Writer()
    await server._accept(reader(b"", eof=False), cast(asyncio.StreamWriter, target))
    assert target.closed

    target = Writer(drain_error=ConnectionError("closed"))
    await server._accept(reader(connect()), cast(asyncio.StreamWriter, target))
    assert target.closed


async def test_global_and_per_printer_capacity_do_not_evict_admitted_sessions(
    tmp_path: Path,
) -> None:
    server, _auth, _ingress, _reports = make_server(
        tmp_path, max_sessions=2, max_sessions_per_printer=1
    )
    first_reader = reader(connect(), eof=False)
    first_writer = Writer()
    first = asyncio.create_task(
        server._accept(first_reader, cast(asyncio.StreamWriter, first_writer))
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert bytes(first_writer.output) == encode_connack()

    second = await run_session(server, connect())
    assert second.output == b""
    assert not first.done()

    first_reader.feed_eof()
    await first

    global_server, _auth, _ingress, _reports = make_server(
        tmp_path, max_sessions=1, max_sessions_per_printer=1
    )
    held_reader = reader(b"", eof=False)
    held_writer = Writer()
    held = asyncio.create_task(
        global_server._accept(held_reader, cast(asyncio.StreamWriter, held_writer))
    )
    await asyncio.sleep(0)
    rejected = await run_session(global_server, connect())
    assert rejected.closed
    assert not held.done()
    held_reader.feed_eof()
    await held


async def test_two_admitted_printer_sessions_release_independently(tmp_path: Path) -> None:
    server, _auth, _ingress, _reports = make_server(tmp_path)
    first_reader = reader(connect(), eof=False)
    second_reader = reader(connect(), eof=False)
    first = asyncio.create_task(server._accept(first_reader, cast(asyncio.StreamWriter, Writer())))
    second = asyncio.create_task(
        server._accept(second_reader, cast(asyncio.StreamWriter, Writer()))
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert server._printer_sessions == {PRINCIPAL.printer_uuid: 2}
    first_reader.feed_eof()
    await first
    assert server._printer_sessions == {PRINCIPAL.printer_uuid: 1}
    second_reader.feed_eof()
    await second
    assert server._printer_sessions == {}


async def test_accept_handles_missing_task_and_propagates_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, _auth, _ingress, _reports = make_server(tmp_path)
    target = Writer()
    with monkeypatch.context() as context:
        context.setattr(asyncio, "current_task", lambda: None)
        await server._accept(reader(b""), cast(asyncio.StreamWriter, target))
    assert target.closed

    held_reader = reader(b"", eof=False)
    held_writer = Writer()
    task = asyncio.create_task(server._accept(held_reader, cast(asyncio.StreamWriter, held_writer)))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert held_writer.closed


async def test_close_cancels_sessions_that_exceed_shutdown_bound(tmp_path: Path) -> None:
    server, _auth, _ingress, _reports = make_server(tmp_path, shutdown=0.01)
    release = asyncio.Event()

    async def stubborn() -> None:
        await release.wait()

    task = asyncio.create_task(stubborn())
    await asyncio.sleep(0)
    target = Writer()
    server._sessions.add(task)
    server._writers.add(cast(asyncio.StreamWriter, target))

    await server.close()

    assert target.closed
    assert task.cancelled()


async def test_close_before_start_and_preclosed_session_are_safe(tmp_path: Path) -> None:
    server, _auth, _ingress, _reports = make_server(tmp_path)
    await server.close()
    assert server.sockets == ()
    await server._session(reader(b""), cast(asyncio.StreamWriter, Writer()))


def generate_certificate(tmp_path: Path) -> None:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("OpenSSL CLI is required for the loopback TLS contract test")
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
            str(tmp_path / "key.pem"),
            "-out",
            str(tmp_path / "cert.pem"),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def test_real_loopback_listener_is_tls13_only_and_shuts_down(tmp_path: Path) -> None:
    generate_certificate(tmp_path)
    server, _auth, ingress, reports = make_server(tmp_path, port=0)
    await server.start()
    assert server.sockets
    port = server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    client_reader, client_writer = await asyncio.open_connection(
        "127.0.0.1", port, ssl=context, server_hostname="localhost"
    )
    client_writer.write(connect() + subscribe() + frame(0xC0) + frame(0xE0))
    await client_writer.drain()
    expected = (
        encode_connack()
        + encode_suback(2)
        + encode_report(SERIAL, cast(bytes, reports.payload))
        + encode_pingresp()
    )
    assert await client_reader.readexactly(len(expected)) == expected
    assert client_writer.get_extra_info("ssl_object").version() == "TLSv1.3"
    assert ingress.commands == []
    client_writer.close()
    await client_writer.wait_closed()
    await server.close()
    assert server.sockets == ()


async def test_start_guards_and_close_are_idempotent(tmp_path: Path) -> None:
    generate_certificate(tmp_path)
    server, _auth, _ingress, _reports = make_server(tmp_path, port=0)
    await server.start()
    with pytest.raises(RuntimeError, match="already started"):
        await server.start()
    await server.close()
    await server.close()
    with pytest.raises(RuntimeError, match="closing"):
        await server.start()

    disabled = GroveBridgeConfig()
    with pytest.raises(ValueError, match="enabled"):
        MqttTlsServer(disabled, _auth, _ingress, _reports)  # type: ignore[arg-type]

    incomplete = config(tmp_path).model_copy(update={"tls_certificate_file": None})
    with pytest.raises(ValueError, match="TLS"):
        MqttTlsServer(incomplete, _auth, _ingress, _reports)  # type: ignore[arg-type]


def test_tls_context_rejects_missing_material(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        _tls_context(tmp_path / "missing.crt", tmp_path / "missing.key")
