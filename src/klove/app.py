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
from klove.config import OnboardingConfig, load_config, read_secret
from klove.domain.onboarding import RegisteredPrinter
from klove.northbound.api import create_api, ready_key
from klove.orchestration.admission import PrinterAdmissionGates
from klove.orchestration.bootstrap import FileBootstrapImporter
from klove.orchestration.control import ControlService
from klove.orchestration.onboarding import CompositeActuatorFenceInspector, PrinterLifecycleService
from klove.orchestration.runtime_registry import RegistryRuntimeSupervisor
from klove.persistence.printer_registry import PrinterStore
from klove.persistence.secret_store import SecretStore
from klove.persistence.start_journal import StartJournal
from klove.registry import PrinterRegistry
from klove.security.auth import BearerAuthenticator
from klove.security.owner_sessions import OwnerCredentialAuthenticator, OwnerSessionStore

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _RuntimeControlConfig:
    endpoint: str
    verify_tls: bool


async def serve(config_path: Path, stop: asyncio.Event | None = None) -> None:
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
        )
        lifecycle = PrinterLifecycleService(
            store,
            secrets,
            MoonrakerOnboardingProbe(EndpointAddressPolicy(config.registry.allowed_probe_cidrs)),
            CompositeActuatorFenceInspector(controls, start_journal),
            admissions=admissions,
            runtime=runtime,
        )
        await FileBootstrapImporter(lifecycle, store).import_all(config.printers)
        scopes = {"printers:read"}
        if config.control.enabled:
            scopes.add("printers:control")
        owner_authenticator, owner_sessions = _owner_security(config.onboarding)
        app = create_api(
            registry,
            BearerAuthenticator(read_secret(config.api.token_file), scopes=frozenset(scopes)),
            controls,
            owner_authenticator=owner_authenticator,
            owner_sessions=owner_sessions,
            lifecycle=lifecycle if owner_sessions is not None else None,
        )
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, config.api.listen_host, config.api.listen_port)
        try:
            await site.start()
            active_printers = await runtime.refresh()
            app[ready_key].ready = True
            LOGGER.info("klove ready active_printers=%d", len(active_printers))
            await stop_event.wait()
        finally:
            app[ready_key].ready = False
            stop_event.set()
            await runtime.shutdown()
            await runner.cleanup()


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
) -> tuple[OwnerCredentialAuthenticator | None, OwnerSessionStore | None]:
    """Compose independent owner security only when explicitly enabled."""
    if not config.enabled:
        return None, None
    credential_file = cast(Path, config.owner_credential_file)
    authenticator = OwnerCredentialAuthenticator(read_secret(credential_file))
    sessions = OwnerSessionStore(
        frozenset(config.allowed_grove_origins),
        capacity=config.session_capacity,
        inactivity_timeout_seconds=config.session_inactivity_seconds,
        absolute_timeout_seconds=config.session_absolute_seconds,
        cookie_secure=config.cookie_secure,
    )
    return authenticator, sessions
