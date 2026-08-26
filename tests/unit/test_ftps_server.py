from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import pytest

from klove.config import GroveBridgeConfig
from klove.domain.artifacts import ArtifactLimits
from klove.ftps import server as server_module
from klove.ftps.protocol import FtpsAction, FtpsEvent, FtpsProtocolError
from klove.ftps.server import (
    FtpsBindDiagnosticCode,
    FtpsBindDiagnosticResult,
    FtpsBindSetDiagnostic,
    FtpsTlsServer,
    _blocking,
    _bound_port,
    _DataConnectionSlot,
    _ipv4,
    _PassiveEndpoint,
    _PassiveLease,
    _pasv_reply,
    _tls_context,
    _wait_closed,
)
from klove.ftps.staging import FtpsStagingStore
from klove.orchestration.admission import PrinterAdmissionGates
from klove.security.compatibility import CompatibilityAuthenticator, CompatibilityPrincipal
from klove.security.compatibility_sessions import CompatibilitySessionRegistry
from tests.support.ftps_conformance_client import FtpsConformanceClient

from ..onboarding_helpers import printer, safety_profile

ACCESS_CODE = "X" * 20
PRINTER_UUID = "01234567-89ab-4def-8123-456789abcdef"
PRINCIPAL = CompatibilityPrincipal(
    printer_uuid=PRINTER_UUID,
    proxy_serial="KLOVE-01234567-89AB-CDEF-0123-456789ABCDEF",
    record_revision=1,
    control_enabled=True,
    dispatch_enabled=True,
)
ADMISSIONS = PrinterAdmissionGates()


class Authenticator:
    def __init__(self) -> None:
        self.principal: CompatibilityPrincipal | None = PRINCIPAL
        self.valid = True
        self.authenticate_calls: list[str] = []
        self.revalidate_calls: list[CompatibilityPrincipal] = []
        self.admission_leases: list[object] = []

    async def authenticate_ftps(self, access_code: str) -> CompatibilityPrincipal | None:
        self.authenticate_calls.append(access_code)
        return self.principal if access_code == ACCESS_CODE else None

    async def revalidate(
        self, principal: CompatibilityPrincipal, admission_lease: object | None = None
    ) -> bool:
        self.revalidate_calls.append(principal)
        if admission_lease is not None:
            self.admission_leases.append(admission_lease)
        return self.valid


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


def config(tmp_path: Path) -> GroveBridgeConfig:
    return GroveBridgeConfig.model_construct(
        enabled=True,
        listen_host="127.0.0.1",
        mqtt_port=4883,
        ftps_control_port=0,
        ftps_passive_port_min=40400,
        ftps_passive_port_max=40400,
        ftps_advertised_ipv4="127.0.0.1",
        tls_certificate_file=tmp_path / "cert.pem",
        tls_private_key_file=tmp_path / "key.pem",
        staging_directory=tmp_path / "staging",
        max_sessions=4,
        max_sessions_per_printer=2,
        max_commands_per_session=8,
        session_idle_seconds=2.0,
        transfer_timeout_seconds=2.0,
        shutdown_timeout_seconds=1.0,
        max_concurrent_transfers=1,
        ingress_capacity=4,
        staging_ttl_seconds=60,
    )


def readiness_config(tmp_path: Path) -> GroveBridgeConfig:
    return config(tmp_path).model_copy(
        update={
            "listen_host": "127.0.0.1",
            "ftps_advertised_ipv4": "127.0.0.1",
            "ftps_control_port": 49000,
            "ftps_passive_port_min": 49001,
            "ftps_passive_port_max": 49003,
        }
    )


class ProbeSocket:
    def __init__(self, port: int, close_error: BaseException | None = None) -> None:
        self.port = port
        self.closed = False
        self.close_attempts = 0
        self.close_error = close_error

    def close(self) -> None:
        self.closed = True
        self.close_attempts += 1
        if self.close_error is not None:
            raise self.close_error


def test_bind_diagnostic_checks_every_configured_port_and_releases_on_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = readiness_config(tmp_path)
    diagnostic = FtpsBindSetDiagnostic(settings)
    probes: list[ProbeSocket] = []

    def open_probe(_host: str, port: int) -> ProbeSocket:
        probe = ProbeSocket(port)
        probes.append(probe)
        return probe

    monkeypatch.setattr(server_module, "_open_probe_socket", open_probe)

    assert diagnostic.check() == FtpsBindDiagnosticResult(True)
    assert [probe.port for probe in probes] == [49000, 49001, 49002, 49003]
    assert all(probe.closed for probe in probes)


def test_bind_diagnostic_returns_fixed_control_conflict_without_leaking_passive_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = readiness_config(tmp_path)
    calls: list[int] = []

    def open_probe(_host: str, port: int) -> ProbeSocket:
        calls.append(port)
        raise OSError("unavailable")

    monkeypatch.setattr(server_module, "_open_probe_socket", open_probe)
    diagnostic = FtpsBindSetDiagnostic(settings)
    result = diagnostic.check()

    assert result == FtpsBindDiagnosticResult(
        False, FtpsBindDiagnosticCode.CONTROL_PORT_UNAVAILABLE
    )
    assert calls == [settings.ftps_control_port]


def test_bind_diagnostic_releases_control_port_after_passive_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = readiness_config(tmp_path)
    control = ProbeSocket(settings.ftps_control_port)

    def open_probe(_host: str, port: int) -> ProbeSocket:
        if port == settings.ftps_control_port:
            return control
        raise OSError("unavailable")

    monkeypatch.setattr(server_module, "_open_probe_socket", open_probe)
    diagnostic = FtpsBindSetDiagnostic(settings)
    result = diagnostic.check()

    assert result == FtpsBindDiagnosticResult(
        False, FtpsBindDiagnosticCode.PASSIVE_PORT_UNAVAILABLE
    )
    assert control.closed is True


def test_bind_diagnostic_releases_control_port_on_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = readiness_config(tmp_path)
    control = ProbeSocket(settings.ftps_control_port)

    def open_probe(_host: str, port: int) -> ProbeSocket:
        if port == settings.ftps_control_port:
            return control
        raise KeyboardInterrupt

    monkeypatch.setattr(server_module, "_open_probe_socket", open_probe)

    with pytest.raises(KeyboardInterrupt):
        FtpsBindSetDiagnostic(settings).check()

    assert control.closed is True


def test_bind_diagnostic_requires_enabled_exact_bridge_config(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="enabled Grove bridge"):
        FtpsBindSetDiagnostic(GroveBridgeConfig())
    with pytest.raises(ValueError, match="enabled Grove bridge"):
        FtpsBindSetDiagnostic(cast(GroveBridgeConfig, object()))


def test_bind_diagnostic_chains_release_interruption_from_pending_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = readiness_config(tmp_path)
    release_interruption = KeyboardInterrupt("release interrupted")
    pending_interruption = KeyboardInterrupt("bind interrupted")
    control = ProbeSocket(settings.ftps_control_port, release_interruption)

    def open_probe(_host: str, port: int) -> ProbeSocket:
        if port == settings.ftps_control_port:
            return control
        raise pending_interruption

    monkeypatch.setattr(server_module, "_open_probe_socket", open_probe)

    with pytest.raises(KeyboardInterrupt, match="bind interrupted") as caught:
        FtpsBindSetDiagnostic(settings).check()

    assert caught.value.__cause__ is release_interruption
    assert control.close_attempts == 1


def test_bind_diagnostic_attempts_every_close_and_returns_fixed_release_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = readiness_config(tmp_path)
    original = [
        ProbeSocket(settings.ftps_control_port, OSError("close failed")),
        ProbeSocket(settings.ftps_passive_port_min),
        ProbeSocket(settings.ftps_passive_port_min + 1, OSError("close failed")),
        ProbeSocket(settings.ftps_passive_port_max),
    ]
    probes = list(original)

    def open_probe(_host: str, _port: int) -> ProbeSocket:
        return probes.pop(0)

    monkeypatch.setattr(server_module, "_open_probe_socket", open_probe)

    assert FtpsBindSetDiagnostic(settings).check() == FtpsBindDiagnosticResult(
        False, FtpsBindDiagnosticCode.PROBE_RELEASE_FAILED
    )
    assert all(probe.close_attempts == 1 for probe in original)


def test_bind_diagnostic_attempts_every_close_before_reraising_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = readiness_config(tmp_path)
    original = [
        ProbeSocket(settings.ftps_control_port, KeyboardInterrupt()),
        ProbeSocket(settings.ftps_passive_port_min),
        ProbeSocket(settings.ftps_passive_port_min + 1),
        ProbeSocket(settings.ftps_passive_port_max),
    ]
    probes = list(original)

    def open_probe(_host: str, _port: int) -> ProbeSocket:
        return probes.pop(0)

    monkeypatch.setattr(server_module, "_open_probe_socket", open_probe)

    with pytest.raises(KeyboardInterrupt):
        FtpsBindSetDiagnostic(settings).check()

    assert all(probe.close_attempts == 1 for probe in original)


def test_bind_diagnostic_preserves_first_release_interruption_after_all_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = readiness_config(tmp_path)
    original = [
        ProbeSocket(settings.ftps_control_port, KeyboardInterrupt("later interruption")),
        ProbeSocket(settings.ftps_passive_port_min, KeyboardInterrupt("first interruption")),
        ProbeSocket(settings.ftps_passive_port_min + 1),
        ProbeSocket(settings.ftps_passive_port_max),
    ]
    probes = list(original)

    def open_probe(_host: str, _port: int) -> ProbeSocket:
        return probes.pop(0)

    monkeypatch.setattr(server_module, "_open_probe_socket", open_probe)

    with pytest.raises(KeyboardInterrupt, match="first interruption"):
        FtpsBindSetDiagnostic(settings).check()

    assert all(probe.close_attempts == 1 for probe in original)


@pytest.mark.parametrize(
    ("available", "code"),
    [
        (True, FtpsBindDiagnosticCode.CONTROL_PORT_UNAVAILABLE),
        (False, None),
        (False, "ftps_control_port_unavailable"),
        (1, None),
    ],
)
def test_bind_diagnostic_result_rejects_nonexact_state(available: object, code: object) -> None:
    with pytest.raises(ValueError):
        FtpsBindDiagnosticResult(cast(bool, available), cast(FtpsBindDiagnosticCode, code))


def test_bind_diagnostic_accepts_wildcard_rfc1918_network_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = readiness_config(tmp_path).model_copy(
        update={
            "listen_host": "0.0.0.0",  # noqa: S104 -- accepted container bridge bind.
            "ftps_advertised_ipv4": "192.168.1.20",
        }
    )
    diagnostic = FtpsBindSetDiagnostic(settings)
    probes: list[ProbeSocket] = []
    calls: list[tuple[str, int]] = []

    def open_probe(host: str, port: int) -> ProbeSocket:
        calls.append((host, port))
        probe = ProbeSocket(port)
        probes.append(probe)
        return probe

    monkeypatch.setattr(server_module, "_open_probe_socket", open_probe)

    assert diagnostic.check() == FtpsBindDiagnosticResult(True)
    assert {host for host, _port in calls} == {"0.0.0.0"}  # noqa: S104 -- asserted bridge bind.
    assert all(probe.closed for probe in probes)


