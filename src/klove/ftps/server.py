"""Bounded implicit-TLS FTPS listener for the accepted Grove upload profile."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import ssl
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar, cast

from klove.config import GroveBridgeConfig
from klove.ftps.protocol import FtpsAction, FtpsProtocolError, FtpsProtocolSession
from klove.ftps.staging import FtpsStagingStore
from klove.orchestration.admission import PrinterAdmissionGates
from klove.security.compatibility import CompatibilityAuthenticator, CompatibilityPrincipal

_MAX_LINE_BYTES = 512
_DATA_CHUNK_BYTES = 64 * 1024
_T = TypeVar("_T")
_DataConnection = tuple[asyncio.StreamReader, asyncio.StreamWriter, str, bool]


class FtpsTlsServer:
    """Serve one exact cleanup or protected passive upload per TLS session."""

    def __init__(
        self,
        config: GroveBridgeConfig,
        authenticator: CompatibilityAuthenticator,
        staging: FtpsStagingStore,
        admissions: PrinterAdmissionGates,
        *,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if type(config) is not GroveBridgeConfig or not config.enabled:
            raise ValueError("enabled Grove bridge configuration required")
        if config.tls_certificate_file is None or config.tls_private_key_file is None:
            raise ValueError("complete TLS configuration required")
        if config.ftps_advertised_ipv4 is None:
            raise ValueError("FTPS advertised address required")
        self._config = config
        self._certificate = config.tls_certificate_file
        self._private_key = config.tls_private_key_file
        self._advertised_ipv4 = config.ftps_advertised_ipv4
        self._authenticator = authenticator
        self._staging = staging
        self._admissions = admissions
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._server: asyncio.Server | None = None
        self._sessions: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._printer_sessions: dict[str, int] = {}
        self._passive_servers: set[asyncio.Server] = set()
        self._transfers = asyncio.Semaphore(config.max_concurrent_transfers)
        self._closing = False

    @property
    def sockets(self) -> tuple[object, ...]:
        if self._server is None or self._server.sockets is None:
            return ()
        return tuple(self._server.sockets)

    async def start(self) -> None:
        """Bind the implicit TLS 1.3 control listener."""
        if self._server is not None or self._closing:
            raise RuntimeError("FTPS server is already started or closing")
        self._server = await asyncio.start_server(
            self._accept,
            self._config.listen_host,
            self._config.ftps_control_port,
            ssl=_tls_context(self._certificate, self._private_key),
            ssl_handshake_timeout=self._config.session_idle_seconds,
            ssl_shutdown_timeout=self._config.shutdown_timeout_seconds,
            limit=_MAX_LINE_BYTES + 1,
        )

    async def close(self) -> None:
        """Stop admission and bound shutdown of control and passive sockets."""
        if self._closing:
            return
        self._closing = True
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for passive in tuple(self._passive_servers):
            passive.close()
        for writer in tuple(self._writers):
            writer.close()
        timeout = self._config.shutdown_timeout_seconds
        waiters = [item.wait_closed() for item in self._passive_servers]
        if server is not None:
            waiters.append(server.wait_closed())
        if waiters:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.gather(*waiters), timeout=timeout / 3)
        current = asyncio.current_task()
        pending = {task for task in self._sessions if task is not current and not task.done()}
        if pending:
            _, pending = await asyncio.wait(pending, timeout=timeout / 3)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=timeout / 3)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is None or self._closing or len(self._sessions) >= self._config.max_sessions:
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
        passive: asyncio.Server | None = None
        data_future: asyncio.Future[_DataConnection] | None = None
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
                    data_future = asyncio.get_running_loop().create_future()
                    passive = await self._open_passive(peer, data_future)
                    port = _bound_port(passive)
                    await self._reply(writer, 227, _pasv_reply(self._advertised_ipv4, port))
                elif event.action is FtpsAction.UPLOAD_REQUESTED:
                    passive = cast(asyncio.Server, passive)
                    data_future = cast(asyncio.Future[_DataConnection], data_future)
                    client_path = cast(str, event.argument)
                    await self._reply(writer, 150, "Opening protected data connection")
                    async with asyncio.timeout(self._config.transfer_timeout_seconds):
                        data_reader, data_writer, data_peer, protected = await data_future
                        passive.close()
                        await passive.wait_closed()
                        self._passive_servers.discard(passive)
                        passive = None
                        same_peer = data_peer == peer
                        if not same_peer or not protected:
                            data_writer.close()
                            await _wait_closed(data_writer, self._config.shutdown_timeout_seconds)
                            return
                        await self._receive_stage(data_reader, data_writer, principal, client_path)
                    protocol.complete_data_transfer(protected=True, same_peer=True)
                    await self._reply(writer, 226, "Transfer complete")
                else:
                    if event.action is not FtpsAction.SESSION_CLOSED:
                        raise FtpsProtocolError("unexpected FTPS protocol event")
                    await self._reply(writer, 221, "Goodbye")
                    return
        finally:
            if passive is not None:
                passive.close()
                await passive.wait_closed()
                self._passive_servers.discard(passive)
            if data_writer is not None:
                data_writer.close()
                await _wait_closed(data_writer, self._config.shutdown_timeout_seconds)
            if transfer_slot:
                self._transfers.release()
            if admitted is not None:
                self._release_printer(admitted)

    async def _open_passive(
        self,
        control_peer: str,
        future: asyncio.Future[_DataConnection],
    ) -> asyncio.Server:
        async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            data_peer = _ipv4(writer.get_extra_info("peername"))
            protected = writer.get_extra_info("ssl_object") is not None
            if future.done() or self._closing or data_peer != control_peer or not protected:
                writer.close()
                await _wait_closed(writer, self._config.shutdown_timeout_seconds)
                return
            future.set_result((reader, writer, data_peer, protected))

        context = _tls_context(self._certificate, self._private_key)
        for port in range(
            self._config.ftps_passive_port_min, self._config.ftps_passive_port_max + 1
        ):
            try:
                server = await asyncio.start_server(
                    accept,
                    self._config.listen_host,
                    port,
                    ssl=context,
                    ssl_handshake_timeout=self._config.transfer_timeout_seconds,
                    ssl_shutdown_timeout=self._config.shutdown_timeout_seconds,
                )
            except OSError:
                continue
            self._passive_servers.add(server)
            return server
        raise OSError("no configured FTPS passive port is available")

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


def _bound_port(server: asyncio.Server) -> int:
    if not server.sockets:
        raise OSError("passive listener has no socket")
    value = server.sockets[0].getsockname()
    if not isinstance(value, tuple) or len(value) < 2 or type(value[1]) is not int:
        raise OSError("passive listener address is invalid")
    return value[1]


def _pasv_reply(address: str, port: int) -> str:
    high, low = divmod(port, 256)
    return f"Entering Passive Mode ({address.replace('.', ',')},{high},{low})"


async def _wait_closed(writer: asyncio.StreamWriter, close_timeout: float) -> None:
    with contextlib.suppress(ConnectionError, OSError, TimeoutError):
        await asyncio.wait_for(writer.wait_closed(), timeout=close_timeout)


async def _blocking[T](operation: Callable[[], T]) -> T:
    """Let one private-file operation settle before propagating cancellation."""
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
