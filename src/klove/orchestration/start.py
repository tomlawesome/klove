"""Durable at-most-once print start and restart/reconnect reconciliation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

from klove.config import PrinterConfig
from klove.domain.artifacts import SafetyProfile, target_for_safety_profile
from klove.domain.models import PrinterPhase
from klove.domain.start import (
    StartBoundary,
    StartFailure,
    StartFailureCode,
    StartJournalRecord,
    StartJournalState,
    StartObservation,
    StartOperationResult,
    StartPreflight,
    StartReconciliationDecision,
    StartState,
    reconcile_start,
)
from klove.domain.upload import MoonrakerGcodeMetadata, RemoteFileDigest, VerifiedUpload
from klove.errors import JournalError, StartTransportError
from klove.orchestration.admission import PrinterAdmissionGates, PrinterAdmissionLease
from klove.persistence.start_journal import (
    JournalConflictError,
    JournalFenceError,
    StartJournal,
)


class StartTransport(Protocol):
    """Narrow exact-file and typed-start boundary for one Moonraker endpoint."""

    async def metadata(self, path: str) -> MoonrakerGcodeMetadata | None: ...

    async def download(self, path: str, expected_size: int) -> RemoteFileDigest: ...

    async def query(self) -> StartObservation: ...

    async def dispatch(self, path: str) -> None: ...


@dataclass(frozen=True, slots=True)
class _StartRequest:
    verified: VerifiedUpload


@dataclass(frozen=True, slots=True)
class _TaskEntry:
    request: _StartRequest
    task: asyncio.Task[StartOperationResult]
    admission_lease: PrinterAdmissionLease | None


class StartService:
    """Fence one printer around a durable reservation and one start dispatch."""

    def __init__(  # noqa: PLR0913 -- explicit durable service dependencies.
        self,
        printer: PrinterConfig,
        transport: StartTransport,
        journal: StartJournal,
        *,
        enabled: bool,
        confirmation_timeout_seconds: float,
        poll_interval_seconds: float,
        admissions: PrinterAdmissionGates,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._printer = printer
        self._transport = transport
        self._journal = journal
        self._enabled = enabled and printer.dispatch_enabled
        self._confirmation_timeout = confirmation_timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._clock = clock
        self._sleep = sleep
        self._ready = False
        self._journal_available = True
        self._admissions = admissions
        self._task_lock = asyncio.Lock()
        self._by_key: dict[str, _TaskEntry] = {}
        self._by_operation: dict[str, _TaskEntry] = {}

    async def initialize(self) -> None:
        """Validate storage and reconcile every uncertain operation before dispatch."""
        try:
            self._journal.initialize()
        except JournalError:
            self._journal_available = False
            self._ready = False
            return
        self._journal_available = True
        await self.reconcile_after_reconnect()

    async def reconcile_after_reconnect(self) -> None:
        """Close the dispatch gate while durable unresolved rows are re-observed."""
        self._ready = False
        if not self._journal_is_available():
            return
        async with self._admissions.hold(str(self._printer.uuid)):
            try:
                records = self._journal.unresolved(self._printer.uuid)
            except JournalError:
                self._journal_available = False
                return
            for record in records:
                await self._confirm(record)
                if not self._journal_is_available():
                    return
            self._ready = True

    async def execute(
        self,
        verified: VerifiedUpload,
        *,
        admission_lease: PrinterAdmissionLease | None = None,
    ) -> StartOperationResult:
        """Start an exact verified upload at most once across callers and restarts."""
        request = _StartRequest(verified)
        operation_id = verified.qualification.operation_id
        idempotency_key = verified.qualification.idempotency_key
        async with self._task_lock:
            keyed = self._by_key.get(idempotency_key)
            operated = self._by_operation.get(operation_id)
            existing = keyed or operated
            if existing is not None:
                if keyed is not existing or operated is not existing or existing.request != request:
                    return _denied(verified, StartFailureCode.IDEMPOTENCY_CONFLICT)
                if (
                    admission_lease is not None
                    and existing.admission_lease is None
                    and not existing.task.done()
                ):
                    return _denied(verified, StartFailureCode.IDEMPOTENCY_CONFLICT)
                task = existing.task
            else:
                task = asyncio.create_task(self._execute_safely(verified, admission_lease))
                entry = _TaskEntry(request, task, admission_lease)
                self._by_key[idempotency_key] = entry
                self._by_operation[operation_id] = entry
                task.add_done_callback(
                    lambda completed: self._forget_task(operation_id, idempotency_key, completed)
                )
        return await asyncio.shield(task)

    async def reconcile(
        self,
        verified: VerifiedUpload,
        *,
        admission_lease: PrinterAdmissionLease | None = None,
    ) -> StartOperationResult:
        """Read-only reconcile one durable start row without issuing another RPC."""
        if not self._journal_is_available():
            return _denied(verified, StartFailureCode.JOURNAL_UNAVAILABLE)
        async with self._admissions.hold(str(self._printer.uuid), lease=admission_lease):
            try:
                record = self._journal.lookup(verified)
            except JournalConflictError:
                return _denied(verified, StartFailureCode.IDEMPOTENCY_CONFLICT)
            except JournalError:
                self._journal_available = False
                return _denied(verified, StartFailureCode.JOURNAL_UNAVAILABLE)
            if record is None:
                return _denied(verified, StartFailureCode.RECONCILIATION_PENDING)
            if record.state is StartJournalState.CONFIRMED:
                return _result_for_record(record)
            return await self._confirm(record)

    async def observe(
        self,
        verified: VerifiedUpload,
        *,
        admission_lease: PrinterAdmissionLease | None = None,
    ) -> StartObservation | None:
        """Read one bounded current/history observation without changing start state."""
        async with self._admissions.hold(str(self._printer.uuid), lease=admission_lease):
            if self._current_profile(verified) is None:
                return None
            try:
                return await self._transport.query()
            except StartTransportError:
                return None

    def _forget_task(
        self,
        operation_id: str,
        idempotency_key: str,
        completed: asyncio.Task[StartOperationResult],
    ) -> None:
        """Forget only the matching finished task, never a newer reservation."""
        entry = self._by_key.get(idempotency_key)
        if entry is not None and entry.task is completed:
            self._by_key.pop(idempotency_key, None)
            self._by_operation.pop(operation_id, None)

    async def _execute_safely(
        self,
        verified: VerifiedUpload,
        admission_lease: PrinterAdmissionLease | None,
    ) -> StartOperationResult:
        try:
            return await self._execute_once(verified, admission_lease)
        except asyncio.CancelledError:
            return _denied(verified, StartFailureCode.INTERNAL_FAILURE)
        except Exception:
            return _denied(verified, StartFailureCode.INTERNAL_FAILURE)

    async def _execute_once(  # noqa: PLR0911, PLR0912 -- explicit safety gates.
        self,
        verified: VerifiedUpload,
        admission_lease: PrinterAdmissionLease | None,
    ) -> StartOperationResult:
        if not self._enabled:
            return _denied(verified, StartFailureCode.DISPATCH_DISABLED)
        if not self._journal_is_available():
            return _denied(verified, StartFailureCode.JOURNAL_UNAVAILABLE)
        if not self._dispatch_ready():
            return _denied(verified, StartFailureCode.RECONCILIATION_PENDING)

        async with self._admissions.hold(str(self._printer.uuid), lease=admission_lease):
            if not self._dispatch_ready():
                return _denied(verified, StartFailureCode.RECONCILIATION_PENDING)
            try:
                existing = self._journal.lookup(verified)
            except JournalConflictError:
                return _denied(verified, StartFailureCode.IDEMPOTENCY_CONFLICT)
            except JournalError:
                self._journal_available = False
                return _denied(verified, StartFailureCode.JOURNAL_UNAVAILABLE)
            if existing is not None:
                return _result_for_record(existing)
            try:
                if self._journal.unresolved(self._printer.uuid):
                    return _unknown(
                        verified,
                        StartBoundary.RECONCILIATION,
                        StartFailureCode.PRINTER_FENCED,
                    )
            except JournalError:
                self._journal_available = False
                return _denied(verified, StartFailureCode.JOURNAL_UNAVAILABLE)

            if self._current_profile(verified) is None:
                return _denied(verified, StartFailureCode.TARGET_STALE)
            if not await self._remote_matches(verified):
                return _denied(verified, StartFailureCode.REMOTE_MISMATCH)
            try:
                observation = await self._transport.query()
            except StartTransportError:
                return _denied(verified, StartFailureCode.PRINTER_NOT_IDLE)
            if observation.phase is not PrinterPhase.IDLE or (
                observation.latest_job is not None
                and observation.latest_job.filename == verified.path
            ):
                return _denied(verified, StartFailureCode.PRINTER_NOT_IDLE)
            preflight = StartPreflight(observation=observation)
            pending = StartJournalRecord(
                operation_id=verified.qualification.operation_id,
                idempotency_key=verified.qualification.idempotency_key,
                printer_uuid=verified.qualification.target.printer_uuid,
                verified=verified,
                preflight=preflight,
                state=StartJournalState.DISPATCHING,
            )
            try:
                record, created = self._journal.reserve(pending)
            except JournalFenceError:
                return _unknown(
                    verified,
                    StartBoundary.RECONCILIATION,
                    StartFailureCode.PRINTER_FENCED,
                )
            except JournalConflictError:
                return _denied(verified, StartFailureCode.IDEMPOTENCY_CONFLICT)
            except JournalError:
                self._journal_available = False
                return _denied(verified, StartFailureCode.JOURNAL_UNAVAILABLE)
            if not created:
                return _result_for_record(record)

            try:
                with suppress(StartTransportError):
                    await self._transport.dispatch(verified.path)
                return await self._confirm(record)
            except asyncio.CancelledError:
                return _unknown(
                    verified,
                    StartBoundary.TRANSPORT,
                    StartFailureCode.TRANSPORT_AMBIGUOUS,
                )
            except Exception:
                return _unknown(
                    verified,
                    StartBoundary.INTERNAL,
                    StartFailureCode.INTERNAL_FAILURE,
                )

    def _current_profile(self, verified: VerifiedUpload) -> SafetyProfile | None:
        target = verified.qualification.target
        if target.printer_uuid != self._printer.uuid:
            return None
        profiles = tuple(
            profile
            for profile in self._printer.safety_profiles
            if profile.slicer_profile_id == target.slicer_profile_id
        )
        if len(profiles) != 1 or target_for_safety_profile(profiles[0]) != target:
            return None
        return profiles[0]

    def _journal_is_available(self) -> bool:
        return self._journal_available

    def _dispatch_ready(self) -> bool:
        return self._ready

    async def _remote_matches(
        self,
        verified: VerifiedUpload,
    ) -> bool:
        expected_size = verified.qualification.selected_plate.gcode_size_bytes
        try:
            before = await self._transport.metadata(verified.path)
            if before != verified.metadata:
                return False
            remote = await self._transport.download(verified.path, expected_size)
            if remote != verified.remote_file:
                return False
            after = await self._transport.metadata(verified.path)
            return after == before
        except StartTransportError:
            return False

    async def _confirm(self, record: StartJournalRecord) -> StartOperationResult:
        deadline = self._clock() + self._confirmation_timeout
        transport_failed = False
        while True:
            try:
                observation = await self._transport.query()
            except StartTransportError:
                transport_failed = True
            else:
                decision, confirmation = reconcile_start(
                    record.verified,
                    record.preflight,
                    observation,
                )
                if decision is StartReconciliationDecision.CONFIRMED:
                    if confirmation is None:
                        return self._persist_unknown(
                            record,
                            StartBoundary.JOURNAL,
                            StartFailureCode.JOURNAL_UNAVAILABLE,
                        )
                    try:
                        confirmed = self._journal.mark_confirmed(record, confirmation)
                    except JournalError:
                        self._journal_available = False
                        return _unknown(
                            record.verified,
                            StartBoundary.JOURNAL,
                            StartFailureCode.JOURNAL_UNAVAILABLE,
                        )
                    return _result_for_record(confirmed)
                if decision is StartReconciliationDecision.AMBIGUOUS:
                    return self._persist_unknown(
                        record,
                        StartBoundary.TRANSPORT,
                        StartFailureCode.TRANSPORT_AMBIGUOUS,
                    )
            remaining = deadline - self._clock()
            if remaining <= 0:
                return self._persist_unknown(
                    record,
                    StartBoundary.TRANSPORT,
                    StartFailureCode.TRANSPORT_AMBIGUOUS
                    if transport_failed
                    else StartFailureCode.CONFIRMATION_TIMEOUT,
                )
            await self._sleep(min(self._poll_interval, remaining))

    def _persist_unknown(
        self,
        record: StartJournalRecord,
        boundary: StartBoundary,
        code: StartFailureCode,
    ) -> StartOperationResult:
        failure = StartFailure(boundary=boundary, code=code)
        try:
            changed = self._journal.mark_unknown(record, failure)
        except JournalError:
            self._journal_available = False
            return _unknown(
                record.verified,
                StartBoundary.JOURNAL,
                StartFailureCode.JOURNAL_UNAVAILABLE,
            )
        return _result_for_record(changed)


def _result_for_record(record: StartJournalRecord) -> StartOperationResult:
    if record.state is StartJournalState.CONFIRMED:
        return StartOperationResult(
            operation_id=record.operation_id,
            idempotency_key=record.idempotency_key,
            state=StartState.CONFIRMED,
            confirmation=record.confirmation,
        )
    failure = record.failure or StartFailure(
        boundary=StartBoundary.TRANSPORT,
        code=StartFailureCode.TRANSPORT_AMBIGUOUS,
    )
    return StartOperationResult(
        operation_id=record.operation_id,
        idempotency_key=record.idempotency_key,
        state=StartState.OUTCOME_UNKNOWN,
        failure=failure,
    )


def _denied(verified: VerifiedUpload, code: StartFailureCode) -> StartOperationResult:
    boundary = (
        StartBoundary.IDEMPOTENCY
        if code is StartFailureCode.IDEMPOTENCY_CONFLICT
        else StartBoundary.TARGET
        if code in {StartFailureCode.DISPATCH_DISABLED, StartFailureCode.TARGET_STALE}
        else StartBoundary.REMOTE_FILE
        if code is StartFailureCode.REMOTE_MISMATCH
        else StartBoundary.PRINTER_STATE
        if code is StartFailureCode.PRINTER_NOT_IDLE
        else StartBoundary.JOURNAL
        if code is StartFailureCode.JOURNAL_UNAVAILABLE
        else StartBoundary.INTERNAL
        if code is StartFailureCode.INTERNAL_FAILURE
        else StartBoundary.RECONCILIATION
    )
    return StartOperationResult(
        operation_id=verified.qualification.operation_id,
        idempotency_key=verified.qualification.idempotency_key,
        state=StartState.DENIED,
        failure=StartFailure(boundary=boundary, code=code),
    )


def _unknown(
    verified: VerifiedUpload,
    boundary: StartBoundary,
    code: StartFailureCode,
) -> StartOperationResult:
    return StartOperationResult(
        operation_id=verified.qualification.operation_id,
        idempotency_key=verified.qualification.idempotency_key,
        state=StartState.OUTCOME_UNKNOWN,
        failure=StartFailure(boundary=boundary, code=code),
    )
