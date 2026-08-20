"""Dynamic fail-closed activation of canonical registry printers."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Protocol

import aiohttp

from klove.adapters.moonraker.client import MoonrakerMonitor, MoonrakerMonitorConfig
from klove.domain.onboarding import PrinterLifecycle, RegisteredPrinter
from klove.errors import KloveError
from klove.orchestration.admission import PrinterAdmissionGates
from klove.persistence.printer_registry import PrinterStore
from klove.persistence.secret_store import SecretStore
from klove.registry import PrinterRegistry

LOGGER = logging.getLogger(__name__)
_COMPATIBILITY_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20}$")


class RegistryRuntimeError(KloveError):
    """The canonical registry cannot safely drive the runtime."""


class _Monitor(Protocol):
    async def run(self, stop: asyncio.Event) -> None:
        """Run until stopped or failed."""


MonitorFactory = Callable[
    [MoonrakerMonitorConfig, str, PrinterRegistry, aiohttp.ClientSession],
    _Monitor,
]


@dataclass(frozen=True, slots=True)
class _RuntimeMonitorConfig:
    id: str
    endpoint: str
    verify_tls: bool


@dataclass(frozen=True, slots=True)
class _DesiredMonitor:
    record: RegisteredPrinter
    api_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _ActiveMonitor:
    record: RegisteredPrinter
    api_key: str = field(repr=False)
    stop: asyncio.Event
    task: asyncio.Task[None]


class RegistryRuntimeSupervisor:
    """Reconcile exact active registry records into isolated monitor tasks."""

    def __init__(  # noqa: PLR0913 -- explicit runtime and shared admission dependencies.
        self,
        store: PrinterStore,
        secrets: SecretStore,
        registry: PrinterRegistry,
        session: aiohttp.ClientSession,
        *,
        admissions: PrinterAdmissionGates,
        monitor_factory: MonitorFactory = MoonrakerMonitor,
    ) -> None:
        self._store = store
        self._secrets = secrets
        self._registry = registry
        self._session = session
        self._monitor_factory = monitor_factory
        self._admissions = admissions
        self._active: dict[str, _ActiveMonitor] = {}
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._closed = False

    async def refresh(self) -> tuple[str, ...]:
        """Load durable desired state and reconcile every runtime route."""
        operation = asyncio.create_task(self._refresh(), name="registry-runtime-refresh")
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            with suppress(Exception):
                await operation
            raise

    async def shutdown(self) -> None:
        """Stop every monitor and remove every route, even if the caller is cancelled."""
        operation = asyncio.create_task(self._shutdown(), name="registry-runtime-shutdown")
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            with suppress(Exception):
                await operation
            raise

    async def _refresh(self) -> tuple[str, ...]:
        async with self._lock:
            if self._closed:
                raise RegistryRuntimeError
            try:
                records = self._store.list(include_removed=True)
            except Exception as exc:
                await self._deactivate_all()
                await self._remove_unmanaged_routes()
                raise RegistryRuntimeError from exc
            desired = self._desired(records)
            for printer_id in tuple(self._active):
                async with self._admissions.hold(printer_id):
                    active = self._active[printer_id]
                    replacement = desired.get(printer_id)
                    if replacement is not None and not self._candidate_is_current(replacement):
                        replacement = None
                    if (
                        replacement is None
                        or active.task.done()
                        or active.record != replacement.record
                        or active.api_key != replacement.api_key
                    ):
                        await self._deactivate(printer_id)
            await self._remove_unmanaged_routes()
            for printer_id, candidate in desired.items():
                async with self._admissions.hold(printer_id):
                    if printer_id not in self._active and self._candidate_is_current(candidate):
                        await self._activate(candidate)
            return tuple(sorted(self._active))

    async def _shutdown(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            await self._deactivate_all()
            await self._remove_unmanaged_routes()
        if self._cleanup_tasks:
            await asyncio.gather(*tuple(self._cleanup_tasks), return_exceptions=True)

    def _desired(self, records: tuple[RegisteredPrinter, ...]) -> dict[str, _DesiredMonitor]:
        valid_records = [record for record in records if type(record) is RegisteredPrinter]
        colliding = _colliding_indexes(valid_records)
        desired: dict[str, _DesiredMonitor] = {}
        for index, record in enumerate(valid_records):
            if index in colliding or record.lifecycle is not PrinterLifecycle.ACTIVE:
                continue
            moonraker_ref = record.moonraker_credential_ref
            compatibility_ref = record.compatibility_credential_ref
            if moonraker_ref is None or compatibility_ref is None:
                continue
            try:
                api_key = self._secrets.read(moonraker_ref, minimum_length=32)
                compatibility_secret = self._secrets.read(compatibility_ref, minimum_length=20)
                if _COMPATIBILITY_SECRET_PATTERN.fullmatch(compatibility_secret) is None:
                    raise ValueError
            except Exception:
                LOGGER.warning("registry runtime denied unreadable printer=%s", record.printer_uuid)
                continue
            desired[record.printer_uuid] = _DesiredMonitor(record=record, api_key=api_key)
        return desired

    async def _activate(self, candidate: _DesiredMonitor) -> None:
        printer_id = candidate.record.printer_uuid
        config = _RuntimeMonitorConfig(
            id=printer_id,
            endpoint=candidate.record.endpoint.url,
            verify_tls=candidate.record.endpoint.verify_tls,
        )
        await self._registry.register(printer_id)
        try:
            monitor = self._monitor_factory(
                config,
                candidate.api_key,
                self._registry,
                self._session,
            )
            stop = asyncio.Event()
            task = asyncio.create_task(
                self._run_monitor(printer_id, monitor, stop),
                name=f"moonraker:{printer_id}",
            )
        except Exception:
            await self._registry.unregister(printer_id)
            LOGGER.error("registry runtime could not activate printer=%s", printer_id)
            return
        self._active[printer_id] = _ActiveMonitor(
            record=candidate.record,
            api_key=candidate.api_key,
            stop=stop,
            task=task,
        )
        task.add_done_callback(lambda completed: self._schedule_cleanup(printer_id, completed))

    async def _deactivate(self, printer_id: str) -> None:
        active = self._active.pop(printer_id)
        active.stop.set()
        active.task.cancel()
        await asyncio.gather(active.task, return_exceptions=True)
        await self._registry.unregister(printer_id)

    async def _deactivate_all(self) -> None:
        for printer_id in tuple(self._active):
            async with self._admissions.hold(printer_id):
                await self._deactivate(printer_id)

    async def _remove_unmanaged_routes(self) -> None:
        for snapshot in await self._registry.list():
            async with self._admissions.hold(snapshot.printer_id):
                if snapshot.printer_id not in self._active:
                    await self._registry.unregister(snapshot.printer_id)

    def _candidate_is_current(self, candidate: _DesiredMonitor) -> bool:
        try:
            return self._store.get(candidate.record.printer_uuid) == candidate.record
        except Exception:
            return False

    async def _run_monitor(
        self,
        printer_id: str,
        monitor: _Monitor,
        stop: asyncio.Event,
    ) -> None:
        try:
            await monitor.run(stop)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.error("registry runtime monitor failed printer=%s", printer_id)

    def _schedule_cleanup(self, printer_id: str, completed: asyncio.Task[None]) -> None:
        cleanup = asyncio.create_task(
            self._cleanup_finished(printer_id, completed),
            name=f"moonraker-cleanup:{printer_id}",
        )
        self._cleanup_tasks.add(cleanup)
        cleanup.add_done_callback(self._cleanup_tasks.discard)

    async def _cleanup_finished(
        self,
        printer_id: str,
        completed: asyncio.Task[None],
    ) -> None:
        async with self._lock, self._admissions.hold(printer_id):
            active = self._active.get(printer_id)
            if active is None or active.task is not completed:
                return
            self._active.pop(printer_id)
            await self._registry.unregister(printer_id)


def _colliding_indexes(records: list[RegisteredPrinter]) -> set[int]:
    keys: list[tuple[str, ...]] = []
    for record in records:
        references = tuple(
            reference
            for reference in (
                record.moonraker_credential_ref,
                record.compatibility_credential_ref,
            )
            if reference is not None
        )
        keys.append((record.printer_uuid, record.endpoint.url, *references))
    counts = Counter(key for record_keys in keys for key in record_keys)
    return {
        index
        for index, record_keys in enumerate(keys)
        if any(counts[key] > 1 for key in record_keys)
    }