def test_bind_diagnostic_rejects_nonprivate_advertised_topology_without_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = readiness_config(tmp_path).model_copy(update={"ftps_advertised_ipv4": "8.8.8.8"})
    diagnostic = FtpsBindSetDiagnostic(settings)

    monkeypatch.setattr(
        server_module,
        "_open_probe_socket",
        lambda _host, _port: pytest.fail("unsupported topology must not bind"),
    )

    assert diagnostic.check() == FtpsBindDiagnosticResult(
        False, FtpsBindDiagnosticCode.UNSUPPORTED_PRIVATE_TOPOLOGY
    )


def test_bind_diagnostic_rejects_broadcast_or_mismatched_topology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        server_module,
        "_open_probe_socket",
        lambda _host, _port: pytest.fail("unsupported topology must not bind"),
    )
    for update in (
        {"ftps_advertised_ipv4": "255.255.255.255"},
        {"listen_host": "192.168.1.2", "ftps_advertised_ipv4": "192.168.1.3"},
    ):
        settings = readiness_config(tmp_path).model_copy(update=update)
        assert FtpsBindSetDiagnostic(settings).check() == FtpsBindDiagnosticResult(
            False, FtpsBindDiagnosticCode.UNSUPPORTED_PRIVATE_TOPOLOGY
        )


@pytest.mark.parametrize(
    "update",
    [
        {"ftps_advertised_ipv4": None},
        {"listen_host": "not-an-ip-address"},
        {"ftps_advertised_ipv4": "not-an-ip-address"},
        {"listen_host": 1},
        {"ftps_advertised_ipv4": 1},
    ],
)
def test_bind_diagnostic_rejects_defensively_invalid_topology_without_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, update: dict[str, object]
) -> None:
    settings = readiness_config(tmp_path).model_copy(update=update)
    monkeypatch.setattr(
        server_module,
        "_open_probe_socket",
        lambda _host, _port: pytest.fail("unsupported topology must not bind"),
    )

    assert FtpsBindSetDiagnostic(settings).check() == FtpsBindDiagnosticResult(
        False, FtpsBindDiagnosticCode.UNSUPPORTED_PRIVATE_TOPOLOGY
    )


def real_socket_or_skip() -> socket.socket:
    try:
        return socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except PermissionError:
        pytest.skip("raw loopback sockets are unavailable in this sandbox")


def real_bind_or_skip(port: int) -> socket.socket:
    probe = real_socket_or_skip()
    try:
        probe.bind(("127.0.0.1", port))
    except BaseException:
        probe.close()
        raise
    return probe


def real_readiness_config_or_skip(tmp_path: Path) -> GroveBridgeConfig:
    control = real_bind_or_skip(0)
    passive = real_bind_or_skip(0)
    try:
        control_port = cast(int, control.getsockname()[1])
        passive_port = cast(int, passive.getsockname()[1])
    finally:
        control.close()
        passive.close()
    return readiness_config(tmp_path).model_copy(
        update={
            "ftps_control_port": control_port,
            "ftps_passive_port_min": passive_port,
            "ftps_passive_port_max": passive_port,
        }
    )


def real_ownership_config_or_skip(tmp_path: Path, *, passive_count: int = 2) -> GroveBridgeConfig:
    """Find one momentarily free exact control port and contiguous passive set."""
    for _attempt in range(100):
        seed = real_bind_or_skip(0)
        control = real_bind_or_skip(0)
        base = cast(int, seed.getsockname()[1])
        control_port = cast(int, control.getsockname()[1])
        seed.close()
        control.close()
        if base + passive_count - 1 > 65535 or control_port in range(base, base + passive_count):
            continue
        held: list[socket.socket] = []
        try:
            for port in range(base, base + passive_count):
                held.append(real_bind_or_skip(port))
        except OSError:
            for probe in held:
                probe.close()
            continue
        for probe in held:
            probe.close()
        return readiness_config(tmp_path).model_copy(
            update={
                "ftps_control_port": control_port,
                "ftps_passive_port_min": base,
                "ftps_passive_port_max": base + passive_count - 1,
            }
        )
    pytest.skip("no contiguous loopback passive range was available")


def test_bind_diagnostic_real_loopback_success_releases_every_port(tmp_path: Path) -> None:
    settings = real_readiness_config_or_skip(tmp_path)

    assert FtpsBindSetDiagnostic(settings).check() == FtpsBindDiagnosticResult(True)
    for port in (settings.ftps_control_port, settings.ftps_passive_port_min):
        probe = real_bind_or_skip(port)
        probe.close()


def test_bind_diagnostic_real_loopback_control_conflict(tmp_path: Path) -> None:
    settings = real_readiness_config_or_skip(tmp_path)
    blocker = real_bind_or_skip(settings.ftps_control_port)
    try:
        result = FtpsBindSetDiagnostic(settings).check()
    finally:
        blocker.close()

    assert result == FtpsBindDiagnosticResult(
        False, FtpsBindDiagnosticCode.CONTROL_PORT_UNAVAILABLE
    )


def test_bind_diagnostic_real_loopback_passive_conflict_releases_control(tmp_path: Path) -> None:
    settings = real_readiness_config_or_skip(tmp_path)
    blocker = real_bind_or_skip(settings.ftps_passive_port_min)
    try:
        result = FtpsBindSetDiagnostic(settings).check()
    finally:
        blocker.close()

    assert result == FtpsBindDiagnosticResult(
        False, FtpsBindDiagnosticCode.PASSIVE_PORT_UNAVAILABLE
    )
    released_control = real_bind_or_skip(settings.ftps_control_port)
    released_control.close()


async def test_runtime_ownership_real_loopback_is_exact_exclusive_and_released(
    tmp_path: Path,
) -> None:
    generate_certificate(tmp_path)
    settings = real_ownership_config_or_skip(tmp_path)
    server = FtpsTlsServer(
        settings,
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )

    await server.start()
    ports = (
        settings.ftps_control_port,
        *range(settings.ftps_passive_port_min, settings.ftps_passive_port_max + 1),
    )
    assert cast(Any, server.sockets[0]).getsockname() == (
        settings.listen_host,
        settings.ftps_control_port,
    )
    for port in ports:
        with pytest.raises(OSError):
            real_bind_or_skip(port)

    await server.close()
    for port in ports:
        released = real_bind_or_skip(port)
        released.close()


async def test_runtime_ownership_serializes_concurrent_start_and_close(tmp_path: Path) -> None:
    generate_certificate(tmp_path)
    settings = real_ownership_config_or_skip(tmp_path)
    server = FtpsTlsServer(
        settings,
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )

    starts = await asyncio.gather(server.start(), server.start(), return_exceptions=True)
    assert sum(result is None for result in starts) == 1
    assert sum(isinstance(result, RuntimeError) for result in starts) == 1

    await asyncio.gather(server.close(), server.close())
    for port in (
        settings.ftps_control_port,
        *range(settings.ftps_passive_port_min, settings.ftps_passive_port_max + 1),
    ):
        released = real_bind_or_skip(port)
        released.close()


async def test_runtime_ownership_real_loopback_each_conflict_releases_all_prior_ports(
    tmp_path: Path,
) -> None:
    settings = real_ownership_config_or_skip(tmp_path, passive_count=3)
    ports = (
        settings.ftps_control_port,
        *range(settings.ftps_passive_port_min, settings.ftps_passive_port_max + 1),
    )
    for conflict in ports:
        blocker = real_bind_or_skip(conflict)
        server = FtpsTlsServer(
            settings,
            cast(CompatibilityAuthenticator, Authenticator()),
            cast(FtpsStagingStore, Staging()),
            ADMISSIONS,
        )
        try:
            with pytest.raises(OSError):
                await server.start()
            for port in ports:
                if port == conflict:
                    continue
                released = real_bind_or_skip(port)
                released.close()
        finally:
            blocker.close()
            await server.close()


async def test_real_conformance_cleanup_and_upload_create_only_private_stage(
    tmp_path: Path,
) -> None:
    generate_certificate(tmp_path)
    settings = real_readiness_config_or_skip(tmp_path)
    settings.staging_directory.mkdir(mode=0o700)
    staging = FtpsStagingStore(
        settings.staging_directory,
        limits=ArtifactLimits(),
        capacity=settings.ingress_capacity,
        clock_ms=lambda: 1_000,
    )
    staging.initialize()
    auth = Authenticator()
    server = FtpsTlsServer(
        settings,
        cast(CompatibilityAuthenticator, auth),
        staging,
        ADMISSIONS,
        clock_ms=lambda: 1_000,
    )
    await server.start()
    port = cast(Any, server.sockets[0]).getsockname()[1]
    client = FtpsConformanceClient("127.0.0.1", port, ACCESS_CODE, timeout=2.0)

    cleanup = await asyncio.to_thread(client.cleanup)
    payload = b"bounded harmless archive bytes"
    upload = await asyncio.to_thread(client.upload, [payload[:8], payload[8:]])
    await server.close()

    assert cleanup.reply_codes == (220, 331, 230, 200, 200, 250, 221)
    assert upload.reply_codes == (220, 331, 230, 200, 200, 227, 150, 226, 221)
    assert upload.byte_count == len(payload)
    assert upload.sha256 == f"sha256:{hashlib.sha256(payload).hexdigest()}"
    receipt_path, source_path = sorted(settings.staging_directory.iterdir())
    if receipt_path.suffix != ".receipt":
        receipt_path, source_path = source_path, receipt_path
    receipt = json.loads(receipt_path.read_bytes())
    assert receipt["printer_uuid"] == PRINTER_UUID
    assert receipt["client_path"] == "/observation.3mf"
    assert receipt["created_at_unix_ms"] == 1_000
    assert receipt["expires_at_unix_ms"] == 61_000
    assert receipt["archive_size_bytes"] == len(payload)
    assert source_path.read_bytes() == payload
    assert auth.authenticate_calls == [ACCESS_CODE, ACCESS_CODE]
    assert auth.revalidate_calls == [PRINCIPAL] * len(auth.revalidate_calls)
    assert len(auth.revalidate_calls) >= 11
    assert len(auth.admission_leases) == 1


async def test_authentication_and_revalidation_denials_never_stage(tmp_path: Path) -> None:
    settings = real_readiness_config_or_skip(tmp_path)
    settings.staging_directory.mkdir(mode=0o700)
    staging = FtpsStagingStore(
        settings.staging_directory, limits=ArtifactLimits(), capacity=settings.ingress_capacity
    )
    auth = Authenticator()
    auth.principal = None
    server = FtpsTlsServer(settings, cast(CompatibilityAuthenticator, auth), staging, ADMISSIONS)
    server._ready = True
    reader = asyncio.StreamReader()
    reader.feed_data(b"USER bblp\r\n" + f"PASS {ACCESS_CODE}\r\n".encode())
    reader.feed_eof()
    writer = Writer()
    await server._accept(reader, cast(asyncio.StreamWriter, writer))
    assert bytes(writer.output).endswith(b"530 Authentication failed\r\n")
    assert list(settings.staging_directory.iterdir()) == []

    auth.principal = PRINCIPAL
    auth.valid = False
    target = Writer(peer=("127.0.0.1", 12345))
    reader = asyncio.StreamReader()
    reader.feed_data(b"USER bblp\r\n" + f"PASS {ACCESS_CODE}\r\n".encode() + b"PBSZ 0\r\n")
    reader.feed_eof()
    await server._accept(reader, cast(asyncio.StreamWriter, target))
    assert bytes(target.output).endswith(b"230 Authenticated\r\n")
    assert list(settings.staging_directory.iterdir()) == []


