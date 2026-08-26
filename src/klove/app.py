"""Application composition and lifecycle."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import aiohttp
from aiohttp import web

from klove.adapters.moonraker.client import MoonrakerMonitor
from klove.adapters.moonraker.control import MoonrakerControlTransport
from klove.adapters.moonraker.onboarding import EndpointAddressPolicy, MoonrakerOnboardingProbe
from klove.config import AppConfig, GroveBridgeConfig, OnboardingConfig, load_config, read_secret
from klove.domain.onboarding import RegisteredPrinter
from klove.ftps.server import FtpsTlsServer
from klove.ftps.staging import FtpsStagingStore
from klove.northbound.api import create_api, ready_key
from klove.orchestration.admission import PrinterAdmissionGates
from klove.orchestration.bootstrap import FileBootstrapImporter
from klove.orchestration.completion import CompletionHandoffService
from klove.orchestration.control import ControlService
from klove.orchestration.onboarding import CompositeActuatorFenceInspector, PrinterLifecycleService
from klove.orchestration.runtime_registry import RegistryRuntimeSupervisor
from klove.persistence.printer_registry import PrinterStore
from klove.persistence.secret_store import SecretStore
from klove.persistence.start_journal import StartJournal
from klove.registry import PrinterRegistry
from klove.security.auth import BearerAuthenticator
from klove.security.compatibility import CompatibilityAuthenticator
from klove.security.compatibility_sessions import CompatibilitySessionRegistry
from klove.security.frame_handshake import FrameHandshakeStore
from klove.security.owner_sessions import OwnerCredentialAuthenticator, OwnerSessionStore

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _RuntimeControlConfig:
    endpoint: str
    verify_tls: bool


@dataclass(frozen=True, slots=True)
class _CompatibilityBridge:
    sessions: CompatibilitySessionRegistry | None = None
    authenticator: CompatibilityAuthenticator | None = None
    staging: FtpsStagingStore | None = None

    def server(
        self,
        config: GroveBridgeConfig,
        admissions: PrinterAdmissionGates,
    ) -> FtpsTlsServer | None:
        """Construct the listener only for one completely initialized bridge."""
        if self.sessions is None:
            return None
        return FtpsTlsServer(
            config,
            cast(CompatibilityAuthenticator, self.authenticator),
            cast(FtpsStagingStore, self.staging),
            admissions,
            session_registry=self.sessions,
        )


@dataclass(frozen=True, slots=True)
class _CleanupResources:
    app: web.Application
    stop_event: asyncio.Event
    ftps: FtpsTlsServer | None
    compatibility_sessions: CompatibilitySessionRegistry | None
    runtime: RegistryRuntimeSupervisor
    runner: web.AppRunner


async def serve(  # noqa: PLR0915 -- explicit application composition and ordered lifecycle.
    config_path: Path, stop: asyncio.Event | None = None
) -> None:
    """Reconcile durable state, then run registry-backed routes and the API."""
    config = load_config(config_path)
    stop_event = stop or asyncio.Event()
    secrets = SecretStore(config.registry.secret_directory)
    store = PrinterStore(config.registry.database_file, secrets)
    store.initialize()
    start_journal = StartJournal(config.dispatch.journal_file)
    start_journal.initialize()
    registry = PrinterRegistry([])
    admissions = PrinterAdmissionGates()
    bridge = _initialize_compatibility_bridge(config, store, secrets, admissions)
    controls = ControlService(
        registry,
        {},
        confirmation_timeout_seconds=config.control.confirmation_timeout_seconds,
        poll_interval_seconds=config.control.poll_interval_seconds,
        idempotency_capacity=config.control.idempotency_capacity,
        admissions=admissions,
    )
    timeout = aiohttp.ClientTimeout(total=60, connect=10, sock_read=50)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        runtime = RegistryRuntimeSupervisor(
            store,
            secrets,
            registry,
            session,
            admissions=admissions,
            monitor_factory=MoonrakerMonitor,
            controls=controls if config.control.enabled else None,
            control_transport_factory=(
                (
                    lambda record, api_key, active_session: _control_transport(
                        record,
                        api_key,
                        active_session,
                        request_timeout_seconds=config.control.request_timeout_seconds,
                    )
                )
                if config.control.enabled
                else None
            ),
            committed_record_observer=bridge.sessions,
        )
        lifecycle = PrinterLifecycleService(
            store,
            secrets,
            MoonrakerOnboardingProbe(EndpointAddressPolicy(config.registry.allowed_probe_cidrs)),
            CompositeActuatorFenceInspector(controls, start_journal),
            admissions=admissions,
            runtime=runtime,
        )
        scopes = {"printers:read"}
        if config.control.enabled:
            scopes.add("printers:control")
        owner_authenticator, owner_sessions, frame_handshakes = _owner_security(config.onboarding)
        completion_handoff = None
        if frame_handshakes is not None:
            completion_handoff = CompletionHandoffService(
                store,
                secrets,
                cast(str, config.onboarding.compatibility_host),
                admissions,
            )
        app = create_api(
            registry,
            BearerAuthenticator(read_secret(config.api.token_file), scopes=frozenset(scopes)),
            controls,
            owner_authenticator=owner_authenticator,
            owner_sessions=owner_sessions,
            lifecycle=lifecycle if owner_sessions is not None else None,
            frame_handshakes=frame_handshakes,
            completion_handoff=completion_handoff,
        )
        runner = web.AppRunner(app, access_log=None)
        failure: BaseException | None = None
        ftps: FtpsTlsServer | None = None
        try:
            await runner.setup()
            site = web.TCPSite(runner, config.api.listen_host, config.api.listen_port)
            await site.start()
            await FileBootstrapImporter(lifecycle, store).import_all(config.printers)
            ftps = bridge.server(config.grove_bridge, admissions)
            active_printers = await runtime.refresh()
            if ftps is not None:
                await ftps.start()
            app[ready_key].ready = True
            LOGGER.info("klove ready active_printers=%d", len(active_printers))
            await stop_event.wait()
        except BaseException as exc:
            failure = exc
        cleanup = asyncio.create_task(
            _cleanup_runtime(
                _CleanupResources(app, stop_event, ftps, bridge.sessions, runtime, runner)
            ),
            name="klove-runtime-cleanup",
        )
        while True:
            try:
                cleanup_failure = await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError as exc:
                if failure is None:
                    failure = exc
                continue
        if failure is not None:
            if cleanup_failure is not None:
                failure.add_note("runtime cleanup also failed")
            raise failure
        if cleanup_failure is not None:
            raise cleanup_failure


def _initialize_compatibility_bridge(
    config: AppConfig,
    store: PrinterStore,
    secrets: SecretStore,
    admissions: PrinterAdmissionGates,
) -> _CompatibilityBridge:
    """Recover bridge storage without touching it while the gate is disabled."""
    if not config.grove_bridge.enabled:
        return _CompatibilityBridge()
    sessions = CompatibilitySessionRegistry()
    authenticator = CompatibilityAuthenticator(store, secrets, admissions)
    staging = FtpsStagingStore(
        config.grove_bridge.staging_directory,
        limits=config.artifacts,
        capacity=config.grove_bridge.ingress_capacity,
    )
    staging.initialize()
    staging.reconcile()
    return _CompatibilityBridge(
        sessions=sessions,
        authenticator=authenticator,
        staging=staging,
    )


async def _cleanup_runtime(resources: _CleanupResources) -> BaseException | None:
    """Stop every independently owned runtime surface and retain the first failure."""
    resources.app[ready_key].ready = False
    resources.stop_event.set()
    failure: BaseException | None = None
    if resources.ftps is not None:
        for _attempt in range(2):
            try:
                await resources.ftps.close()
            except BaseException as exc:
                if failure is None:
                    failure = exc
    if resources.compatibility_sessions is not None:
        try:
            resources.compatibility_sessions.fence_all()
        except BaseException as exc:
            if failure is None:
                failure = exc
    for operation in (resources.runtime.shutdown, resources.runner.cleanup):
        try:
            await operation()
        except BaseException as exc:
            if failure is None:
                failure = exc
    return failure


def _control_transport(
    record: RegisteredPrinter,
    api_key: str,
    session: aiohttp.ClientSession,
    *,
    request_timeout_seconds: float,
) -> MoonrakerControlTransport:
    return MoonrakerControlTransport(
        _RuntimeControlConfig(
            endpoint=record.endpoint.url,
            verify_tls=record.endpoint.verify_tls,
        ),
        api_key,
        session,
        request_timeout_seconds=request_timeout_seconds,
    )


def _owner_security(
    config: OnboardingConfig,
) -> tuple[
    OwnerCredentialAuthenticator | None,
    OwnerSessionStore | None,
    FrameHandshakeStore | None,
]:
    """Compose independent owner security only when explicitly enabled."""
    if not config.enabled:
        return None, None, None
    credential_file = cast(Path, config.owner_credential_file)
    authenticator = OwnerCredentialAuthenticator(read_secret(credential_file))
    sessions = OwnerSessionStore(
        frozenset(config.allowed_grove_origins),
        capacity=config.session_capacity,
        inactivity_timeout_seconds=config.session_inactivity_seconds,
        absolute_timeout_seconds=config.session_absolute_seconds,
        cookie_secure=config.cookie_secure,
        frame_origin=config.frame_origin,
    )
    handshakes = (
        FrameHandshakeStore(
            frozenset(config.allowed_grove_origins),
            capacity=config.session_capacity,
        )
        if config.frame_enabled
        else None
    )
    return authenticator, sessions, handshakes
