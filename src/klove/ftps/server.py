"""Bounded implicit-TLS FTPS listener for the accepted Grove upload profile."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TypeVar, cast

from klove.config import GroveBridgeConfig
from klove.ftps.protocol import FtpsAction, FtpsProtocolError, FtpsProtocolSession
from klove.ftps.staging import FtpsStagingStore
from klove.orchestration.admission import PrinterAdmissionGates
from klove.security.compatibility import CompatibilityAuthenticator, CompatibilityPrincipal
from klove.security.compatibility_sessions import CompatibilitySessionRegistry

_MAX_LINE_BYTES = 512
_DATA_CHUNK_BYTES = 64 * 1024
_T = TypeVar("_T")
_DataConnection = tuple[asyncio.StreamReader, asyncio.StreamWriter, str, bool]


@dataclass(slots=True)
class _DataConnectionSlot:
    """Own an accepted data writer until the control task claims it."""

    future: asyncio.Future[_DataConnection]
    writer: asyncio.StreamWriter | None = None
    closed: bool = False

    def accept(self, connection: _DataConnection) -> bool:
        """Publish one accepted connection only after retaining its writer."""
        if self.closed:
            return False
        if self.future.done():
            return False
        self.writer = connection[1]
        self.future.set_result(connection)
        return True

    def claim(self) -> _DataConnection:
        """Transfer writer ownership to the resumed control task."""
        connection = self.future.result()
        self.writer = None
        return connection

    def close(self) -> asyncio.StreamWriter | None:
        """Close admission and return any writer still owned by this slot."""
        self.closed = True
        writer, self.writer = self.writer, None
        return writer


@dataclass(slots=True)
class _PassiveEndpoint:
    """One continuously owned passive listener and its current exact lease."""

    port: int
    server: asyncio.Server | None = None
    lease: _PassiveLease | None = None


@dataclass(slots=True)
class _PassiveLease:
    """Temporarily route one owned passive listener to one control session."""

    endpoint: _PassiveEndpoint
    control_peer: str
    slot: _DataConnectionSlot
    released: bool = False

    @property
    def sockets(self) -> tuple[object, ...]:
        server = self.endpoint.server
        if server is None or server.sockets is None:
            return ()
        return tuple(server.sockets)

    def release(self) -> None:
        """Release only this exact lease; stale cleanup cannot release its successor."""
        if self.released:
            return
        self.released = True
        if self.endpoint.lease is self:
            self.endpoint.lease = None


class _RejectPassiveProtocol(asyncio.Protocol):
    """Close one excess or unleased passive connection before TLS begins."""

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        transport.close()


class _PassiveTlsProtocol(asyncio.Protocol):
    """Own one raw passive transport through a bounded server-side TLS upgrade."""

    def __init__(
        self,
        owner: FtpsTlsServer,
        endpoint: _PassiveEndpoint,
        accepted_lease: _PassiveLease,
        context: ssl.SSLContext,
    ) -> None:
        self._owner = owner
        self._endpoint = endpoint
        self._accepted_lease = accepted_lease
        self._context = context
        self._transport: asyncio.Transport | None = None
        self._upgrade_task: asyncio.Task[None] | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        if not isinstance(transport, asyncio.Transport):
            transport.close()
            self._owner._release_passive_protocol(self)
            return
        self._transport = transport
        transport.pause_reading()
        task = asyncio.create_task(self._upgrade())
        self._upgrade_task = task

    async def _upgrade(self) -> None:
        owner = self._owner
        loop = asyncio.get_running_loop()
        raw_transport = self._transport
        reader = asyncio.StreamReader(loop=loop)

        def connected(
            connected_reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            owner._schedule_passive_client(
                self._endpoint,
                self._accepted_lease,
                connected_reader,
                writer,
            )

        stream_protocol = asyncio.StreamReaderProtocol(reader, connected, loop=loop)
        transferred = False
        try:
            if raw_transport is None or owner._closing or not owner._ready:
                return
            tls_transport = await loop.start_tls(
                raw_transport,
                stream_protocol,
                self._context,
                server_side=True,
                ssl_handshake_timeout=owner._config.transfer_timeout_seconds,
                ssl_shutdown_timeout=owner._config.shutdown_timeout_seconds,
            )
            if tls_transport is None:
                raise ConnectionError("FTPS passive TLS upgrade did not return a transport")
            stream_protocol.connection_made(tls_transport)
            self._transport = None
            transferred = True
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError, TimeoutError, ssl.SSLError):
            pass
        finally:
            if transferred:
                self._upgrade_task = None
                owner._release_passive_protocol(self)
            else:
                self.close()

    def close(self) -> asyncio.Task[None] | None:
        """Close the current raw/TLS transport and release its global permit once."""
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()
        task, self._upgrade_task = self._upgrade_task, None
        current = asyncio.current_task()
        if task is not None and task is not current and not task.done():
            task.cancel()
        self._owner._release_passive_protocol(self)
        return task if task is not current else None


class FtpsBindDiagnosticCode(StrEnum):
    """Fixed, non-secret dispositions for FTPS bind diagnostics."""

    CONTROL_PORT_UNAVAILABLE = "ftps_control_port_unavailable"
    PASSIVE_PORT_UNAVAILABLE = "ftps_passive_port_unavailable"
    PROBE_RELEASE_FAILED = "ftps_bind_probe_release_failed"
    UNSUPPORTED_PRIVATE_TOPOLOGY = "ftps_unsupported_private_topology"


@dataclass(frozen=True, slots=True)
class FtpsBindDiagnosticResult:
    """The bounded result of checking the configured FTPS bind set.

    A probe-release failure makes no availability or successful-release claim.
    """

    available: bool
    code: FtpsBindDiagnosticCode | None = None

    def __post_init__(self) -> None:
        if type(self.available) is not bool:
            raise ValueError("FTPS diagnostic availability must be boolean")
        if self.available and self.code is not None:
            raise ValueError("available FTPS diagnostic cannot carry a code")
        if not self.available and type(self.code) is not FtpsBindDiagnosticCode:
            raise ValueError("unavailable FTPS diagnostic requires one exact code")


class FtpsBindSetDiagnostic:
    """Check the exact non-actuating FTPS socket set once.

    This is diagnostic-only: it does not create a TLS context, accept a
    connection, authenticate a client, expose a listener, or make any later
    listener-ready claim. Every acquired socket close is attempted before return.
    """

    def __init__(self, config: GroveBridgeConfig) -> None:
        if type(config) is not GroveBridgeConfig or not config.enabled:
            raise ValueError("enabled Grove bridge configuration required")
        self._config = config

    def check(self) -> FtpsBindDiagnosticResult:
        """Bind the exact port set and attempt to release every acquired probe."""
        if not self._config.has_supported_ftps_topology():
            return FtpsBindDiagnosticResult(
                False, FtpsBindDiagnosticCode.UNSUPPORTED_PRIVATE_TOPOLOGY
            )
        probes: list[socket.socket] = []
        result: FtpsBindDiagnosticResult
        pending: BaseException | None = None
        try:
            probes.append(
                _open_probe_socket(self._config.listen_host, self._config.ftps_control_port)
            )
        except OSError:
            result = FtpsBindDiagnosticResult(
                False, FtpsBindDiagnosticCode.CONTROL_PORT_UNAVAILABLE
            )
        else:
            try:
                for port in range(
                    self._config.ftps_passive_port_min,
                    self._config.ftps_passive_port_max + 1,
                ):
                    probes.append(_open_probe_socket(self._config.listen_host, port))
            except OSError:
                result = FtpsBindDiagnosticResult(
                    False, FtpsBindDiagnosticCode.PASSIVE_PORT_UNAVAILABLE
                )
            except BaseException as exc:
                pending = exc
            else:
                result = FtpsBindDiagnosticResult(True)
        release_failed, release_interruption = _release_probes(probes)
        if pending is not None:
            if release_interruption is not None:
                raise pending from release_interruption
            raise pending
        if release_interruption is not None:
            raise release_interruption
        if release_failed:
            return FtpsBindDiagnosticResult(False, FtpsBindDiagnosticCode.PROBE_RELEASE_FAILED)
        return result


def _open_probe_socket(host: str, port: int) -> socket.socket:
    """Bind one exact TCP port without creating an accepting listener."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except BaseException:
        probe.close()
        raise
    return probe