class Writer:
    def __init__(
        self, *, peer: object = ("127.0.0.1", 12345), ssl_object: object | None = None
    ) -> None:
        self.output = bytearray()
        self.peer = peer
        self.ssl_object = ssl_object
        self.closed = False

    def get_extra_info(self, name: str) -> object:
        if name == "peername":
            return self.peer
        return self.ssl_object if name == "ssl_object" else None

    def write(self, payload: bytes) -> None:
        self.output.extend(payload)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class PassiveServer:
    def __init__(self, *, port: int = 50000, sockets: list[object] | None = None) -> None:
        self.sockets = sockets if sockets is not None else [BoundSocket(port)]
        self.closed = False
        self.waited = False
        self.start_calls = 0
        self.close_clients_calls = 0

    async def start_serving(self) -> None:
        self.start_calls += 1

    def close(self) -> None:
        self.closed = True

    def close_clients(self) -> None:
        self.close_clients_calls += 1

    async def wait_closed(self) -> None:
        self.waited = True


class HangingPassiveServer(PassiveServer):
    def __init__(self) -> None:
        super().__init__()
        self.hanging = True
        self.wait_calls = 0

    async def wait_closed(self) -> None:
        self.waited = True
        self.wait_calls += 1
        if self.hanging:
            await asyncio.Event().wait()


class UncertainCloseServer(PassiveServer):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            raise OSError("close outcome is uncertain")
        super().close()


class BoundSocket:
    def __init__(self, port: int) -> None:
        self.port = port

    def getsockname(self) -> tuple[str, int]:
        return ("127.0.0.1", self.port)


class InvalidSocket:
    def getsockname(self) -> tuple[object, ...]:
        return ("127.0.0.1", "not-a-port")


class Stage:
    def __init__(self, *, write_error: BaseException | None = None) -> None:
        self.chunks: list[bytes] = []
        self.committed = False
        self.aborted = False
        self.write_error = write_error

    def write(self, chunk: bytes) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.chunks.append(chunk)

    def commit(self) -> None:
        self.committed = True

    def abort(self) -> None:
        self.aborted = True


class Staging:
    def __init__(self, stage: Stage | None = None) -> None:
        self.reservations: list[dict[str, object]] = []
        self.stage = stage or Stage()

    def create_reservation(self, **kwargs: object) -> object:
        self.reservations.append(kwargs)
        return object()

    def begin(self, _reservation: object) -> Stage:
        return self.stage


class ErrorReader:
    async def read(self, _size: int) -> bytes:
        raise OSError("read failed")


def test_helpers_and_constructor_fail_closed(tmp_path: Path) -> None:
    auth = cast(CompatibilityAuthenticator, Authenticator())
    staging = cast(FtpsStagingStore, object())
    with pytest.raises(ValueError, match="enabled"):
        FtpsTlsServer(GroveBridgeConfig(), auth, staging, ADMISSIONS)
    incomplete = config(tmp_path).model_copy(update={"tls_certificate_file": None})
    with pytest.raises(ValueError, match="TLS"):
        FtpsTlsServer(incomplete, auth, staging, ADMISSIONS)
    missing_advertised = config(tmp_path).model_copy(update={"ftps_advertised_ipv4": None})
    with pytest.raises(ValueError, match="advertised"):
        FtpsTlsServer(missing_advertised, auth, staging, ADMISSIONS)
    assert _pasv_reply("127.0.0.1", 50000) == "Entering Passive Mode (127,0,0,1,195,80)"
    with pytest.raises(OSError):
        _tls_context(tmp_path / "missing.crt", tmp_path / "missing.key")


@pytest.mark.parametrize(
    "update",
    [
        {"ftps_advertised_ipv4": "8.8.8.8"},
        {"ftps_advertised_ipv4": "255.255.255.255"},
        {"ftps_advertised_ipv4": "169.254.1.1"},
        {"ftps_advertised_ipv4": "100.64.0.1"},
        {"ftps_advertised_ipv4": "192.0.2.1"},
        {"ftps_advertised_ipv4": "240.0.0.1"},
        {"listen_host": "192.168.1.20", "ftps_advertised_ipv4": "192.168.1.21"},
        {
            "listen_host": "0.0.0.0",  # noqa: S104 -- forged topology, never bound.
            "ftps_advertised_ipv4": "127.0.0.1",
        },
    ],
)
def test_constructor_rejects_forged_unsupported_private_topology(
    tmp_path: Path, update: dict[str, object]
) -> None:
    config_values = config(tmp_path).model_dump()
    config_values.update(update)
    forged = GroveBridgeConfig.model_construct(**config_values)

    with pytest.raises(ValueError, match="supported private FTPS topology"):
        FtpsTlsServer(
            forged,
            cast(CompatibilityAuthenticator, Authenticator()),
            cast(FtpsStagingStore, Staging()),
            ADMISSIONS,
        )


def test_constructor_rejects_forged_model_copy_unsupported_topology(tmp_path: Path) -> None:
    forged = config(tmp_path).model_copy(update={"ftps_advertised_ipv4": "8.8.8.8"})

    with pytest.raises(ValueError, match="supported private FTPS topology"):
        FtpsTlsServer(
            forged,
            cast(CompatibilityAuthenticator, Authenticator()),
            cast(FtpsStagingStore, Staging()),
            ADMISSIONS,
        )


@pytest.mark.parametrize(
    "update",
    [
        {},
        {"listen_host": "10.20.30.40", "ftps_advertised_ipv4": "10.20.30.40"},
        {
            "listen_host": "0.0.0.0",  # noqa: S104 -- accepted topology, never bound.
            "ftps_advertised_ipv4": "192.168.1.20",
        },
    ],
)
def test_constructor_accepts_supported_private_topology(
    tmp_path: Path, update: dict[str, object]
) -> None:
    configured = config(tmp_path).model_copy(update=update)

    server = FtpsTlsServer(
        configured,
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )

    assert server.sockets == ()


async def test_plaintext_never_reaches_protocol(tmp_path: Path) -> None:
    generate_certificate(tmp_path)
    settings = real_readiness_config_or_skip(tmp_path)
    settings.staging_directory.mkdir(mode=0o700)
    server = FtpsTlsServer(
        settings,
        cast(CompatibilityAuthenticator, Authenticator()),
        FtpsStagingStore(
            settings.staging_directory,
            limits=ArtifactLimits(),
            capacity=settings.ingress_capacity,
        ),
        ADMISSIONS,
    )
    await server.start()
    port = cast(Any, server.sockets[0]).getsockname()[1]

    plaintext_reader, plaintext_writer = await asyncio.open_connection("127.0.0.1", port)
    plaintext_writer.write(b"USER bblp\r\n")
    await plaintext_writer.drain()
    assert await asyncio.wait_for(plaintext_reader.read(), timeout=2.0) == b""
    plaintext_writer.close()
    await plaintext_writer.wait_closed()
    await server.close()


def test_tls_context_is_tls13_only(tmp_path: Path) -> None:
    generate_certificate(tmp_path)
    context = _tls_context(tmp_path / "cert.pem", tmp_path / "key.pem")
    assert context.minimum_version is ssl.TLSVersion.TLSv1_3
    assert context.maximum_version is ssl.TLSVersion.TLSv1_3


