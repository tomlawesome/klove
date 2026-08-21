"""Idempotent job-control dispatch and post-action reconciliation."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Protocol

from klove.domain.control import (
    ControlIntent,
    ControlOperation,
    ControlResult,
    ControlStatus,
    LiveControlState,
    ReconciliationDecision,
    authorize_cached_intent,
    reconcile_postcondition,
    validate_cached_recheck,
    validate_live_preflight,
)
from klove.errors import ControlTransportError
from klove.orchestration.admission import PrinterAdmissionGates
from klove.registry import PrinterRegistry


class ControlTransport(Protocol):
    """Narrow replaceable boundary around one printer's control transport."""

    async def query(self) -> LiveControlState: ...

    async def dispatch(self, operation: ControlOperation) -> None: ...


class ControlService:
    """Serialize controls per printer and deduplicate them per process epoch."""

    def __init__(  # noqa: PLR0913 -- explicit policy and shared admission dependencies.
        self,
        registry: PrinterRegistry,
        transports: dict[str, ControlTransport],
        *,
        confirmation_timeout_seconds: float,
        poll_interval_seconds: float,
        idempotency_capacity: int,
        admissions: PrinterAdmissionGates,
    ) -> None:
        self._registry = registry
        self._transports = dict(transports)
        self._confirmation_timeout = confirmation_timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._capacity = idempotency_capacity
        self._journal: OrderedDict[str, tuple[ControlIntent, asyncio.Task[ControlResult]]] = (
            OrderedDict()
        )
        self._journal_lock = asyncio.Lock()
        self._admissions = admissions
        self._uncertain_tokens: set[tuple[str, str]] = set()

    def register_transport(self, printer_id: str, transport: ControlTransport) -> None:
        """Install one canonical runtime route while its shared gate is held."""
        if printer_id in self._transports:
            raise KeyError("control transport is already registered")
        self._transports[printer_id] = transport

    def unregister_transport(self, printer_id: str) -> bool:
        """Remove one canonical runtime route while its shared gate is held."""
        return self._transports.pop(printer_id, None) is not None

    async def execute(self, intent: ControlIntent) -> ControlResult:
        """Execute an intent once; duplicates await and reuse the original result."""
        async with self._journal_lock:
            existing = self._journal.get(intent.idempotency_key)
            if existing is not None:
                previous_intent, task = existing
                if previous_intent != intent:
                    return _denied(intent.operation, "idempotency_conflict")
                self._journal.move_to_end(intent.idempotency_key)
            else:
                if len(self._journal) >= self._capacity and not self._evict_one():
                    return _denied(intent.operation, "idempotency_capacity")
                task = asyncio.create_task(self._execute_safely(intent))
                self._journal[intent.idempotency_key] = (intent, task)
        return await asyncio.shield(task)

    async def has_unresolved(self, printer_id: str) -> bool:
        """Report in-flight or outcome-unknown control evidence for one printer."""
        async with self._journal_lock:
            for intent, task in self._journal.values():
                if intent.printer_id != printer_id:
                    continue
                if not task.done() or task.cancelled():
                    return True
                if task.result().status is ControlStatus.OUTCOME_UNKNOWN:
                    return True
            return any(candidate == printer_id for candidate, _token in self._uncertain_tokens)

    def _evict_one(self) -> bool:
        for key, (_intent, task) in self._journal.items():
            if not task.done() or task.cancelled():
                continue
            result = task.result()
            if result.status is ControlStatus.DENIED:
                del self._journal[key]
                return True
        return False

    async def _execute_safely(self, intent: ControlIntent) -> ControlResult:
        try:
            return await self._execute_once(intent)
        except Exception:
            return _unknown(intent.operation)

    async def _execute_once(  # noqa: PLR0911 -- fail-closed exits precede dispatch.
        self, intent: ControlIntent
    ) -> ControlResult:
        async with self._admissions.hold(intent.printer_id):
            transport = self._transports.get(intent.printer_id)
            if transport is None:
                return _denied(intent.operation, "control_disabled")
            uncertainty_key = (intent.printer_id, intent.state_token)
            if uncertainty_key in self._uncertain_tokens:
                return _unknown(intent.operation)
            cached = authorize_cached_intent(intent, await self._registry.get(intent.printer_id))
            if isinstance(cached, str):
                return _denied(intent.operation, cached)
            try:
                preflight = await transport.query()
            except ControlTransportError:
                return _denied(intent.operation, "preflight_unavailable")
            mismatch = validate_live_preflight(cached, preflight)
            if mismatch is not None:
                return _denied(intent.operation, mismatch)

            latest_mismatch = validate_cached_recheck(
                intent,
                cached,
                await self._registry.get(intent.printer_id),
                preflight=preflight,
            )
            if latest_mismatch is not None:
                return _denied(intent.operation, latest_mismatch)

            self._uncertain_tokens.add(uncertainty_key)
            try:
                await transport.dispatch(intent.operation)
            except ControlTransportError:
                return _unknown(intent.operation)
            result = await self._confirm(intent.operation, transport, preflight)
            if result.status is ControlStatus.CONFIRMED:
                self._uncertain_tokens.remove(uncertainty_key)
            return result

    async def _confirm(
        self,
        operation: ControlOperation,
        transport: ControlTransport,
        preflight: LiveControlState,
    ) -> ControlResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._confirmation_timeout
        while True:
            try:
                live = await transport.query()
            except ControlTransportError:
                return _unknown(operation)
            decision = reconcile_postcondition(operation, live, preflight=preflight)
            if decision is ReconciliationDecision.CONFIRMED:
                return ControlResult(
                    operation=operation, status=ControlStatus.CONFIRMED, code="confirmed"
                )
            if decision is ReconciliationDecision.AMBIGUOUS:
                return _unknown(operation)
            remaining = deadline - loop.time()
            if remaining <= 0:
                return _unknown(operation)
            await asyncio.sleep(min(self._poll_interval, remaining))


def _denied(operation: ControlOperation, code: str) -> ControlResult:
    return ControlResult(operation=operation, status=ControlStatus.DENIED, code=code)


def _unknown(operation: ControlOperation) -> ControlResult:
    return ControlResult(
        operation=operation,
        status=ControlStatus.OUTCOME_UNKNOWN,
        code="outcome_unknown",
    )