def _open_owned_socket(host: str, port: int) -> socket.socket:
    """Exclusively bind one exact listener socket for runtime ownership."""
    owned = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        owned.setblocking(False)
        owned.bind((host, port))
        bound = owned.getsockname()
        if (
            not isinstance(bound, tuple)
            or len(bound) < 2
            or _ipv4(bound) != host
            or type(bound[1]) is not int
            or bound[1] != port
        ):
            raise OSError("FTPS owned socket did not bind the exact configured address")
    except BaseException:
        owned.close()
        raise
    return owned


def _valid_owned_socket_config(config: GroveBridgeConfig) -> bool:
    """Defensively revalidate the exact runtime-owned IPv4 socket set."""
    if type(config) is not GroveBridgeConfig:
        return False
    try:
        values = (
            config.ftps_control_port,
            config.ftps_passive_port_min,
            config.ftps_passive_port_max,
        )
        passive = range(config.ftps_passive_port_min, config.ftps_passive_port_max + 1)
        fields_are_exact = (
            config.enabled
            and all(type(port) is int and 1 <= port <= 65535 for port in values)
            and type(config.mqtt_port) is int
            and 1 <= config.mqtt_port <= 65535
            and config.ftps_passive_port_max >= config.ftps_passive_port_min
            and len(passive) <= 64
            and config.ftps_control_port not in passive
            and config.mqtt_port != config.ftps_control_port
            and config.mqtt_port not in passive
            and type(config.listen_host) is str
            and type(config.ftps_advertised_ipv4) is str
            and isinstance(config.tls_certificate_file, Path)
            and isinstance(config.tls_private_key_file, Path)
            and config.tls_certificate_file != config.tls_private_key_file
        )
        return fields_are_exact and config.has_supported_ftps_topology()
    except (TypeError, ValueError):
        return False