async def test_start_refuses_repeat_and_close_stops_every_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generate_certificate(tmp_path)
    settings = real_readiness_config_or_skip(tmp_path)
    server = FtpsTlsServer(
        settings,
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    started: list[dict[str, object]] = []
    listener = PassiveServer()

    async def start_server(*_args: object, **kwargs: object) -> PassiveServer:
        started.append(kwargs)
        return listener

    monkeypatch.setattr(asyncio, "start_server", start_server)
    await server.start()
    assert all(call["start_serving"] is False for call in started)
    assert started[-1]["limit"] == 513
    assert started[-1]["ssl_handshake_timeout"] == settings.session_idle_seconds
    with pytest.raises(RuntimeError, match="already started"):
        await server.start()

    writer = Writer()
    server._writers.add(cast(asyncio.StreamWriter, writer))
    passive = PassiveServer()
    server._passive_servers.add(cast(asyncio.Server, passive))
    await server.close()
    await server.close()
    assert listener.closed and listener.waited
    assert passive.closed and passive.waited
    assert writer.closed
    with pytest.raises(RuntimeError, match="closing"):
        await server.start()


@pytest.mark.parametrize(
    "update",
    [
        {"ftps_control_port": True},
        {"ftps_passive_port_min": 0},
        {"ftps_passive_port_max": 65536},
        {"ftps_passive_port_min": 49003, "ftps_passive_port_max": 49001},
        {"ftps_passive_port_min": 49001, "ftps_passive_port_max": 49065},
        {"ftps_control_port": 49001},
        {"mqtt_port": 49000},
        {"mqtt_port": 49002},
    ],
)
async def test_start_rejects_forged_socket_set_before_opening_any_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, update: dict[str, object]
) -> None:
    forged = readiness_config(tmp_path).model_copy(update=update)
    server = FtpsTlsServer(
        forged,
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    monkeypatch.setattr(
        server_module,
        "_open_owned_socket",
        lambda _host, _port: pytest.fail("forged socket set must not open a port"),
    )

    with pytest.raises(ValueError, match="supported private FTPS topology"):
        await server.start()


@pytest.mark.parametrize("failure", [OSError("transfer failed"), asyncio.CancelledError()])
async def test_start_transfer_failure_closes_every_raw_and_transferred_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    settings = readiness_config(tmp_path).model_copy(
        update={"ftps_passive_port_min": 49001, "ftps_passive_port_max": 49002}
    )
    server = FtpsTlsServer(
        settings,
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    opened: list[ProbeSocket] = []

    def open_owned(_host: str, port: int) -> ProbeSocket:
        owned = ProbeSocket(port)
        opened.append(owned)
        return owned

    class TransferredServer(PassiveServer):
        def __init__(self, owned: ProbeSocket) -> None:
            super().__init__(port=owned.port)
            self.owned = owned

        def close(self) -> None:
            super().close()
            self.owned.close()

    calls = 0

    async def create_server(
        _loop: object, _factory: object, *, sock: ProbeSocket, **_kwargs: object
    ) -> TransferredServer:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise failure
        return TransferredServer(sock)

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(server_module, "_open_owned_socket", open_owned)
    monkeypatch.setattr(server_module, "_tls_context", lambda _certificate, _key: object())
    monkeypatch.setattr(type(loop), "create_server", create_server)

    with pytest.raises(type(failure)):
        await server.start()

    assert len(opened) == 3
    assert all(owned.close_attempts == 1 for owned in opened)
    assert not server._owned_servers
    assert not server._owned_sockets


async def test_control_never_activates_when_passive_activation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = readiness_config(tmp_path).model_copy(
        update={"ftps_passive_port_min": 49001, "ftps_passive_port_max": 49002}
    )
    server = FtpsTlsServer(
        settings,
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    opened: list[ProbeSocket] = []

    def open_owned(_host: str, port: int) -> ProbeSocket:
        owned = ProbeSocket(port)
        opened.append(owned)
        return owned

    class ActivationServer(PassiveServer):
        def __init__(self, owned: ProbeSocket, *, fail: bool = False) -> None:
            super().__init__(port=owned.port)
            self.owned = owned
            self.fail = fail

        async def start_serving(self) -> None:
            self.start_calls += 1
            if self.fail:
                raise OSError("activation failed")

        def close(self) -> None:
            super().close()
            self.owned.close()

    passives: list[ActivationServer] = []

    async def create_server(
        _loop: object, _factory: object, *, sock: ProbeSocket, **_kwargs: object
    ) -> ActivationServer:
        passive = ActivationServer(sock, fail=len(passives) == 1)
        passives.append(passive)
        return passive

    control: ActivationServer | None = None

    async def start_server(*_args: object, **kwargs: object) -> ActivationServer:
        nonlocal control
        control = ActivationServer(cast(ProbeSocket, kwargs["sock"]))
        return control

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(server_module, "_open_owned_socket", open_owned)
    monkeypatch.setattr(server_module, "_tls_context", lambda _certificate, _key: object())
    monkeypatch.setattr(type(loop), "create_server", create_server)
    monkeypatch.setattr(asyncio, "start_server", start_server)

    with pytest.raises(OSError, match="activation failed"):
        await server.start()

    assert [passive.start_calls for passive in passives] == [1, 1]
    assert control is not None and control.start_calls == 0
    assert all(owned.close_attempts == 1 for owned in opened)
    assert not server._owned_servers


async def test_sockets_and_close_cancel_pending_sessions(tmp_path: Path) -> None:
    server = FtpsTlsServer(
        config(tmp_path).model_copy(update={"shutdown_timeout_seconds": 0.03}),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    assert server.sockets == ()
    waiting = asyncio.Event()

    async def pending_session() -> None:
        await waiting.wait()

    task = asyncio.create_task(pending_session())
    await asyncio.sleep(0)
    server._sessions.add(task)
    await server.close()
    assert task.cancelled()


async def test_accept_fail_closed_limits_errors_and_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    closing = Writer()
    server._closing = True
    await server._accept(asyncio.StreamReader(), cast(asyncio.StreamWriter, closing))
    assert closing.closed

    server._closing = False
    server._ready = True
    server._sessions.add(cast(asyncio.Task[None], object()))
    limited = Writer()
    await server._accept(asyncio.StreamReader(), cast(asyncio.StreamWriter, limited))
    assert limited.closed
    server._sessions.clear()

    async def protocol_error(_reader: asyncio.StreamReader, _writer: asyncio.StreamWriter) -> None:
        raise FtpsProtocolError("rejected")

    monkeypatch.setattr(server, "_session", protocol_error)
    writer = Writer()
    await server._accept(asyncio.StreamReader(), cast(asyncio.StreamWriter, writer))
    assert writer.closed

    async def unknown_error(_reader: asyncio.StreamReader, _writer: asyncio.StreamWriter) -> None:
        raise RuntimeError("peer-controlled failure")

    monkeypatch.setattr(server, "_session", unknown_error)
    await server._accept(asyncio.StreamReader(), cast(asyncio.StreamWriter, Writer()))

    async def cancelled(_reader: asyncio.StreamReader, _writer: asyncio.StreamWriter) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(server, "_session", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await server._accept(asyncio.StreamReader(), cast(asyncio.StreamWriter, Writer()))


async def test_session_upload_closes_passive_and_stages_exact_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = config(tmp_path)
    stage = Stage()
    staging = Staging(stage)
    auth = Authenticator()
    server = FtpsTlsServer(
        settings,
        cast(CompatibilityAuthenticator, auth),
        cast(FtpsStagingStore, staging),
        ADMISSIONS,
        clock_ms=lambda: 10,
    )
    data_reader = asyncio.StreamReader()
    data_reader.feed_data(b"one" + b"two")
    data_reader.feed_eof()
    data_writer = Writer(peer=("127.0.0.1", 2222), ssl_object=object())
    passive = PassiveServer()

    async def open_passive(
        _peer: str,
        slot: _DataConnectionSlot,
    ) -> PassiveServer:
        slot.accept((data_reader, cast(asyncio.StreamWriter, data_writer), "127.0.0.1", True))
        return passive

    monkeypatch.setattr(server, "_open_passive", open_passive)
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"USER bblp\r\n"
        + f"PASS {ACCESS_CODE}\r\n".encode()
        + b"PBSZ 0\r\nPROT P\r\nPASV\r\nSTOR /observation.3mf\r\nQUIT\r\n"
    )
    reader.feed_eof()
    writer = Writer()
    await server._session(reader, cast(asyncio.StreamWriter, writer))

    assert stage.chunks == [b"onetwo"]
    assert stage.committed and not stage.aborted
    assert staging.reservations == [
        {
            "printer_uuid": PRINCIPAL.printer_uuid,
            "client_path": "/observation.3mf",
            "created_at_unix_ms": 10,
            "expires_at_unix_ms": 60_010,
        }
    ]
    assert data_writer.closed and passive.closed and passive.waited
    assert bytes(writer.output).endswith(b"226 Transfer complete\r\n221 Goodbye\r\n")
    assert auth.revalidate_calls == [PRINCIPAL] * 7
    assert len(auth.admission_leases) == 1
    assert server._printer_sessions == {}


async def test_lifecycle_revocation_closes_accepted_data_before_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = printer(
        printer_uuid=PRINCIPAL.printer_uuid,
        safety_profiles=(safety_profile(printer_uuid=PRINCIPAL.printer_uuid),),
    )
    sessions = CompatibilitySessionRegistry()
    sessions.reconcile_committed(record)
    auth = Authenticator()
    auth.principal = CompatibilityPrincipal(
        printer_uuid=record.printer_uuid,
        proxy_serial=record.proxy_serial,
        record_revision=record.revision,
        control_enabled=record.control_enabled,
        dispatch_enabled=record.dispatch_enabled,
    )
    server = FtpsTlsServer(
        config(tmp_path).model_copy(update={"shutdown_timeout_seconds": 0.01}),
        cast(CompatibilityAuthenticator, auth),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
        session_registry=sessions,
    )
    accepted_writer = Writer(peer=("127.0.0.1", 2), ssl_object=object())
    opened = asyncio.Event()
    release = asyncio.Event()

    async def open_passive(_peer: str, slot: _DataConnectionSlot) -> PassiveServer:
        assert slot.accept(
            (asyncio.StreamReader(), cast(asyncio.StreamWriter, accepted_writer), "127.0.0.1", True)
        )
        opened.set()
        await release.wait()
        return PassiveServer()

    monkeypatch.setattr(server, "_open_passive", open_passive)
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"USER bblp\r\n" + f"PASS {ACCESS_CODE}\r\n".encode() + b"PBSZ 0\r\nPROT P\r\nPASV\r\n"
    )
    writer = Writer()
    task = asyncio.create_task(server._session(reader, cast(asyncio.StreamWriter, writer)))
    await opened.wait()
    sessions.reconcile_committed(
        record.model_copy(update={"revision": 2, "updated_at_unix_ms": 1_100})
    )
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert accepted_writer.closed
    assert writer.closed
    assert server._printer_sessions == {}
    assert not server._transfers.locked()


async def test_stale_ftps_session_is_closed_before_230(tmp_path: Path) -> None:
    record = printer(
        printer_uuid=PRINCIPAL.printer_uuid,
        safety_profiles=(safety_profile(printer_uuid=PRINCIPAL.printer_uuid),),
    )
    sessions = CompatibilitySessionRegistry()
    sessions.reconcile_committed(
        record.model_copy(update={"revision": 2, "updated_at_unix_ms": 1_100})
    )
    auth = Authenticator()
    auth.principal = CompatibilityPrincipal(
        printer_uuid=record.printer_uuid,
        proxy_serial=record.proxy_serial,
        record_revision=record.revision,
        control_enabled=record.control_enabled,
        dispatch_enabled=record.dispatch_enabled,
    )
    server = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, auth),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
        session_registry=sessions,
    )
    reader = asyncio.StreamReader()
    reader.feed_data(b"USER bblp\r\n" + f"PASS {ACCESS_CODE}\r\n".encode())
    writer = Writer()
    task = asyncio.create_task(server._session(reader, cast(asyncio.StreamWriter, writer)))

    with pytest.raises(asyncio.CancelledError):
        await task
    assert writer.closed
    assert b"230 Authenticated" not in writer.output


async def test_upload_retains_passive_listener_until_a_later_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = config(tmp_path).model_copy(
        update={"shutdown_timeout_seconds": 0.01, "transfer_timeout_seconds": 1.0}
    )
    server = FtpsTlsServer(
        settings,
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    passive = HangingPassiveServer()
    data_reader = asyncio.StreamReader()
    data_reader.feed_data(b"one")
    data_reader.feed_eof()
    data_writer = Writer(peer=("127.0.0.1", 2), ssl_object=object())

    async def open_passive(_peer: str, slot: _DataConnectionSlot) -> HangingPassiveServer:
        server._passive_servers.add(cast(asyncio.Server, passive))
        assert slot.accept(
            (data_reader, cast(asyncio.StreamWriter, data_writer), "127.0.0.1", True)
        )
        return passive

    monkeypatch.setattr(server, "_open_passive", open_passive)

    async def receive_stage(
        _reader: asyncio.StreamReader,
        _writer: asyncio.StreamWriter,
        _principal: CompatibilityPrincipal,
        _client_path: str,
    ) -> None:
        return None

    monkeypatch.setattr(server, "_receive_stage", receive_stage)
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"USER bblp\r\n"
        + f"PASS {ACCESS_CODE}\r\n".encode()
        + b"PBSZ 0\r\nPROT P\r\nPASV\r\nSTOR /observation.3mf\r\nQUIT\r\n"
    )
    writer = Writer()
    await server._session(reader, cast(asyncio.StreamWriter, writer))

    assert data_writer.closed
    assert cast(asyncio.Server, passive) in server._passive_servers
    await server._close_passive_servers()
    assert cast(asyncio.Server, passive) in server._passive_servers
    passive.hanging = False
    await server.close()
    assert cast(asyncio.Server, passive) not in server._passive_servers


async def test_lifecycle_revocation_bounds_hanging_passive_close_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = printer(
        printer_uuid=PRINCIPAL.printer_uuid,
        safety_profiles=(safety_profile(printer_uuid=PRINCIPAL.printer_uuid),),
    )
    sessions = CompatibilitySessionRegistry()
    sessions.reconcile_committed(record)
    auth = Authenticator()
    auth.principal = CompatibilityPrincipal(
        printer_uuid=record.printer_uuid,
        proxy_serial=record.proxy_serial,
        record_revision=record.revision,
        control_enabled=record.control_enabled,
        dispatch_enabled=record.dispatch_enabled,
    )
    server = FtpsTlsServer(
        config(tmp_path).model_copy(
            update={"shutdown_timeout_seconds": 0.01, "transfer_timeout_seconds": 1.0}
        ),
        cast(CompatibilityAuthenticator, auth),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
        session_registry=sessions,
    )
    passive = HangingPassiveServer()

    async def open_passive(_peer: str, _slot: _DataConnectionSlot) -> HangingPassiveServer:
        server._passive_servers.add(cast(asyncio.Server, passive))
        return passive

    monkeypatch.setattr(server, "_open_passive", open_passive)
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"USER bblp\r\n"
        + f"PASS {ACCESS_CODE}\r\n".encode()
        + b"PBSZ 0\r\nPROT P\r\nPASV\r\nSTOR /observation.3mf\r\n"
    )
    writer = Writer()
    task = asyncio.create_task(server._session(reader, cast(asyncio.StreamWriter, writer)))
    for _ in range(20):
        if b"150 Opening protected data connection" in writer.output:
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("transfer did not reach passive-data wait")
    sessions.reconcile_committed(
        record.model_copy(update={"revision": 2, "updated_at_unix_ms": 1_100})
    )

    with pytest.raises(asyncio.CancelledError):
        await task

    assert passive.closed and passive.waited and writer.closed
    assert cast(asyncio.Server, passive) in server._passive_servers
    assert passive.wait_calls == 1
    assert server._printer_sessions == {}
    assert not server._transfers.locked()
    passive.hanging = False
    await server.close()
    assert cast(asyncio.Server, passive) not in server._passive_servers
    assert passive.wait_calls == 2


