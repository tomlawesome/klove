"""Bounded TLS-only MQTT session runtime for the Grove compatibility facade."""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from collections.abc import Awaitable
from pathlib import Path
from typing import Protocol, TypeVar

from klove.config import GroveBridgeConfig
from klove.domain.translation import DecodedCommand, decode_grove_request_bytes
from klove.northbound.mqtt.codec import (
    MAX_REPORT_BYTES,
    MAX_WIRE_BYTES,
    ConnectPacket,
    DisconnectPacket,
    MqttCodecError,
    MqttPacketParser,
    PingreqPacket,
    PublishPacket,
    SubscribePacket,
    encode_connack,
    encode_pingresp,
    encode_puback,
    encode_report,
    encode_suback,
)
from klove.orchestration.mqtt_control import MqttControlIngress
from klove.security.compatibility import CompatibilityAuthenticator, CompatibilityPrincipal


class MqttReportProvider(Protocol):
    """Return one already-sanitized bounded JSON report for a principal."""

    def report(self, principal: CompatibilityPrincipal) -> Awaitable[bytes]: ...


_T = TypeVar("_T")


class MqttTlsServer:
    """Serve the exact observed Grove MQTT flow without broker semantics."""

    def __init__(
        self,
        config: GroveBridgeConfig,
        authenticator: CompatibilityAuthenticator,
        ingress: MqttControlIngress,
        reports: MqttReportProvider,
    ) -> None:
        if type(config) is not GroveBridgeConfig or not config.enabled:
            raise ValueError("enabled Grove bridge configuration required")
        certificate = config.tls_certificate_file
        private_key = config.tls_private_key_file
        if certificate is None or private_key is None:
            raise ValueError("complete TLS configuration required")
        self._config = config
        self._certificate = certificate
        self._private_key = private_key
        self._authenticator = authenticator
        self._ingress = ingress
        self._reports = reports
        self._server: asyncio.Server | None = None
        self._sessions: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._printer_sessions: dict[str, int] = {}
        self._closing = False

    @property
    def sockets(self) -> tuple[object, ...]:
        """Expose bound sockets for application composition and tests."""
        if self._server is None or self._server.sockets is None:
            return ()
        return tuple(self._server.sockets)

    async def start(self) -> None:
        """Bind one TLS 1.3-only listener."""
        if self._server is not None or self._closing:
            raise RuntimeError("MQTT server is already started or closing")
        context = _tls_context(
            self._certificate,
            self._private_key,
        )
        self._server = await asyncio.start_server(
            self._accept,
            self._config.listen_host,
            self._config.mqtt_port,
            ssl=context,
            ssl_handshake_timeout=self._config.session_idle_seconds,
            ssl_shutdown_timeout=self._config.shutdown_timeout_seconds,
            start_serving=True,
        )

    async def close(self) -> None:
        """Stop admission and close every session within the shutdown bound."""
        if self._closing:
            return
        self._closing = True
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for writer in tuple(self._writers):
            writer.close()
        timeout = self._config.shutdown_timeout_seconds
        grace = timeout / 3
        if server is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(server.wait_closed(), timeout=grace)
        current = asyncio.current_task()
        pending = {task for task in self._sessions if task is not current and not task.done()}
        if pending:
            _, pending = await asyncio.wait(pending, timeout=grace)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=grace)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is None:
            writer.close()
            return
        if self._closing or len(self._sessions) >= self._config.max_sessions:
            writer.close()
            await _wait_closed(writer, self._config.shutdown_timeout_seconds)
            return
        self._sessions.add(task)
        self._writers.add(writer)
        try:
            await self._session(reader, writer)
        except (MqttCodecError, TimeoutError, ConnectionError, OSError, ValueError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: S110 -- protocol failures close without secret-bearing logs.
            pass
        finally:
            self._sessions.discard(task)
            self._writers.discard(writer)
            writer.close()
            await _wait_closed(writer, self._config.shutdown_timeout_seconds)

    async def _session(  # noqa: PLR0911, PLR0912 -- explicit fail-closed state exits.
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        parser = MqttPacketParser()
        principal: CompatibilityPrincipal | None = None
        subscribed = False
        commands = 0
        admitted_printer: str | None = None
        try:
            while not self._closing:
                chunk = await self._bounded(reader.read(MAX_WIRE_BYTES))
                if not chunk:
                    return
                packets = parser.feed(chunk)
                for packet in packets:
                    if principal is None:
                        if type(packet) is not ConnectPacket:
                            return
                        principal = await self._authenticate(packet)
                        if principal is None or not self._admit_printer(principal.printer_uuid):
                            return
                        admitted_printer = principal.printer_uuid
                        await self._write(writer, encode_connack())
                        continue

                    if not await self._bounded(self._authenticator.revalidate(principal)):
                        return
                    if not subscribed:
                        if type(packet) is not SubscribePacket or not _topics_match(
                            packet, principal.proxy_serial
                        ):
                            return
                        subscribed = True
                        await self._write(writer, encode_suback(packet.packet_id))
                        await self._publish_report(writer, principal)
                        continue

                    if type(packet) is PingreqPacket:
                        await self._write(writer, encode_pingresp())
                    elif type(packet) is DisconnectPacket:
                        return
                    elif type(packet) is PublishPacket and packet.topic == (
                        f"device/{principal.proxy_serial}/request"
                    ):
                        if commands >= self._config.max_commands_per_session:
                            return
                        commands += 1
                        await self._command(writer, principal, packet)
                    else:
                        return
        finally:
            if admitted_printer is not None:
                remaining = self._printer_sessions.get(admitted_printer, 1) - 1
                if remaining > 0:
                    self._printer_sessions[admitted_printer] = remaining
                else:
                    self._printer_sessions.pop(admitted_printer, None)

    async def _authenticate(
        self, packet: ConnectPacket
    ) -> CompatibilityPrincipal | None:
        return await self._bounded(
            self._authenticator.authenticate_mqtt(
                packet.serial,
                packet.credentials.password,
            )
        )

    def _admit_printer(self, printer_uuid: str) -> bool:
        count = self._printer_sessions.get(printer_uuid, 0)
        if count >= self._config.max_sessions_per_printer:
            return False
        self._printer_sessions[printer_uuid] = count + 1
        return True

    async def _command(
        self,
        writer: asyncio.StreamWriter,
        principal: CompatibilityPrincipal,
        packet: PublishPacket,
    ) -> None:
        decoded = decode_grove_request_bytes(packet.payload)
        if type(decoded) is DecodedCommand:
            await self._bounded(self._ingress.execute(principal, decoded))
            await self._publish_report(writer, principal)
        await self._write(writer, encode_puback(packet.packet_id))

    async def _publish_report(
        self, writer: asyncio.StreamWriter, principal: CompatibilityPrincipal
    ) -> None:
        payload = await self._bounded(self._reports.report(principal))
        if type(payload) is not bytes or len(payload) > MAX_REPORT_BYTES:
            raise ValueError("invalid MQTT report")
        await self._write(writer, encode_report(principal.proxy_serial, payload))

    async def _write(self, writer: asyncio.StreamWriter, payload: bytes) -> None:
        writer.write(payload)
        await self._bounded(writer.drain())

    async def _bounded(self, operation: Awaitable[_T]) -> _T:
        async with asyncio.timeout(self._config.session_idle_seconds):
            return await operation


def _topics_match(packet: SubscribePacket, serial: str) -> bool:
    return set(packet.topics) == {
        f"device/{serial}/report",
        f"device/{serial}/request",
    }


def _tls_context(certificate: Path, private_key: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(str(certificate), str(private_key))
    return context


async def _wait_closed(writer: asyncio.StreamWriter, timeout_seconds: float) -> None:
    with contextlib.suppress(ConnectionError, OSError, TimeoutError):
        async with asyncio.timeout(timeout_seconds):
            await writer.wait_closed()