def _close_owned_sockets(owned_sockets: set[socket.socket]) -> BaseException | None:
    """Attempt every raw close; retain any handle whose release is ambiguous."""
    interruption: BaseException | None = None
    for owned in tuple(owned_sockets):
        try:
            owned.close()
        except Exception:  # noqa: S110 -- retain the ambiguous socket for explicit retry.
            pass
        except BaseException as exc:
            if interruption is None:
                interruption = exc
        else:
            owned_sockets.discard(owned)
    return interruption


def _release_probes(probes: list[socket.socket]) -> tuple[bool, BaseException | None]:
    """Attempt every close, preserving only fixed ordinary failures."""
    ordinary_failure = False
    interruption: BaseException | None = None
    while probes:
        try:
            probes.pop().close()
        except Exception:
            ordinary_failure = True
        except BaseException as exc:
            if interruption is None:
                interruption = exc
    return ordinary_failure, interruption


class FtpsTlsServer:
    """Serve one exact cleanup or protected passive upload per TLS session."""

    def __init__(  # noqa: PLR0913 -- explicit listener and revocation dependencies.
        self,
        config: GroveBridgeConfig,
        authenticator: CompatibilityAuthenticator,
        staging: FtpsStagingStore,
        admissions: PrinterAdmissionGates,
        *,
        clock_ms: Callable[[], int] | None = None,
        session_registry: CompatibilitySessionRegistry | None = None,
    ) -> None:
        if type(config) is not GroveBridgeConfig or not config.enabled:
            raise ValueError("enabled Grove bridge configuration required")
        if config.tls_certificate_file is None or config.tls_private_key_file is None:
            raise ValueError("complete TLS configuration required")
        if config.ftps_advertised_ipv4 is None:
            raise ValueError("FTPS advertised address required")
        if not config.has_supported_ftps_topology():
            raise ValueError("supported private FTPS topology required")
        self._config = config
        self._certificate = config.tls_certificate_file
        self._private_key = config.tls_private_key_file
        self._advertised_ipv4 = config.ftps_advertised_ipv4
        self._authenticator = authenticator
        self._staging = staging
        self._admissions = admissions
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._session_registry = session_registry
        self._server: asyncio.Server | None = None
        self._owned_servers: set[asyncio.Server] = set()
        self._owned_sockets: set[socket.socket] = set()
        self._passive_endpoints: dict[int, _PassiveEndpoint] = {}
        self._passive_client_tasks: set[asyncio.Task[None]] = set()
        self._passive_client_writers: set[asyncio.StreamWriter] = set()
        self._passive_protocols: set[_PassiveTlsProtocol] = set()
        self._tls_server_context: ssl.SSLContext | None = None
        self._sessions: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._printer_sessions: dict[str, int] = {}
        self._passive_servers: set[asyncio.Server] = set()
        self._transfers = asyncio.Semaphore(config.max_concurrent_transfers)
        self._closing = False
        self._ready = False
        self._lifecycle_lock = asyncio.Lock()

    @property
    def sockets(self) -> tuple[object, ...]:
        if self._server is None or self._server.sockets is None:
            return ()
        return tuple(self._server.sockets)

    async def start(self) -> None:
        """Atomically own the exact control and complete passive listener set."""
        async with self._lifecycle_lock:
            await self._start_locked()

    async def _start_locked(self) -> None:  # noqa: PLR0912, PLR0915 -- explicit transaction.
        """Create every dormant listener before activating the owned set."""
        if self._server is not None or self._owned_servers or self._owned_sockets or self._closing:
            raise RuntimeError("FTPS server is already started or closing")
        if not _valid_owned_socket_config(self._config):
            raise ValueError("supported private FTPS topology required")
        ports = (
            self._config.ftps_control_port,
            *range(
                self._config.ftps_passive_port_min,
                self._config.ftps_passive_port_max + 1,
            ),
        )
        acquired: dict[int, socket.socket] = {}
        try:
            for port in ports:
                owned = _open_owned_socket(self._config.listen_host, port)
                acquired[port] = owned
                self._owned_sockets.add(owned)
        except BaseException as exc:
            interruption = _close_owned_sockets(self._owned_sockets)
            self._closing = bool(self._owned_sockets)
            if interruption is not None:
                raise exc from interruption
            raise
        endpoints: dict[int, _PassiveEndpoint] = {}
        try:
            context = _tls_context(self._certificate, self._private_key)
            self._tls_server_context = context
            loop = asyncio.get_running_loop()
            for port in ports[1:]:
                endpoint = _PassiveEndpoint(port)

                def protocol_factory(
                    *,
                    owned_endpoint: _PassiveEndpoint = endpoint,
                ) -> asyncio.Protocol:
                    accepted_lease = owned_endpoint.lease
                    if (
                        not self._ready
                        or self._closing
                        or accepted_lease is None
                        or accepted_lease.released
                        or len(self._passive_protocols) >= self._config.max_concurrent_transfers
                    ):
                        return _RejectPassiveProtocol()
                    protocol = _PassiveTlsProtocol(
                        self,
                        owned_endpoint,
                        accepted_lease,
                        context,
                    )
                    self._passive_protocols.add(protocol)
                    return protocol

                passive = await loop.create_server(
                    protocol_factory,
                    sock=acquired[port],
                    start_serving=False,
                )
                self._owned_sockets.discard(acquired[port])
                endpoint.server = passive
                endpoints[port] = endpoint
                self._owned_servers.add(passive)
                self._passive_servers.add(passive)
            control = await asyncio.start_server(
                self._accept,
                sock=acquired[ports[0]],
                ssl=context,
                ssl_handshake_timeout=self._config.session_idle_seconds,
                ssl_shutdown_timeout=self._config.shutdown_timeout_seconds,
                limit=_MAX_LINE_BYTES + 1,
                start_serving=False,
            )
            self._owned_sockets.discard(acquired[ports[0]])
            self._owned_servers.add(control)
        except BaseException:
            self._ready = False
            cleanup = asyncio.create_task(self._close_owned_listeners())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
            self._closing = bool(self._owned_servers or self._owned_sockets)
            if not self._closing:
                self._tls_server_context = None
            raise
        self._passive_endpoints = endpoints
        self._server = control
        try:
            for endpoint_item in endpoints.values():
                if endpoint_item.server is None:
                    raise RuntimeError("FTPS passive listener construction was incomplete")
                await endpoint_item.server.start_serving()
            await control.start_serving()
        except BaseException:
            self._ready = False
            cleanup = asyncio.create_task(self._close_owned_listeners())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
            self._server = None
            self._closing = bool(self._owned_servers or self._owned_sockets)
            if not self._closing:
                self._passive_endpoints = {}
                self._tls_server_context = None
            raise
        self._ready = True

    async def close(self) -> None:
        """Stop admission and bound shutdown of control and passive sockets."""
        async with self._lifecycle_lock:
            cleanup = asyncio.create_task(self._close_locked())
            try:
                interruption = await asyncio.shield(cleanup)
            except asyncio.CancelledError as cancellation:
                interruption = await cleanup
                if interruption is not None:
                    raise cancellation from interruption
                raise
            if interruption is not None:
                raise interruption

    async def _close_locked(  # noqa: PLR0912, PLR0915 -- explicit independent phases.
        self,
    ) -> BaseException | None:
        """Attempt every shutdown phase and return the first interruption afterward."""
        self._closing = True
        self._ready = False
        self._server = None
        interruption: BaseException | None = None
        for endpoint in self._passive_endpoints.values():
            if endpoint.lease is not None:
                try:
                    endpoint.lease.release()
                except BaseException as exc:
                    if interruption is None:
                        interruption = exc
        try:
            await self._close_owned_listeners()
        except BaseException as exc:
            if interruption is None:
                interruption = exc
        protocol_tasks: set[asyncio.Task[None]] = set()
        for protocol in tuple(self._passive_protocols):
            try:
                task = protocol.close()
                if task is not None:
                    protocol_tasks.add(task)
            except BaseException as exc:
                if interruption is None:
                    interruption = exc
        writers = tuple(self._writers | self._passive_client_writers)
        for writer in writers:
            try:
                writer.close()
            except BaseException as exc:
                if interruption is None:
                    interruption = exc
        timeout = self._config.shutdown_timeout_seconds
        writer_tasks = [asyncio.create_task(_settle_writer_closed(writer)) for writer in writers]
        writer_done: set[asyncio.Task[BaseException | None]] = set()
        writer_pending: set[asyncio.Task[BaseException | None]] = set(writer_tasks)
        try:
            if writer_pending:
                writer_done, writer_pending = await asyncio.wait(
                    writer_pending,
                    timeout=timeout,
                )
        except BaseException as exc:
            if interruption is None:
                interruption = exc
        for writer_task in writer_pending:
            writer_task.cancel()
            writer_task.add_done_callback(_consume_settle_task)
        for writer_task in writer_tasks:
            if writer_task not in writer_done:
                continue
            try:
                writer_interruption = writer_task.result()
            except BaseException as exc:
                writer_interruption = exc
            if writer_interruption is not None and interruption is None:
                interruption = writer_interruption
        current = asyncio.current_task()
        pending = {
            task
            for task in self._sessions | self._passive_client_tasks | protocol_tasks
            if task is not current and not task.done()
        }
        try:
            if pending:
                _, pending = await asyncio.wait(pending, timeout=timeout / 3)
        except BaseException as exc:
            if interruption is None:
                interruption = exc
        for task in pending:
            try:
                task.cancel()
            except BaseException as exc:
                if interruption is None:
                    interruption = exc
        try:
            if pending:
                await asyncio.wait(pending, timeout=timeout / 3)
        except BaseException as exc:
            if interruption is None:
                interruption = exc
        if not self._owned_servers and not self._owned_sockets:
            self._passive_endpoints = {}
            self._tls_server_context = None
        return interruption

    async def _close_owned_listeners(self) -> None:
        """Settle every listener/raw close despite caller cancellation."""
        cleanup = asyncio.create_task(self._close_owned_listeners_settle())
        try:
            interruption = await asyncio.shield(cleanup)
        except asyncio.CancelledError as cancellation:
            interruption = await cleanup
            if interruption is not None:
                raise cancellation from interruption
            raise
        if interruption is not None:
            raise interruption

    async def _close_owned_listeners_settle(  # noqa: PLR0912 -- independent closes.
        self,
    ) -> BaseException | None:
        """Attempt every owned close and retain ambiguous handles for a later retry."""
        servers = tuple(self._owned_servers | self._passive_servers)
        interruption: BaseException | None = None
        ambiguous: set[asyncio.Server] = set()
        for server in servers:
            try:
                server.close()
            except Exception:
                ambiguous.add(server)
            except BaseException as exc:
                ambiguous.add(server)
                if interruption is None:
                    interruption = exc
            close_clients = getattr(server, "close_clients", None)
            if close_clients is not None:
                try:
                    close_clients()
                except Exception:
                    ambiguous.add(server)
                except BaseException as exc:
                    ambiguous.add(server)
                    if interruption is None:
                        interruption = exc

        async def settle(server: asyncio.Server) -> tuple[bool, BaseException | None]:
            try:
                return (
                    await _wait_server_closed(server, self._config.shutdown_timeout_seconds),
                    None,
                )
            except BaseException as exc:
                return False, exc

        complete = await asyncio.gather(*(settle(server) for server in servers))
        for server, (closed, wait_interruption) in zip(servers, complete, strict=True):
            if wait_interruption is not None and interruption is None:
                interruption = wait_interruption
            if closed and server not in ambiguous:
                self._owned_servers.discard(server)
                self._passive_servers.discard(server)
        socket_interruption = _close_owned_sockets(self._owned_sockets)
        if interruption is not None:
            if socket_interruption is not None:
                interruption.__cause__ = socket_interruption
            return interruption
        return socket_interruption

    async def _close_passive_servers(self) -> None:
        """Compatibility wrapper for retrying all continuously owned listeners."""
        await self._close_owned_listeners()

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if (
            task is None
            or not self._ready
            or self._closing
            or len(self._sessions) >= self._config.max_sessions
        ):
            writer.close()
            await _wait_closed(writer, self._config.shutdown_timeout_seconds)
            return
        self._sessions.add(task)
        self._writers.add(writer)
        try:
            await self._session(reader, writer)
        except (FtpsProtocolError, TimeoutError, ConnectionError, OSError, ValueError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: S110 -- never log peer commands or credentials here.
            pass
        finally:
            self._sessions.discard(task)
            self._writers.discard(writer)
            writer.close()
            await _wait_closed(writer, self._config.shutdown_timeout_seconds)

    async def _session(  # noqa: PLR0911, PLR0912, PLR0915 -- explicit protocol exits.
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = _ipv4(writer.get_extra_info("peername"))
        if peer is None:
            return
        protocol = FtpsProtocolSession(lambda _path: True)
        principal: CompatibilityPrincipal | None = None
        admitted: str | None = None
        registered_session = False
        session_task: asyncio.Task[None] | None = None
        passive: _PassiveLease | asyncio.Server | None = None
        data_slot: _DataConnectionSlot | None = None
        data_writer: asyncio.StreamWriter | None = None
        transfer_slot = False
        commands = 0
        await self._reply(writer, 220, "Klove ready")
        try:
            while not self._closing:
                line = await self._bounded(reader.readuntil(b"\r\n"))
                commands += 1
                if commands > self._config.max_commands_per_session:
                    return
                event = protocol.receive_line(line)
                if event.action is FtpsAction.USERNAME_ACCEPTED:
                    await self._reply(writer, 331, "Password required")
                elif event.action is FtpsAction.PASSWORD_PRESENTED:
                    principal = await self._bounded(
                        self._authenticator.authenticate_ftps(event.argument or "")
                    )
                    if principal is None or not self._admit_printer(principal.printer_uuid):
                        await self._reply(writer, 530, "Authentication failed")
                        return
                    admitted = principal.printer_uuid
                    if self._session_registry is not None:
                        current = asyncio.current_task()
                        if current is None or not self._session_registry.register(
                            principal,
                            current,
                            writer,
                        ):
                            return
                        session_task = current
                        registered_session = True
                    await self._reply(writer, 230, "Authenticated")
                elif principal is None or not await self._bounded(
                    self._authenticator.revalidate(principal)
                ):
                    return
                elif event.action in {
                    FtpsAction.PROTECTION_BUFFER_SET,
                    FtpsAction.PRIVATE_DATA_PROTECTION_SET,
                }:
                    await self._reply(writer, 200, "Accepted")
                elif event.action is FtpsAction.DELETE_REQUESTED:
                    await self._reply(writer, 250, "Accepted")
                elif event.action is FtpsAction.PASSIVE_ENDPOINT_REQUESTED:
                    if self._transfers.locked():
                        return
                    await self._transfers.acquire()
                    transfer_slot = True
                    data_slot = _DataConnectionSlot(asyncio.get_running_loop().create_future())
                    passive = await self._open_passive(peer, data_slot)
                    port = _bound_port(passive)
                    await self._reply(writer, 227, _pasv_reply(self._advertised_ipv4, port))
                elif event.action is FtpsAction.UPLOAD_REQUESTED:
                    passive = cast(asyncio.Server, passive)
                    data_slot = cast(_DataConnectionSlot, data_slot)
                    client_path = cast(str, event.argument)
                    await self._reply(writer, 150, "Opening protected data connection")
                    async with asyncio.timeout(self._config.transfer_timeout_seconds):
                        await data_slot.future
                        data_reader, data_writer, data_peer, protected = data_slot.claim()
                        same_peer = data_peer == peer
                        if not same_peer or not protected:
                            data_writer.close()
                            await _wait_closed(data_writer, self._config.shutdown_timeout_seconds)
                            return
                        await self._receive_stage(data_reader, data_writer, principal, client_path)
                        data_writer.close()
                        await _wait_closed(data_writer, self._config.shutdown_timeout_seconds)
                        data_writer = None
                        data_slot.close()
                        await self._release_passive(passive)
                        passive = None
                        data_slot = None
                    protocol.complete_data_transfer(protected=True, same_peer=True)
                    await self._reply(writer, 226, "Transfer complete")
                else:
                    if event.action is not FtpsAction.SESSION_CLOSED:
                        raise FtpsProtocolError("unexpected FTPS protocol event")
                    await self._reply(writer, 221, "Goodbye")
                    return
        finally:
            if data_writer is not None:
                data_writer.close()
                await _wait_closed(data_writer, self._config.shutdown_timeout_seconds)
            accepted_writer = data_slot.close() if data_slot is not None else None
            if accepted_writer is not None:
                accepted_writer.close()
                await _wait_closed(accepted_writer, self._config.shutdown_timeout_seconds)
            if passive is not None:
                await self._release_passive(passive)
            if transfer_slot:
                self._transfers.release()
            registry = self._session_registry
            if (
                registered_session
                and principal is not None
                and session_task is not None
                and registry is not None
            ):
                registry.unregister(principal, session_task, writer)
            if admitted is not None:
                self._release_printer(admitted)

    async def _open_passive(
        self,
        control_peer: str,
        slot: _DataConnectionSlot,
    ) -> _PassiveLease:
        if not self._ready or self._closing:
            raise OSError("FTPS passive listener set is not owned")
        for port in range(
            self._config.ftps_passive_port_min, self._config.ftps_passive_port_max + 1
        ):
            endpoint = self._passive_endpoints.get(port)
            if endpoint is None or endpoint.server is None or endpoint.lease is not None:
                continue
            lease = _PassiveLease(endpoint, control_peer, slot)
            endpoint.lease = lease
            return lease
        raise OSError("no configured FTPS passive listener lease is available")

    def _schedule_passive_client(
        self,
        endpoint: _PassiveEndpoint,
        accepted_lease: _PassiveLease | None,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Track a post-handshake client before its asynchronous checks begin."""
        self._passive_client_writers.add(writer)
        task = asyncio.create_task(self._accept_passive(endpoint, accepted_lease, reader, writer))
        self._passive_client_tasks.add(task)
        task.add_done_callback(self._passive_client_tasks.discard)

    def _release_passive_protocol(self, protocol: _PassiveTlsProtocol) -> None:
        self._passive_protocols.discard(protocol)

    async def _accept_passive(
        self,
        endpoint: _PassiveEndpoint,
        accepted_lease: _PassiveLease | None,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        lease = accepted_lease
        data_peer = _ipv4(writer.get_extra_info("peername"))
        transferred = False
        try:
            if (
                not self._ready
                or self._closing
                or lease is None
                or lease.released
                or endpoint.lease is not lease
                or data_peer != lease.control_peer
            ):
                return
            protected = writer.get_extra_info("ssl_object") is not None
            if (
                not self._ready
                or self._closing
                or endpoint.lease is not lease
                or lease.released
                or not protected
                or not lease.slot.accept((reader, writer, data_peer, protected))
            ):
                return
            transferred = True
            self._passive_client_writers.discard(writer)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError, TimeoutError, ssl.SSLError):
            pass
        finally:
            if not transferred:
                self._passive_client_writers.discard(writer)
                writer.close()
                await _wait_closed(writer, self._config.shutdown_timeout_seconds)

    async def _release_passive(self, passive: _PassiveLease | asyncio.Server) -> None:
        """Release a runtime lease; close only legacy injected test listeners."""
        if isinstance(passive, _PassiveLease):
            passive.release()
            return
        passive.close()
        if await _wait_server_closed(passive, self._config.shutdown_timeout_seconds):
            self._passive_servers.discard(passive)

    async def _receive_stage(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        principal: CompatibilityPrincipal,
        client_path: str,
    ) -> None:
        now = self._clock_ms()
        reservation = self._staging.create_reservation(
            printer_uuid=principal.printer_uuid,
            client_path=client_path,
            created_at_unix_ms=now,
            expires_at_unix_ms=now + self._config.staging_ttl_seconds * 1000,
        )
        stage = await _blocking(lambda: self._staging.begin(reservation))
        try:
            while chunk := await reader.read(_DATA_CHUNK_BYTES):
                if not await self._authenticator.revalidate(principal):
                    raise ValueError("FTPS principal changed during transfer")
                await _blocking(lambda: stage.write(chunk))
            async with self._admissions.lease(principal.printer_uuid) as lease:
                if not await self._authenticator.revalidate(principal, admission_lease=lease):
                    raise ValueError("FTPS principal changed during transfer")
                await _blocking(stage.commit)
        except BaseException:
            await _blocking(stage.abort)
            raise
        finally:
            writer.close()
            await _wait_closed(writer, self._config.shutdown_timeout_seconds)

    def _admit_printer(self, printer_uuid: str) -> bool:
        count = self._printer_sessions.get(printer_uuid, 0)
        if count >= self._config.max_sessions_per_printer:
            return False
        self._printer_sessions[printer_uuid] = count + 1
        return True

    def _release_printer(self, printer_uuid: str) -> None:
        remaining = self._printer_sessions.get(printer_uuid, 1) - 1
        if remaining:
            self._printer_sessions[printer_uuid] = remaining
        else:
            self._printer_sessions.pop(printer_uuid, None)

    async def _reply(self, writer: asyncio.StreamWriter, code: int, text: str) -> None:
        writer.write(f"{code} {text}\r\n".encode("ascii"))
        await self._bounded(writer.drain())

    async def _bounded(self, operation: Awaitable[_T]) -> _T:
        async with asyncio.timeout(self._config.session_idle_seconds):
            return await operation


def _tls_context(certificate: Path, private_key: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(str(certificate), str(private_key))
    return context


def _ipv4(sockname: object) -> str | None:
    if not isinstance(sockname, tuple) or not sockname or not isinstance(sockname[0], str):
        return None
    try:
        address = ipaddress.ip_address(sockname[0])
    except ValueError:
        return None
    return str(address) if isinstance(address, ipaddress.IPv4Address) else None


def _bound_port(server: asyncio.Server | _PassiveLease) -> int:
    if not server.sockets:
        raise OSError("passive listener has no socket")
    value = cast(socket.socket, server.sockets[0]).getsockname()
    if not isinstance(value, tuple) or len(value) < 2 or type(value[1]) is not int:
        raise OSError("passive listener address is invalid")
    return value[1]


def _pasv_reply(address: str, port: int) -> str:
    high, low = divmod(port, 256)
    return f"Entering Passive Mode ({address.replace('.', ',')},{high},{low})"


async def _wait_closed(writer: asyncio.StreamWriter, close_timeout: float) -> None:
    with contextlib.suppress(ConnectionError, OSError, TimeoutError):
        await asyncio.wait_for(writer.wait_closed(), timeout=close_timeout)


async def _settle_writer_closed(
    writer: asyncio.StreamWriter,
) -> BaseException | None:
    """Return one writer interruption while treating ordinary close failure as settled."""
    try:
        await writer.wait_closed()
    except (ConnectionError, OSError, TimeoutError):
        return None
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        return exc
    return None


def _consume_settle_task(task: asyncio.Task[BaseException | None]) -> None:
    """Consume a bounded-phase task that settled after its caller moved on."""
    with contextlib.suppress(BaseException):
        task.result()


async def _wait_server_closed(server: asyncio.Server, close_timeout: float) -> bool:
    """Bound passive-listener close and report whether closure completed."""
    try:
        await asyncio.wait_for(server.wait_closed(), timeout=close_timeout)
    except (ConnectionError, OSError, TimeoutError):
        return False
    return True


async def _blocking[T](operation: Callable[[], T]) -> T:
    """Let one private-file operation settle before propagating cancellation."""
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