async def test_session_fail_closed_before_stage_for_data_and_resource_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = config(tmp_path)
    auth = Authenticator()
    staging = Staging()
    server = FtpsTlsServer(
        settings,
        cast(CompatibilityAuthenticator, auth),
        cast(FtpsStagingStore, staging),
        ADMISSIONS,
    )

    async def no_passive(_peer: str, _future: object) -> PassiveServer:
        raise AssertionError("must not open a passive listener when transfer is unavailable")

    await server._transfers.acquire()
    monkeypatch.setattr(server, "_open_passive", no_passive)
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"USER bblp\r\n" + f"PASS {ACCESS_CODE}\r\n".encode() + b"PBSZ 0\r\nPROT P\r\nPASV\r\n"
    )
    reader.feed_eof()
    await server._session(reader, cast(asyncio.StreamWriter, Writer()))
    server._transfers.release()
    assert staging.reservations == []

    async def mismatched_peer(
        _peer: str,
        slot: _DataConnectionSlot,
    ) -> PassiveServer:
        slot.accept(
            (
                asyncio.StreamReader(),
                cast(asyncio.StreamWriter, Writer(peer=("127.0.0.2", 3))),
                "127.0.0.2",
                True,
            )
        )
        return PassiveServer()

    monkeypatch.setattr(server, "_open_passive", mismatched_peer)
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"USER bblp\r\n"
        + f"PASS {ACCESS_CODE}\r\n".encode()
        + b"PBSZ 0\r\nPROT P\r\nPASV\r\nSTOR /observation.3mf\r\n"
    )
    reader.feed_eof()
    await server._session(reader, cast(asyncio.StreamWriter, Writer()))
    assert staging.reservations == []

    async def unprotected_data(
        _peer: str,
        slot: _DataConnectionSlot,
    ) -> PassiveServer:
        slot.accept(
            (
                asyncio.StreamReader(),
                cast(asyncio.StreamWriter, Writer(peer=("127.0.0.1", 3))),
                "127.0.0.1",
                False,
            )
        )
        return PassiveServer()

    monkeypatch.setattr(server, "_open_passive", unprotected_data)
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"USER bblp\r\n"
        + f"PASS {ACCESS_CODE}\r\n".encode()
        + b"PBSZ 0\r\nPROT P\r\nPASV\r\nSTOR /observation.3mf\r\n"
    )
    reader.feed_eof()
    await server._session(reader, cast(asyncio.StreamWriter, Writer()))
    assert staging.reservations == []


async def test_session_rejects_unknown_peer_invalid_auth_and_stale_principal(
    tmp_path: Path,
) -> None:
    settings = config(tmp_path)
    staging = Staging()
    auth = Authenticator()
    server = FtpsTlsServer(
        settings,
        cast(CompatibilityAuthenticator, auth),
        cast(FtpsStagingStore, staging),
        ADMISSIONS,
    )
    await server._session(
        asyncio.StreamReader(), cast(asyncio.StreamWriter, Writer(peer=("::1", 4)))
    )

    auth.principal = None
    reader = asyncio.StreamReader()
    reader.feed_data(b"USER bblp\r\n" + f"PASS {ACCESS_CODE}\r\n".encode())
    reader.feed_eof()
    writer = Writer()
    await server._session(reader, cast(asyncio.StreamWriter, writer))
    assert bytes(writer.output).endswith(b"530 Authentication failed\r\n")

    auth.principal = PRINCIPAL
    auth.valid = False
    reader = asyncio.StreamReader()
    reader.feed_data(b"USER bblp\r\n" + f"PASS {ACCESS_CODE}\r\n".encode() + b"PBSZ 0\r\n")
    reader.feed_eof()
    await server._session(reader, cast(asyncio.StreamWriter, Writer()))
    assert server._printer_sessions == {}

    server._closing = True
    closing_writer = Writer()
    await server._session(asyncio.StreamReader(), cast(asyncio.StreamWriter, closing_writer))
    assert bytes(closing_writer.output) == b"220 Klove ready\r\n"


async def test_session_fails_closed_for_impossible_protocol_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )

    class UnhandledProtocol:
        def __init__(self, _authorizer: object) -> None:
            self.calls = 0

        def receive_line(self, _line: bytes) -> FtpsEvent:
            self.calls += 1
            if self.calls == 1:
                return FtpsEvent(FtpsAction.PASSWORD_PRESENTED, ACCESS_CODE)
            if self.calls == 2:
                return FtpsEvent(FtpsAction.DATA_TRANSFER_COMPLETED)
            return FtpsEvent(FtpsAction.SESSION_CLOSED)

    monkeypatch.setattr(server_module, "FtpsProtocolSession", UnhandledProtocol)
    reader = asyncio.StreamReader()
    reader.feed_data(b"ignored\r\nignored\r\n")
    with pytest.raises(FtpsProtocolError, match="unexpected"):
        await server._session(reader, cast(asyncio.StreamWriter, Writer()))


async def test_passive_leases_are_distinct_bounded_and_reusable(tmp_path: Path) -> None:
    settings = config(tmp_path).model_copy(
        update={"ftps_passive_port_min": 40000, "ftps_passive_port_max": 40001}
    )
    server = FtpsTlsServer(
        settings,
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    first_endpoint = _PassiveEndpoint(40000, cast(asyncio.Server, PassiveServer(port=40000)))
    second_endpoint = _PassiveEndpoint(40001, cast(asyncio.Server, PassiveServer(port=40001)))
    server._passive_endpoints = {40000: first_endpoint, 40001: second_endpoint}
    server._ready = True
    first_slot = _DataConnectionSlot(asyncio.get_running_loop().create_future())
    second_slot = _DataConnectionSlot(asyncio.get_running_loop().create_future())

    first = await server._open_passive("127.0.0.1", first_slot)
    second = await server._open_passive("127.0.0.1", second_slot)
    with pytest.raises(OSError, match="lease is available"):
        await server._open_passive(
            "127.0.0.1", _DataConnectionSlot(asyncio.get_running_loop().create_future())
        )

    assert first.endpoint is first_endpoint
    assert second.endpoint is second_endpoint
    first_slot.close()
    first.release()
    replacement = await server._open_passive(
        "127.0.0.1", _DataConnectionSlot(asyncio.get_running_loop().create_future())
    )
    assert replacement.endpoint is first_endpoint
    assert replacement is not first
    first.release()
    assert first_endpoint.lease is replacement


async def test_accept_time_passive_lease_cannot_enter_its_successor(tmp_path: Path) -> None:
    server = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    endpoint = _PassiveEndpoint(40400, cast(asyncio.Server, PassiveServer(port=40400)))
    first_slot = _DataConnectionSlot(asyncio.get_running_loop().create_future())
    first = _PassiveLease(endpoint, "127.0.0.1", first_slot)
    endpoint.lease = first
    accepted_snapshot = first
    first_slot.close()
    first.release()
    second_slot = _DataConnectionSlot(asyncio.get_running_loop().create_future())
    second = _PassiveLease(endpoint, "127.0.0.1", second_slot)
    endpoint.lease = second
    server._ready = True
    stale_writer = Writer(peer=("127.0.0.1", 4), ssl_object=object())

    await server._accept_passive(
        endpoint,
        accepted_snapshot,
        asyncio.StreamReader(),
        cast(asyncio.StreamWriter, stale_writer),
    )

    assert stale_writer.closed
    assert not second_slot.future.done()
    current_writer = Writer(peer=("127.0.0.1", 5), ssl_object=object())
    current_reader = asyncio.StreamReader()
    await server._accept_passive(
        endpoint,
        second,
        current_reader,
        cast(asyncio.StreamWriter, current_writer),
    )
    assert second_slot.future.result()[0] is current_reader


async def test_uncertain_owned_listener_close_is_retained_and_retried(tmp_path: Path) -> None:
    server = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    uncertain = UncertainCloseServer()
    server._owned_servers.add(cast(asyncio.Server, uncertain))

    await server.close()
    assert cast(asyncio.Server, uncertain) in server._owned_servers
    assert uncertain.close_clients_calls == 1

    await server.close()
    assert cast(asyncio.Server, uncertain) not in server._owned_servers
    assert uncertain.close_calls == 2
    assert uncertain.close_clients_calls == 2


async def test_shutdown_closes_tracked_handshakes_and_post_tls_clients(tmp_path: Path) -> None:
    server = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    listener = PassiveServer()
    server._owned_servers.add(cast(asyncio.Server, listener))
    writer = Writer(ssl_object=object())
    server._passive_client_writers.add(cast(asyncio.StreamWriter, writer))

    await server.close()

    assert listener.close_clients_calls == 1
    assert writer.closed


async def test_open_passive_checks_all_ports_and_refuses_unprotected_or_wrong_peer(
    tmp_path: Path,
) -> None:
    settings = config(tmp_path).model_copy(
        update={"ftps_passive_port_min": 40000, "ftps_passive_port_max": 40001}
    )
    server = FtpsTlsServer(
        settings,
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    endpoint_1 = _PassiveEndpoint(40000, cast(asyncio.Server, PassiveServer(port=40000)))
    endpoint_2 = _PassiveEndpoint(40001, cast(asyncio.Server, PassiveServer(port=40001)))
    server._passive_endpoints = {40000: endpoint_1, 40001: endpoint_2}
    server._ready = True
    slot = _DataConnectionSlot(asyncio.get_running_loop().create_future())
    passive = await server._open_passive("127.0.0.1", slot)
    assert passive.endpoint is endpoint_1
    assert _bound_port(passive) == 40000

    wrong = Writer(peer=("127.0.0.2", 4))
    await server._accept_passive(
        endpoint_1,
        passive,
        asyncio.StreamReader(),
        cast(asyncio.StreamWriter, wrong),
    )
    assert wrong.closed and not slot.future.done()
    unprotected = Writer(peer=("127.0.0.1", 4))
    await server._accept_passive(
        endpoint_1,
        passive,
        asyncio.StreamReader(),
        cast(asyncio.StreamWriter, unprotected),
    )
    assert unprotected.closed and not slot.future.done()
    protected = Writer(peer=("127.0.0.1", 4), ssl_object=object())
    data_reader = asyncio.StreamReader()
    await server._accept_passive(
        endpoint_1,
        passive,
        data_reader,
        cast(asyncio.StreamWriter, protected),
    )
    assert slot.future.result() == (
        data_reader,
        cast(asyncio.StreamWriter, protected),
        "127.0.0.1",
        True,
    )
    duplicate = Writer(peer=("127.0.0.1", 4), ssl_object=object())
    await server._accept_passive(
        endpoint_1,
        passive,
        asyncio.StreamReader(),
        cast(asyncio.StreamWriter, duplicate),
    )
    assert duplicate.closed
    slot.close()
    passive.release()
    late = Writer(peer=("127.0.0.1", 4), ssl_object=object())
    await server._accept_passive(
        endpoint_1,
        passive,
        asyncio.StreamReader(),
        cast(asyncio.StreamWriter, late),
    )
    assert late.closed


async def test_open_passive_reports_exhaustion_and_stage_aborts_on_failure(
    tmp_path: Path,
) -> None:
    server = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )

    server._ready = True
    slot = _DataConnectionSlot(asyncio.get_running_loop().create_future())
    with pytest.raises(OSError, match=r"no configured.*lease"):
        await server._open_passive("127.0.0.1", slot)

    stage = Stage(write_error=OSError("full"))
    staging = Staging(stage)
    failing = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, staging),
        ADMISSIONS,
    )
    reader = asyncio.StreamReader()
    reader.feed_data(b"bytes")
    writer = Writer()
    with pytest.raises(OSError, match="full"):
        await failing._receive_stage(
            reader, cast(asyncio.StreamWriter, writer), PRINCIPAL, "/observation.3mf"
        )
    assert stage.aborted and writer.closed


async def test_transfer_has_one_deadline_and_revalidates_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = config(tmp_path).model_copy(update={"transfer_timeout_seconds": 0.01})
    staging = Staging()
    server = FtpsTlsServer(
        settings,
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, staging),
        ADMISSIONS,
    )
    passive = PassiveServer()

    async def no_data(
        _peer: str,
        _slot: _DataConnectionSlot,
    ) -> PassiveServer:
        return passive

    monkeypatch.setattr(server, "_open_passive", no_data)
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"USER bblp\r\n"
        + f"PASS {ACCESS_CODE}\r\n".encode()
        + b"PBSZ 0\r\nPROT P\r\nPASV\r\nSTOR /observation.3mf\r\n"
    )
    reader.feed_eof()
    with pytest.raises(TimeoutError):
        await server._session(reader, cast(asyncio.StreamWriter, Writer()))
    assert passive.closed and passive.waited
    assert staging.reservations == []

    stale_auth = Authenticator()
    stale_auth.valid = False
    stage = Stage()
    stale = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, stale_auth),
        cast(FtpsStagingStore, Staging(stage)),
        ADMISSIONS,
    )
    data = asyncio.StreamReader()
    data.feed_data(b"bytes")
    data.feed_eof()
    with pytest.raises(ValueError, match="principal changed"):
        await stale._receive_stage(
            data, cast(asyncio.StreamWriter, Writer()), PRINCIPAL, "/observation.3mf"
        )
    assert stage.aborted and not stage.committed

    class FinalStaleAuthenticator(Authenticator):
        async def revalidate(
            self, principal: CompatibilityPrincipal, admission_lease: object | None = None
        ) -> bool:
            result = await super().revalidate(principal, admission_lease)
            return result and len(self.revalidate_calls) == 1

    final_stage = Stage()
    final_stale = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, FinalStaleAuthenticator()),
        cast(FtpsStagingStore, Staging(final_stage)),
        ADMISSIONS,
    )
    final_data = asyncio.StreamReader()
    final_data.feed_data(b"bytes")
    final_data.feed_eof()
    with pytest.raises(ValueError, match="principal changed"):
        await final_stale._receive_stage(
            final_data, cast(asyncio.StreamWriter, Writer()), PRINCIPAL, "/observation.3mf"
        )
    assert final_stage.aborted and not final_stage.committed


async def test_blocking_waits_for_private_file_operation_before_cancellation() -> None:
    assert await _blocking(lambda: "ok") == "ok"
    with pytest.raises(OSError, match="write"):
        await _blocking(lambda: (_ for _ in ()).throw(OSError("write")))

    started = threading.Event()
    release = threading.Event()

    def operation() -> None:
        started.set()
        while not release.is_set():
            time.sleep(0.001)

    task = asyncio.create_task(_blocking(operation))
    await asyncio.to_thread(started.wait)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_idle_timeout_max_commands_and_private_bookkeeping(tmp_path: Path) -> None:
    server = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    assert await server._bounded(asyncio.sleep(0, result="ok")) == "ok"
    with pytest.raises(TimeoutError):
        await server._bounded(asyncio.sleep(10))

    limited = FtpsTlsServer(
        config(tmp_path).model_copy(update={"max_commands_per_session": 1}),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    commands = asyncio.StreamReader()
    commands.feed_data(b"USER bblp\r\nPASS " + ACCESS_CODE.encode() + b"\r\n")
    commands.feed_eof()
    command_writer = Writer()
    await limited._session(commands, cast(asyncio.StreamWriter, command_writer))
    assert bytes(command_writer.output) == b"220 Klove ready\r\n331 Password required\r\n"
    assert server._admit_printer(PRINTER_UUID)
    assert server._admit_printer(PRINTER_UUID)
    assert not server._admit_printer(PRINTER_UUID)
    server._release_printer(PRINTER_UUID)
    assert server._printer_sessions[PRINTER_UUID] == 1
    server._release_printer(PRINTER_UUID)
    assert server._printer_sessions == {}


async def test_wait_closed_suppresses_socket_errors_and_helper_rejections() -> None:
    class FailingWriter(Writer):
        async def wait_closed(self) -> None:
            raise ConnectionError("gone")

    await _wait_closed(cast(asyncio.StreamWriter, FailingWriter()), 0.01)
    assert _ipv4(("127.0.0.1", 1)) == "127.0.0.1"
    assert _ipv4(("::1", 1)) is None
    assert _ipv4(("not-an-address", 1)) is None
    assert _ipv4((1, 2)) is None
    assert _ipv4("127.0.0.1") is None
    assert _bound_port(cast(asyncio.Server, PassiveServer(port=1234))) == 1234
    with pytest.raises(OSError, match="no socket"):
        _bound_port(cast(asyncio.Server, PassiveServer(sockets=[])))
    with pytest.raises(OSError, match="invalid"):
        _bound_port(cast(asyncio.Server, PassiveServer(sockets=[InvalidSocket()])))


async def test_socket_ownership_helper_defensive_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slot = _DataConnectionSlot(asyncio.get_running_loop().create_future())
    slot.close()
    assert not slot.accept(
        (
            asyncio.StreamReader(),
            cast(asyncio.StreamWriter, Writer()),
            "127.0.0.1",
            True,
        )
    )
    endpoint = _PassiveEndpoint(40400)
    lease = _PassiveLease(endpoint, "127.0.0.1", slot)
    assert lease.sockets == ()
    successor = _PassiveLease(endpoint, "127.0.0.1", slot)
    endpoint.lease = successor
    lease.release()
    lease.release()
    assert endpoint.lease is successor
    assert not server_module._valid_owned_socket_config(cast(GroveBridgeConfig, object()))

    settings = readiness_config(tmp_path)

    def invalid_topology(_self: GroveBridgeConfig) -> bool:
        raise ValueError("forged topology")

    monkeypatch.setattr(GroveBridgeConfig, "has_supported_ftps_topology", invalid_topology)
    assert not server_module._valid_owned_socket_config(settings)


def test_open_owned_socket_rejects_inexact_bound_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InexactSocket:
        def __init__(self, *_args: object) -> None:
            self.closed = False

        def setblocking(self, _blocking: bool) -> None:
            return None

        def bind(self, _address: tuple[str, int]) -> None:
            return None

        def getsockname(self) -> tuple[str, int]:
            return ("127.0.0.1", 49001)

        def close(self) -> None:
            self.closed = True

    opened = InexactSocket()
    monkeypatch.setattr(socket, "socket", lambda *_args: opened)

    with pytest.raises(OSError, match="exact configured"):
        server_module._open_owned_socket("127.0.0.1", 49000)
    assert opened.closed


def test_close_owned_sockets_retains_errors_and_first_interruption() -> None:
    ordinary = ProbeSocket(1, OSError("ordinary"))
    first = ProbeSocket(2, KeyboardInterrupt("first"))
    later = ProbeSocket(3, KeyboardInterrupt("later"))
    clean = ProbeSocket(4)
    owned = cast(set[socket.socket], {ordinary, first, later, clean})

    interruption = server_module._close_owned_sockets(owned)

    assert interruption is not None and str(interruption) in {"first", "later"}
    assert cast(socket.socket, clean) not in owned
    assert {ordinary, first, later} <= cast(set[ProbeSocket], owned)
    assert all(probe.close_attempts == 1 for probe in (ordinary, first, later, clean))


async def test_start_acquisition_chains_release_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FtpsTlsServer(
        readiness_config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    acquired = ProbeSocket(49000, KeyboardInterrupt("release interrupted"))
    calls = 0

    def open_owned(_host: str, _port: int) -> ProbeSocket:
        nonlocal calls
        calls += 1
        if calls == 1:
            return acquired
        raise OSError("bind interrupted")

    monkeypatch.setattr(server_module, "_open_owned_socket", open_owned)

    with pytest.raises(OSError, match="bind interrupted") as caught:
        await server.start()

    assert isinstance(caught.value.__cause__, KeyboardInterrupt)
    assert acquired.close_attempts == 1


async def test_owned_listener_close_interruption_combinations(tmp_path: Path) -> None:
    class InterruptServer(PassiveServer):
        def __init__(self, message: str) -> None:
            super().__init__()
            self.message = message

        def close(self) -> None:
            raise KeyboardInterrupt(self.message)

    class BareServer:
        def __init__(self) -> None:
            self.closed = False
            self.waited = False

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            self.waited = True

    bare = BareServer()
    bare_owner = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    bare_owner._owned_servers.add(cast(asyncio.Server, bare))
    await bare_owner._close_owned_listeners()
    assert bare.closed and bare.waited

    server_only = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    interrupted = InterruptServer("server-one")
    second_interrupted = InterruptServer("server-two")
    server_only._owned_servers.update(
        {
            cast(asyncio.Server, interrupted),
            cast(asyncio.Server, second_interrupted),
        }
    )
    with pytest.raises(KeyboardInterrupt, match="server"):
        await server_only._close_owned_listeners()

    socket_only = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    raw = ProbeSocket(1, KeyboardInterrupt("socket"))
    socket_only._owned_sockets.add(cast(socket.socket, raw))
    with pytest.raises(KeyboardInterrupt, match="socket"):
        await socket_only._close_owned_listeners()

    combined = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    combined_server = InterruptServer("server")
    combined_socket = ProbeSocket(2, KeyboardInterrupt("socket"))
    combined._owned_servers.add(cast(asyncio.Server, combined_server))
    combined._owned_sockets.add(cast(socket.socket, combined_socket))
    with pytest.raises(KeyboardInterrupt, match="server") as caught:
        await combined._close_owned_listeners()
    assert isinstance(caught.value.__cause__, KeyboardInterrupt)


async def test_construction_cleanup_settles_after_shield_cancellation_and_retains_ambiguity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = readiness_config(tmp_path).model_copy(
        update={"ftps_passive_port_min": 49001, "ftps_passive_port_max": 49002}
    )
    server = FtpsTlsServer(
        settings,
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    opened: list[ProbeSocket] = []

    def open_owned(_host: str, port: int) -> ProbeSocket:
        owned = ProbeSocket(port)
        opened.append(owned)
        return owned

    class AmbiguousTransferredServer(PassiveServer):
        def __init__(self, owned: ProbeSocket) -> None:
            super().__init__(port=owned.port)
            self.owned = owned
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                raise OSError("uncertain close")
            super().close()
            self.owned.close()

    transferred: AmbiguousTransferredServer | None = None
    calls = 0

    async def create_server(
        _loop: object, _factory: object, *, sock: ProbeSocket, **_kwargs: object
    ) -> AmbiguousTransferredServer:
        nonlocal calls, transferred
        calls += 1
        if calls == 2:
            raise OSError("construction failed")
        transferred = AmbiguousTransferredServer(sock)
        return transferred

    original_shield = asyncio.shield
    shield_calls = 0

    async def cancelled_shield(awaitable: object) -> object:
        nonlocal shield_calls
        shield_calls += 1
        if shield_calls == 1:
            raise asyncio.CancelledError
        return await original_shield(cast(Awaitable[object], awaitable))

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(server_module, "_open_owned_socket", open_owned)
    monkeypatch.setattr(server_module, "_tls_context", lambda _certificate, _key: object())
    monkeypatch.setattr(type(loop), "create_server", create_server)
    monkeypatch.setattr(asyncio, "shield", cancelled_shield)

    with pytest.raises(OSError, match="construction failed"):
        await server.start()

    assert transferred is not None
    assert server._closing
    assert cast(asyncio.Server, transferred) in server._owned_servers
    await server.close()
    assert transferred.close_calls == 2
    assert all(owned.close_attempts == 1 for owned in opened)


async def test_activation_defensive_gap_settles_after_shield_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = readiness_config(tmp_path).model_copy(
        update={"ftps_passive_port_min": 49001, "ftps_passive_port_max": 49002}
    )
    server = FtpsTlsServer(
        settings,
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )

    class MutatingServer(PassiveServer):
        def __init__(self, port: int, *, mutate: bool = False) -> None:
            super().__init__(port=port)
            self.mutate = mutate
            self.close_calls = 0

        async def start_serving(self) -> None:
            self.start_calls += 1
            if self.mutate:
                server._passive_endpoints[49002].server = None

        def close(self) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                raise OSError("uncertain close")
            super().close()

    passives: list[MutatingServer] = []

    async def create_server(
        _loop: object, _factory: object, *, sock: ProbeSocket, **_kwargs: object
    ) -> MutatingServer:
        passive = MutatingServer(sock.port, mutate=not passives)
        passives.append(passive)
        return passive

    control = MutatingServer(49000)

    async def start_server(*_args: object, **_kwargs: object) -> MutatingServer:
        return control

    original_shield = asyncio.shield
    shield_calls = 0

    async def cancelled_shield(awaitable: object) -> object:
        nonlocal shield_calls
        shield_calls += 1
        if shield_calls == 1:
            raise asyncio.CancelledError
        return await original_shield(cast(Awaitable[object], awaitable))

    monkeypatch.setattr(server_module, "_open_owned_socket", lambda _host, port: ProbeSocket(port))
    monkeypatch.setattr(server_module, "_tls_context", lambda _certificate, _key: object())
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(type(loop), "create_server", create_server)
    monkeypatch.setattr(asyncio, "start_server", start_server)
    monkeypatch.setattr(asyncio, "shield", cancelled_shield)

    with pytest.raises(RuntimeError, match="construction was incomplete"):
        await server.start()

    assert server._closing
    assert server._passive_endpoints[49002].server is None
    await server.close()
    assert all(passive.close_calls == 2 for passive in passives)
    assert control.close_calls == 2


async def test_shutdown_releases_live_passive_lease_and_unready_open_denies(
    tmp_path: Path,
) -> None:
    server = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    slot = _DataConnectionSlot(asyncio.get_running_loop().create_future())
    with pytest.raises(OSError, match="not owned"):
        await server._open_passive("127.0.0.1", slot)

    endpoint = _PassiveEndpoint(40400)
    lease = _PassiveLease(endpoint, "127.0.0.1", slot)
    endpoint.lease = lease
    server._passive_endpoints = {40400: endpoint}
    await server.close()
    assert lease.released


@pytest.mark.parametrize("failure", [OSError("peer failed"), asyncio.CancelledError()])
async def test_passive_accept_cleanup_propagates_cancellation_only(
    tmp_path: Path, failure: BaseException
) -> None:
    class FailingSlot:
        def accept(self, _connection: object) -> bool:
            raise failure

    server = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    endpoint = _PassiveEndpoint(40400)
    lease = _PassiveLease(
        endpoint,
        "127.0.0.1",
        cast(_DataConnectionSlot, FailingSlot()),
    )
    endpoint.lease = lease
    server._ready = True
    writer = Writer(peer=("127.0.0.1", 1), ssl_object=object())

    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await server._accept_passive(
                endpoint,
                lease,
                asyncio.StreamReader(),
                cast(asyncio.StreamWriter, writer),
            )
    else:
        await server._accept_passive(
            endpoint,
            lease,
            asyncio.StreamReader(),
            cast(asyncio.StreamWriter, writer),
        )
    assert writer.closed


@pytest.mark.parametrize("failure", [OSError("activation"), asyncio.CancelledError()])
async def test_control_activation_failure_never_admits_before_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    settings = readiness_config(tmp_path)
    auth = Authenticator()
    staging = Staging()
    server = FtpsTlsServer(
        settings,
        cast(CompatibilityAuthenticator, auth),
        cast(FtpsStagingStore, staging),
        ADMISSIONS,
    )
    accepted_writer = Writer()

    class ActivationServer(PassiveServer):
        def __init__(self, callback: object | None = None) -> None:
            super().__init__()
            self.callback = callback

        async def start_serving(self) -> None:
            self.start_calls += 1
            if self.callback is not None:
                reader = asyncio.StreamReader()
                reader.feed_data(b"USER bblp\r\n" + f"PASS {ACCESS_CODE}\r\n".encode())
                reader.feed_eof()
                await cast(Any, self.callback)(reader, cast(asyncio.StreamWriter, accepted_writer))
                raise failure

    async def create_server(_loop: object, _factory: object, **_kwargs: object) -> ActivationServer:
        return ActivationServer()

    async def start_server(callback: object, **_kwargs: object) -> ActivationServer:
        return ActivationServer(callback)

    monkeypatch.setattr(server_module, "_open_owned_socket", lambda _host, port: ProbeSocket(port))
    monkeypatch.setattr(server_module, "_tls_context", lambda _certificate, _key: object())
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(type(loop), "create_server", create_server)
    monkeypatch.setattr(asyncio, "start_server", start_server)

    with pytest.raises(type(failure)):
        await server.start()

    assert accepted_writer.closed
    assert accepted_writer.output == b""
    assert auth.authenticate_calls == []
    assert staging.reservations == []
    assert not server._ready


async def test_passive_accept_time_bound_is_global_before_tls_and_shutdown_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = readiness_config(tmp_path).model_copy(
        update={
            "ftps_passive_port_min": 49001,
            "ftps_passive_port_max": 49002,
            "max_sessions": 4,
            "max_concurrent_transfers": 1,
        }
    )
    server = FtpsTlsServer(
        settings,
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    factories: list[Callable[[], asyncio.Protocol]] = []

    async def create_server(
        _loop: object, factory: Callable[[], asyncio.Protocol], **_kwargs: object
    ) -> PassiveServer:
        factories.append(factory)
        return PassiveServer()

    async def start_server(*_args: object, **_kwargs: object) -> PassiveServer:
        return PassiveServer()

    handshake_release = asyncio.Event()
    start_tls_calls = 0

    async def start_tls(_loop: object, *_args: object, **_kwargs: object) -> asyncio.Transport:
        nonlocal start_tls_calls
        start_tls_calls += 1
        await handshake_release.wait()
        raise OSError("test handshake ended")

    class RawTransport(asyncio.Transport):
        def __init__(self) -> None:
            self.closed = False
            self.paused = False

        def pause_reading(self) -> None:
            self.paused = True

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(server_module, "_open_owned_socket", lambda _host, port: ProbeSocket(port))
    monkeypatch.setattr(server_module, "_tls_context", lambda _certificate, _key: object())
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(type(loop), "create_server", create_server)
    monkeypatch.setattr(type(loop), "start_tls", start_tls)
    monkeypatch.setattr(asyncio, "start_server", start_server)
    await server.start()
    assert settings.max_sessions > settings.max_concurrent_transfers
    assert len(factories) == 2

    first_slot = _DataConnectionSlot(asyncio.get_running_loop().create_future())
    first_lease = await server._open_passive("127.0.0.1", first_slot)
    second_endpoint = server._passive_endpoints[49002]
    second_slot = _DataConnectionSlot(asyncio.get_running_loop().create_future())
    second_lease = _PassiveLease(second_endpoint, "127.0.0.1", second_slot)
    second_endpoint.lease = second_lease

    admitted = factories[0]()
    assert isinstance(admitted, server_module._PassiveTlsProtocol)
    admitted_transport = RawTransport()
    admitted.connection_made(admitted_transport)
    await asyncio.sleep(0)
    assert admitted_transport.paused
    assert start_tls_calls == 1
    assert len(server._passive_protocols) == 1

    rejected = factories[1]()
    assert isinstance(rejected, server_module._RejectPassiveProtocol)
    rejected_transport = RawTransport()
    rejected.connection_made(rejected_transport)
    assert rejected_transport.closed
    assert start_tls_calls == 1

    await server.close()
    assert admitted_transport.closed
    assert not server._passive_protocols
    assert first_lease.released
    assert second_lease.released


async def test_shutdown_baseexception_attempts_every_independent_phase(
    tmp_path: Path,
) -> None:
    class FatalPhase(BaseException):
        pass

    class FatalListener(PassiveServer):
        def close(self) -> None:
            raise FatalPhase("listener")

    class FatalWriter(Writer):
        def close(self) -> None:
            self.closed = True
            raise FatalPhase("writer")

    class ProtocolPhase:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    server = FtpsTlsServer(
        config(tmp_path).model_copy(update={"shutdown_timeout_seconds": 0.03}),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    listener = FatalListener()
    raw = ProbeSocket(1)
    fatal_writer = FatalWriter()
    passive_writer = Writer()
    protocol = ProtocolPhase()
    waiting = asyncio.Event()

    async def pending() -> None:
        await waiting.wait()

    session_task = asyncio.create_task(pending())
    passive_task = asyncio.create_task(pending())
    server._owned_servers.add(cast(asyncio.Server, listener))
    server._owned_sockets.add(cast(socket.socket, raw))
    server._writers.add(cast(asyncio.StreamWriter, fatal_writer))
    server._passive_client_writers.add(cast(asyncio.StreamWriter, passive_writer))
    server._sessions.add(session_task)
    server._passive_client_tasks.add(passive_task)
    server._passive_protocols.add(cast(Any, protocol))

    with pytest.raises(FatalPhase, match="listener"):
        await server.close()

    assert raw.close_attempts == 1
    assert listener.close_clients_calls == 1
    assert fatal_writer.closed and passive_writer.closed
    assert protocol.closed
    assert session_task.cancelled() and passive_task.cancelled()


async def test_shutdown_cancellation_settles_every_later_phase(tmp_path: Path) -> None:
    release_listener = asyncio.Event()

    class BlockingListener(PassiveServer):
        async def wait_closed(self) -> None:
            await release_listener.wait()
            self.waited = True

    server = FtpsTlsServer(
        config(tmp_path).model_copy(update={"shutdown_timeout_seconds": 0.3}),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    listener = BlockingListener()
    raw = ProbeSocket(1)
    writer = Writer()
    waiting = asyncio.Event()

    async def pending() -> None:
        await waiting.wait()

    session_task = asyncio.create_task(pending())
    server._owned_servers.add(cast(asyncio.Server, listener))
    server._owned_sockets.add(cast(socket.socket, raw))
    server._writers.add(cast(asyncio.StreamWriter, writer))
    server._sessions.add(session_task)
    close_task = asyncio.create_task(server.close())
    await asyncio.sleep(0)
    close_task.cancel()
    release_listener.set()

    with pytest.raises(asyncio.CancelledError):
        await close_task

    assert raw.close_attempts == 1
    assert writer.closed
    assert session_task.cancelled()


async def test_shutdown_hanging_writer_waits_share_one_phase_deadline(tmp_path: Path) -> None:
    class HangingWriter(Writer):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0
            self.wait_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            super().close()

        async def wait_closed(self) -> None:
            self.wait_calls += 1
            await asyncio.Event().wait()

    timeout = 0.1
    server = FtpsTlsServer(
        config(tmp_path).model_copy(update={"shutdown_timeout_seconds": timeout}),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    writers = [HangingWriter() for _ in range(4)]
    server._writers.update(cast(list[asyncio.StreamWriter], writers))

    started = time.monotonic()
    await server.close()
    elapsed = time.monotonic() - started
    await asyncio.sleep(0)

    assert elapsed < timeout * 2.5
    assert all(writer.close_calls == 1 for writer in writers)
    assert all(writer.wait_calls == 1 for writer in writers)


async def test_writer_settle_phase_defensive_interruptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FatalPhase(BaseException):
        pass

    class OrdinaryFailureWriter(Writer):
        async def wait_closed(self) -> None:
            raise OSError("ordinary")

    assert (
        await server_module._settle_writer_closed(
            cast(asyncio.StreamWriter, OrdinaryFailureWriter())
        )
        is None
    )

    class CancelledWriter(Writer):
        async def wait_closed(self) -> None:
            raise asyncio.CancelledError

    cancelled_server = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    cancelled_server._writers.add(cast(asyncio.StreamWriter, CancelledWriter()))
    interruption = await cancelled_server._close_locked()
    assert isinstance(interruption, asyncio.CancelledError)

    class HangingWriter(Writer):
        async def wait_closed(self) -> None:
            await asyncio.Event().wait()

    wait_server = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    wait_server._writers.add(cast(asyncio.StreamWriter, HangingWriter()))

    async def fail_wait(*_args: object, **_kwargs: object) -> object:
        raise FatalPhase("writer phase")

    monkeypatch.setattr(asyncio, "wait", fail_wait)
    wait_interruption = await wait_server._close_locked()
    assert isinstance(wait_interruption, FatalPhase)
    await asyncio.sleep(0)


async def test_passive_tls_protocol_defensive_transport_and_upgrade_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    endpoint = _PassiveEndpoint(40400)
    slot = _DataConnectionSlot(asyncio.get_running_loop().create_future())
    lease = _PassiveLease(endpoint, "127.0.0.1", slot)
    endpoint.lease = lease
    server._ready = True

    class NonStreamTransport(asyncio.BaseTransport):
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    rejected = server_module._PassiveTlsProtocol(
        server, endpoint, lease, cast(ssl.SSLContext, object())
    )
    server._passive_protocols.add(rejected)
    nonstream = NonStreamTransport()
    rejected.connection_made(nonstream)
    assert nonstream.closed and rejected not in server._passive_protocols

    absent = server_module._PassiveTlsProtocol(
        server, endpoint, lease, cast(ssl.SSLContext, object())
    )
    server._passive_protocols.add(absent)
    await absent._upgrade()
    assert absent not in server._passive_protocols

    class RawTransport(asyncio.Transport):
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    outcomes: list[object] = [None, OSError("TLS failed")]

    async def start_tls(_loop: object, *_args: object, **_kwargs: object) -> object:
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(type(loop), "start_tls", start_tls)
    for _ in range(2):
        protocol = server_module._PassiveTlsProtocol(
            server, endpoint, lease, cast(ssl.SSLContext, object())
        )
        raw = RawTransport()
        protocol._transport = raw
        server._passive_protocols.add(protocol)
        await protocol._upgrade()
        assert raw.closed
        assert protocol not in server._passive_protocols


async def test_close_cancellation_chains_settled_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FatalPhase(BaseException):
        pass

    server = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def close_locked() -> BaseException:
        started.set()
        await release.wait()
        return FatalPhase("settled")

    monkeypatch.setattr(server, "_close_locked", close_locked)
    closing = asyncio.create_task(server.close())
    await started.wait()
    closing.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await closing
    assert isinstance(caught.value.__cause__, FatalPhase)


async def test_listener_close_cancellation_chains_settled_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FatalPhase(BaseException):
        pass

    server = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def settle() -> BaseException:
        started.set()
        await release.wait()
        return FatalPhase("listener settled")

    monkeypatch.setattr(server, "_close_owned_listeners_settle", settle)
    closing = asyncio.create_task(server._close_owned_listeners())
    await started.wait()
    closing.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await closing
    assert isinstance(caught.value.__cause__, FatalPhase)


async def test_close_locked_preserves_first_phase_interruption_across_later_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FatalPhase(BaseException):
        pass

    class FailingLease:
        def release(self) -> None:
            raise FatalPhase("lease")

    class FailingProtocol:
        def close(self) -> None:
            raise FatalPhase("protocol")

    server = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    endpoint = _PassiveEndpoint(40400)
    endpoint.lease = cast(_PassiveLease, FailingLease())
    server._passive_endpoints = {40400: endpoint}
    server._passive_protocols.add(cast(Any, FailingProtocol()))

    async def fail_listeners() -> None:
        raise FatalPhase("listener")

    monkeypatch.setattr(server, "_close_owned_listeners", fail_listeners)
    interruption = await server._close_locked()
    assert isinstance(interruption, FatalPhase) and str(interruption) == "lease"


@pytest.mark.parametrize("phase", ["protocol", "writer-close", "writer-wait"])
async def test_close_locked_records_each_first_phase_interruption(
    tmp_path: Path, phase: str
) -> None:
    class FatalPhase(BaseException):
        pass

    class FailingProtocol:
        def close(self) -> None:
            raise FatalPhase("protocol")

    class PhaseWriter(Writer):
        def close(self) -> None:
            self.closed = True
            if phase == "writer-close":
                raise FatalPhase("writer-close")

        async def wait_closed(self) -> None:
            if phase == "writer-wait":
                raise FatalPhase("writer-wait")

    server = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    if phase == "protocol":
        server._passive_protocols.add(cast(Any, FailingProtocol()))
    else:
        server._writers.add(cast(asyncio.StreamWriter, PhaseWriter()))
    interruption = await server._close_locked()
    assert isinstance(interruption, FatalPhase) and str(interruption) == phase


@pytest.mark.parametrize("wait_failure_call", [1, 2])
async def test_close_locked_records_wait_interruptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wait_failure_call: int
) -> None:
    class FatalPhase(BaseException):
        pass

    server = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    release = asyncio.Event()

    async def pending() -> None:
        await release.wait()

    task = asyncio.create_task(pending())
    server._sessions.add(task)
    calls = 0

    async def wait(
        pending_tasks: set[asyncio.Task[None]], *, timeout: float
    ) -> tuple[set[asyncio.Task[None]], set[asyncio.Task[None]]]:
        del timeout
        nonlocal calls
        calls += 1
        if calls == wait_failure_call:
            raise FatalPhase(f"wait-{calls}")
        return set(), pending_tasks

    monkeypatch.setattr(asyncio, "wait", wait)
    interruption = await server._close_locked()
    assert isinstance(interruption, FatalPhase)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_close_locked_records_task_cancel_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FatalPhase(BaseException):
        pass

    class FailingTask:
        def done(self) -> bool:
            return False

        def cancel(self) -> None:
            raise FatalPhase("cancel")

    server = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    server._sessions.update(
        {
            cast(asyncio.Task[None], FailingTask()),
            cast(asyncio.Task[None], FailingTask()),
        }
    )

    async def wait(
        pending_tasks: set[asyncio.Task[None]], *, timeout: float
    ) -> tuple[set[asyncio.Task[None]], set[asyncio.Task[None]]]:
        del timeout
        return set(), pending_tasks

    monkeypatch.setattr(asyncio, "wait", wait)
    interruption = await server._close_locked()
    assert isinstance(interruption, FatalPhase) and str(interruption) == "cancel"


async def test_listener_settle_records_client_and_wait_interruptions(tmp_path: Path) -> None:
    class FatalPhase(BaseException):
        pass

    class ClientFailureServer(PassiveServer):
        def __init__(self, failure: BaseException) -> None:
            super().__init__()
            self.failure = failure

        def close_clients(self) -> None:
            raise self.failure

    class WaitFailureServer(PassiveServer):
        async def wait_closed(self) -> None:
            raise FatalPhase("wait")

    server = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    servers = {
        cast(asyncio.Server, ClientFailureServer(OSError("ordinary"))),
        cast(asyncio.Server, ClientFailureServer(FatalPhase("fatal-one"))),
        cast(asyncio.Server, ClientFailureServer(FatalPhase("fatal-two"))),
        cast(asyncio.Server, WaitFailureServer()),
    }
    server._owned_servers.update(servers)
    interruption = await server._close_owned_listeners_settle()
    assert isinstance(interruption, FatalPhase)
    assert all(listener in server._owned_servers for listener in servers)

    wait_only = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    wait_server = WaitFailureServer()
    wait_only._owned_servers.add(cast(asyncio.Server, wait_server))
    wait_interruption = await wait_only._close_owned_listeners_settle()
    assert isinstance(wait_interruption, FatalPhase) and str(wait_interruption) == "wait"


async def test_close_phase_repeated_failures_preserve_first_and_cover_later_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FatalPhase(BaseException):
        pass

    class FailingLease:
        def __init__(self, message: str) -> None:
            self.message = message

        def release(self) -> None:
            raise FatalPhase(self.message)

    class FailingWaitWriter(Writer):
        def __init__(self, message: str) -> None:
            super().__init__()
            self.message = message

        async def wait_closed(self) -> None:
            raise FatalPhase(self.message)

    server = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    first_endpoint = _PassiveEndpoint(40400)
    second_endpoint = _PassiveEndpoint(40401)
    first_endpoint.lease = cast(_PassiveLease, FailingLease("lease-one"))
    second_endpoint.lease = cast(_PassiveLease, FailingLease("lease-two"))
    server._passive_endpoints = {40400: first_endpoint, 40401: second_endpoint}
    server._writers.update(
        {
            cast(asyncio.StreamWriter, FailingWaitWriter("wait-one")),
            cast(asyncio.StreamWriter, FailingWaitWriter("wait-two")),
        }
    )
    release = asyncio.Event()

    async def pending() -> None:
        await release.wait()

    task = asyncio.create_task(pending())
    server._sessions.add(task)

    async def wait(
        _pending_tasks: set[asyncio.Task[None]], *, timeout: float
    ) -> tuple[set[asyncio.Task[None]], set[asyncio.Task[None]]]:
        del timeout
        raise FatalPhase("wait-phase")

    monkeypatch.setattr(asyncio, "wait", wait)
    interruption = await server._close_locked()
    assert isinstance(interruption, FatalPhase)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_listener_close_cancellation_reraises_without_settle_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FtpsTlsServer(
        config(tmp_path),
        cast(CompatibilityAuthenticator, Authenticator()),
        cast(FtpsStagingStore, Staging()),
        ADMISSIONS,
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def settle() -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(server, "_close_owned_listeners_settle", settle)
    closing = asyncio.create_task(server._close_owned_listeners())
    await started.wait()
    closing.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await closing
    assert caught.value.__cause__ is None
