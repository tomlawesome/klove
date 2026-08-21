"""Fail-closed composition of registry qualification, upload, and typed start."""

from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Protocol, cast

from klove.domain.artifact_validation import ValidatedGcodeCandidate, inspect_gcode_3mf
from klove.domain.artifacts import (
    CONTRACT_VERSION,
    ArtifactIntent,
    ArtifactLimits,
    ArtifactOperationState,
    ArtifactQualification,
    ArtifactTargetApproval,
    SafetyProfile,
    assess_artifact_intent,
    target_for_safety_profile,
)
from klove.domain.dispatch import (
    DispatchFailure,
    DispatchFailureCode,
    DispatchGrant,
    DispatchJournalRecord,
    DispatchOperationResult,
    DispatchRequest,
    DispatchState,
    result_for_record,
)
from klove.domain.models import JobHistoryStatus, PrinterPhase
from klove.domain.onboarding import PrinterLifecycle, RegisteredPrinter
from klove.domain.start import (
    StartConfirmation,
    StartFailureCode,
    StartObservation,
    StartOperationResult,
    StartState,
)
from klove.domain.upload import UploadState, VerifiedUpload
from klove.errors import JournalError
from klove.orchestration.admission import PrinterAdmissionGates, PrinterAdmissionLease
from klove.orchestration.start import StartService
from klove.orchestration.upload import UploadService
from klove.persistence.dispatch_journal import (
    DispatchJournal,
    DispatchJournalCapacityError,
    DispatchJournalFenceError,
)
from klove.persistence.dispatch_spool import DispatchSpool, DispatchSpoolError


class PrinterLookup(Protocol):
    """Read one canonical registry record; callers never supply a printer config."""

    def get(self, printer_uuid: str) -> RegisteredPrinter | None: ...


class DispatchReceiveLimiter:
    """One installation-wide, non-waiting bound for hostile archive intake."""

    def __init__(self, capacity: int) -> None:
        if type(capacity) is not int or not 1 <= capacity <= 64:
            raise ValueError("receive capacity is outside its accepted bounds")
        self._capacity = capacity
        self._available = capacity

    def try_acquire(self) -> bool:
        """Reserve one intake slot without queuing an untrusted producer."""
        if self._available == 0:
            return False
        self._available -= 1
        return True

    def release(self) -> None:
        """Return one slot and reject coordinator bookkeeping corruption."""
        if self._available >= self._capacity:
            raise RuntimeError("dispatch receive limiter released too many times")
        self._available += 1


@dataclass(slots=True)
class _ReceivingOperation:
    """In-process cancellation handshake for an already durable receiving row."""

    done: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def cancel_requested(self) -> bool:
        """Expose the cancellation handshake without permitting a false reset."""
        return self.cancelled.is_set()


class DispatchCoordinator:
    """Coordinate one printer's accepted dispatch lifecycle without adding transport."""

    def __init__(  # noqa: PLR0913, PLR0917 -- explicit safety-critical dependencies.
        self,
        printer_uuid: str,
        registry: PrinterLookup,
        uploader: UploadService,
        starter: StartService,
        journal: DispatchJournal,
        spool: DispatchSpool,
        *,
        limits: ArtifactLimits,
        operation_capacity: int,
        admissions: PrinterAdmissionGates,
        receive_limiter: DispatchReceiveLimiter,
        receive_timeout_seconds: float = 300.0,
    ) -> None:
        if (
            type(operation_capacity) is not int
            or operation_capacity < 1
            or operation_capacity > 100_000
            or type(receive_timeout_seconds) not in {int, float}
            or not math.isfinite(receive_timeout_seconds)
            or not 1 <= receive_timeout_seconds <= 3_600
        ):
            raise ValueError("coordinator limits are outside their accepted bounds")
        self._printer_uuid = printer_uuid
        self._registry = registry
        self._uploader = uploader
        self._starter = starter
        self._journal = journal
        self._spool = spool
        self._limits = limits
        self._operation_capacity = operation_capacity
        self._admissions = admissions
        self._receive_limiter = receive_limiter
        self._receive_timeout = receive_timeout_seconds
        self._ready = False
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._tasks_lock = asyncio.Lock()
        self._receiving: dict[tuple[str, str], _ReceivingOperation] = {}

    async def initialize(self) -> None:
        """Reconcile durable coordinator state before reopening dispatch admission."""
        self._ready = False
        try:
            self._journal.initialize()
            self._spool.initialize()
            records = self._journal.records()
            known_operations = frozenset(record.request.operation_id for record in records)
            if not self._spool.operation_ids() <= known_operations:
                return
        except (JournalError, DispatchSpoolError):
            return
        continuations: list[DispatchJournalRecord] = []
        for record in records:
            if record.request.printer_uuid != self._printer_uuid:
                continue
            try:
                recovered = self._recover_record(record)
                if recovered.state is DispatchState.PRINTING:
                    recovered = await self._reconcile_printing(recovered)
                elif _needs_start_reconciliation(recovered):
                    recovered = await self._reconcile_started(recovered, resume_missing_start=True)
                if _requires_retained_source(recovered):
                    self._spool.verify(recovered.request)
                else:
                    self._spool.remove(recovered.request)
            except (JournalError, DispatchSpoolError):
                return
            if recovered.state in {DispatchState.ACCEPTED, DispatchState.VERIFIED}:
                continuations.append(recovered)
        self._ready = True
        for record in continuations:
            await self._schedule(record)

    async def submit(  # noqa: PLR0911, PLR0912, PLR0915 -- explicit durable intake denials.
        self,
        grant: DispatchGrant,
        request: DispatchRequest,
        archive: AsyncIterator[bytes],
    ) -> DispatchOperationResult:
        """Durably accept exact bytes, then continue asynchronously under the printer gate."""
        if grant.printer_uuid != request.printer_uuid or request.printer_uuid != self._printer_uuid:
            return _unrecorded(request, DispatchFailureCode.ACCESS_DENIED)
        if not self._ready:
            return _unrecorded(request, DispatchFailureCode.RECONCILIATION_PENDING)
        async with self._admissions.hold(request.printer_uuid):
            profile, failure = self._current_profile(request)
            if profile is None:
                return _unrecorded(request, failure)
            receiving = DispatchJournalRecord(
                grant=grant,
                request=request,
                state=DispatchState.RECEIVING,
            )
            try:
                existing, created = self._journal.reserve(
                    receiving, capacity=self._operation_capacity
                )
            except DispatchJournalCapacityError:
                return _unrecorded(request, DispatchFailureCode.CAPACITY_EXHAUSTED)
            except DispatchJournalFenceError:
                return _unrecorded(request, DispatchFailureCode.PRINTER_FENCED)
            except JournalError:
                return _unrecorded(request, DispatchFailureCode.STORAGE_UNAVAILABLE)
            if not created:
                if existing.grant != grant or existing.request != request:
                    return _unrecorded(request, DispatchFailureCode.OPERATION_CONFLICT)
                await self._schedule(existing)
                return result_for_record(existing)
            receiving_operation = _ReceivingOperation()
            self._receiving[_operation_key(request)] = receiving_operation
        if receiving_operation.cancel_requested:
            try:
                cancelled = self._terminal(
                    receiving, DispatchState.CANCELLED, DispatchFailureCode.CANCELLED
                )
            except JournalError:
                return _unrecorded(request, DispatchFailureCode.STORAGE_UNAVAILABLE)
            finally:
                self._finish_receiving(request, receiving_operation)
            return result_for_record(cancelled)
        if not self._receive_limiter.try_acquire():
            try:
                failed = self._terminal(
                    receiving, DispatchState.FAILED, DispatchFailureCode.CAPACITY_EXHAUSTED
                )
            except JournalError:
                return _unrecorded(request, DispatchFailureCode.STORAGE_UNAVAILABLE)
            finally:
                self._finish_receiving(request, receiving_operation)
            return result_for_record(failed)
        try:
            await self._spool.write_stream(
                request,
                archive,
                timeout_seconds=self._receive_timeout,
                cancel_event=receiving_operation.cancelled,
            )
            # Cancellation observes RECEIVING under this same gate.  Re-enter before
            # choosing its terminal transition so an arriving cancel either wins or
            # subsequently observes the durable ACCEPTED row it may cancel.
            async with self._admissions.hold(request.printer_uuid):
                if _receive_was_cancelled(receiving_operation):
                    accepted = self._terminal(
                        receiving, DispatchState.CANCELLED, DispatchFailureCode.CANCELLED
                    )
                else:
                    accepted = _changed(receiving, state=DispatchState.ACCEPTED)
                    accepted = self._journal.replace(receiving, accepted)
        except asyncio.CancelledError:
            with suppress(JournalError):
                self._terminal(
                    receiving,
                    DispatchState.CANCELLED
                    if receiving_operation.cancel_requested
                    else DispatchState.FAILED,
                    DispatchFailureCode.CANCELLED
                    if receiving_operation.cancel_requested
                    else DispatchFailureCode.SOURCE_INVALID,
                )
            raise
        except DispatchSpoolError:
            try:
                accepted = self._terminal(
                    receiving,
                    DispatchState.CANCELLED
                    if receiving_operation.cancel_requested
                    else DispatchState.FAILED,
                    DispatchFailureCode.CANCELLED
                    if receiving_operation.cancel_requested
                    else DispatchFailureCode.SOURCE_INVALID,
                )
            except JournalError:
                return _unrecorded(request, DispatchFailureCode.STORAGE_UNAVAILABLE)
        except JournalError:
            return _unrecorded(request, DispatchFailureCode.STORAGE_UNAVAILABLE)
        finally:
            self._receive_limiter.release()
            self._finish_receiving(request, receiving_operation)
        if accepted.state is DispatchState.ACCEPTED:
            await self._schedule(accepted)
        return result_for_record(accepted)

    async def result(
        self,
        grant: DispatchGrant,
        operation_id: str,
        idempotency_key: str,
    ) -> DispatchOperationResult | None:
        """Return only a same-principal exact operation; absence is non-enumerating."""
        try:
            record = self._journal.lookup(operation_id, idempotency_key)
        except JournalError:
            return None
        if record is None or record.grant != grant:
            return None
        try:
            if record.state is DispatchState.PRINTING:
                record = await self._reconcile_printing(record)
            elif _needs_start_reconciliation(record):
                record = await self._reconcile_started(record)
        except JournalError:
            return None
        return result_for_record(record)

    async def reconcile(
        self,
        grant: DispatchGrant,
        operation_id: str,
        idempotency_key: str,
    ) -> DispatchOperationResult | None:
        """Compatibility alias for the read-only reconciliation built into lookup."""
        return await self.result(grant, operation_id, idempotency_key)

    async def cancel(  # noqa: PLR0911, PLR0912 -- exact pre-start cancellation outcomes.
        self,
        grant: DispatchGrant,
        operation_id: str,
        idempotency_key: str,
    ) -> DispatchOperationResult | None:
        """Commit pre-start cancellation without issuing transport or cleanup actions."""
        if grant.printer_uuid != self._printer_uuid:
            return None
        receiving_done: asyncio.Event | None = None
        async with self._admissions.hold(grant.printer_uuid):
            try:
                current = self._journal.lookup(operation_id, idempotency_key)
            except JournalError:
                return None
            if current is None or current.grant != grant:
                return None
            if current.state is DispatchState.RECEIVING:
                receiving = self._receiving.get(_operation_key(current.request))
                if receiving is not None:
                    receiving.cancelled.set()
                    receiving_done = receiving.done
                else:
                    cancelled = _changed(
                        current,
                        state=DispatchState.CANCELLED,
                        failure=DispatchFailure(code=DispatchFailureCode.CANCELLED),
                    )
                    try:
                        cancelled = self._journal.replace(current, cancelled)
                    except JournalError:
                        return None
                    self._remove_after_terminal(cancelled)
                    return result_for_record(cancelled)
            elif current.state not in {DispatchState.ACCEPTED, DispatchState.VERIFIED}:
                return result_for_record(current)
            else:
                cancelled = _changed(
                    current,
                    state=DispatchState.CANCELLED,
                    failure=DispatchFailure(code=DispatchFailureCode.CANCELLED),
                )
                try:
                    cancelled = self._journal.replace(current, cancelled)
                except JournalError:
                    return None
                self._remove_after_terminal(cancelled)
                return result_for_record(cancelled)
        if not await _wait_for_receiving(receiving_done):
            return None
        try:
            current = self._journal.lookup(operation_id, idempotency_key)
        except JournalError:
            return None
        if current is None or current.grant != grant:
            return None
        return result_for_record(current)

    def _recover_record(self, record: DispatchJournalRecord) -> DispatchJournalRecord:
        if record.state is DispatchState.RECEIVING:
            self._spool.discard_incomplete(record.request)
            return self._terminal(record, DispatchState.FAILED, DispatchFailureCode.SOURCE_INVALID)
        if record.state is DispatchState.UPLOADING:
            return self._terminal(
                record, DispatchState.OUTCOME_UNKNOWN, DispatchFailureCode.UPLOAD_AMBIGUOUS
            )
        if record.state in {
            DispatchState.ACCEPTED,
            DispatchState.VERIFIED,
        } and not self._spool.exists(record.request):
            return self._terminal(record, DispatchState.FAILED, DispatchFailureCode.SOURCE_MISSING)
        return record

    async def _schedule(self, record: DispatchJournalRecord) -> None:
        async with self._tasks_lock:
            existing = self._tasks.get(record.request.operation_id)
            if existing is not None and not existing.done():
                return
            task = asyncio.create_task(
                self._run(record.request.operation_id, record.request.idempotency_key),
                name=f"dispatch:{record.request.operation_id}",
            )
            self._tasks[record.request.operation_id] = task
            task.add_done_callback(
                lambda done: self._forget_task(record.request.operation_id, done)
            )

    def _forget_task(self, operation_id: str, completed: asyncio.Task[None]) -> None:
        """Remove only the matching finished worker, never a newer replacement."""
        if self._tasks.get(operation_id) is completed:
            self._tasks.pop(operation_id, None)

    async def _run(self, operation_id: str, idempotency_key: str) -> None:
        try:
            record = self._journal.lookup(operation_id, idempotency_key)
            if record is None:
                return
            if record.state is DispatchState.ACCEPTED:
                record = await self._qualify_and_upload(record)
            if record.state is DispatchState.VERIFIED:
                await self._start(record)
        except asyncio.CancelledError:
            raise
        except (JournalError, DispatchSpoolError):
            return
        except Exception:
            try:
                record = self._journal.lookup(operation_id, idempotency_key)
                if record is None:
                    return
                if record.state is DispatchState.UPLOADING:
                    self._terminal(
                        record,
                        DispatchState.OUTCOME_UNKNOWN,
                        DispatchFailureCode.UPLOAD_AMBIGUOUS,
                    )
                elif record.state is DispatchState.STARTING:
                    self._terminal(
                        record,
                        DispatchState.OUTCOME_UNKNOWN,
                        DispatchFailureCode.START_AMBIGUOUS,
                    )
                elif record.state in {DispatchState.ACCEPTED, DispatchState.VERIFIED}:
                    self._terminal(
                        record, DispatchState.OUTCOME_UNKNOWN, DispatchFailureCode.INTERNAL_FAILURE
                    )
            except JournalError:
                return

    async def _qualify_and_upload(  # noqa: PLR0911 -- explicit fail-closed stage exits.
        self, record: DispatchJournalRecord
    ) -> DispatchJournalRecord:
        async with self._admissions.lease(record.request.printer_uuid) as lease:
            current = self._current_record(record)
            if current is None or current.state is not DispatchState.ACCEPTED:
                return record if current is None else current
            profile, failure = self._current_profile(current.request)
            if profile is None:
                return self._terminal(current, DispatchState.FAILED, failure)
            try:
                archive = self._spool.read(current.request)
            except DispatchSpoolError:
                return self._terminal(
                    current, DispatchState.FAILED, DispatchFailureCode.SOURCE_MISSING
                )
            candidate, qualification = self._qualify(current, profile, archive)
            if candidate is None or qualification is None:
                return self._terminal(
                    current, DispatchState.FAILED, DispatchFailureCode.QUALIFICATION_DENIED
                )
            uploading = _changed(
                current,
                state=DispatchState.UPLOADING,
                qualification=qualification,
            )
            uploading = self._journal.replace(current, uploading)
            result = await self._uploader.execute(candidate, qualification, admission_lease=lease)
            if result.state is UploadState.VERIFIED and result.verified is not None:
                return self._journal.replace(
                    uploading,
                    _changed(uploading, state=DispatchState.VERIFIED, verified=result.verified),
                )
            if result.state is UploadState.OUTCOME_UNKNOWN:
                return self._terminal(
                    uploading, DispatchState.OUTCOME_UNKNOWN, DispatchFailureCode.UPLOAD_AMBIGUOUS
                )
            return self._terminal(
                uploading, DispatchState.FAILED, DispatchFailureCode.UPLOAD_DENIED
            )

    async def _start(  # noqa: PLR0911 -- explicit fail-closed stage exits.
        self, record: DispatchJournalRecord
    ) -> DispatchJournalRecord:
        async with self._admissions.lease(record.request.printer_uuid) as lease:
            current = self._current_record(record)
            if current is None or current.state not in {
                DispatchState.VERIFIED,
                DispatchState.STARTING,
            }:
                return record if current is None else current
            profile, failure = self._current_profile(current.request)
            if profile is None:
                return self._terminal(current, DispatchState.FAILED, failure)
            if (
                current.verified is None
                or target_for_safety_profile(profile) != current.verified.qualification.target
            ):
                return self._terminal(
                    current, DispatchState.FAILED, DispatchFailureCode.PROFILE_STALE
                )
            starting = current
            if current.state is DispatchState.VERIFIED:
                starting = self._journal.replace(
                    current, _changed(current, state=DispatchState.STARTING)
                )
            verified = cast(VerifiedUpload, starting.verified)
            result = await self._starter.execute(verified, admission_lease=lease)
            if result.state is StartState.OUTCOME_UNKNOWN:
                return self._terminal(
                    starting, DispatchState.OUTCOME_UNKNOWN, DispatchFailureCode.START_AMBIGUOUS
                )
            if result.state is StartState.DENIED:
                return self._terminal(
                    starting, DispatchState.FAILED, DispatchFailureCode.START_DENIED
                )
            if result.confirmation is None:
                return self._terminal(
                    starting, DispatchState.OUTCOME_UNKNOWN, DispatchFailureCode.START_AMBIGUOUS
                )
            return self._finalize_start(starting, result)

    async def _reconcile_started(
        self,
        record: DispatchJournalRecord,
        *,
        resume_missing_start: bool = False,
    ) -> DispatchJournalRecord:
        """Re-observe a durable potentially-dispatched start without issuing RPCs."""
        async with self._admissions.lease(record.request.printer_uuid) as lease:
            current = self._current_record(record)
            if (
                current is None
                or current.verified is None
                or not _needs_start_reconciliation(current)
            ):
                return record if current is None else current
            result = await self._starter.reconcile(current.verified, admission_lease=lease)
            if result.state is not StartState.CONFIRMED:
                if (
                    resume_missing_start
                    and current.state is DispatchState.STARTING
                    and result.state is StartState.DENIED
                    and result.failure is not None
                    and result.failure.code is StartFailureCode.RECONCILIATION_PENDING
                ):
                    profile, failure = self._current_profile(current.request)
                    if profile is None:
                        return self._terminal(current, DispatchState.FAILED, failure)
                    if target_for_safety_profile(profile) != current.verified.qualification.target:
                        return self._terminal(
                            current, DispatchState.FAILED, DispatchFailureCode.PROFILE_STALE
                        )
                    return self._journal.replace(
                        current, _changed(current, state=DispatchState.VERIFIED)
                    )
                return current
            return await self._finalize_observed_start(current, result, lease)

    async def _reconcile_printing(self, record: DispatchJournalRecord) -> DispatchJournalRecord:
        async with self._admissions.lease(record.request.printer_uuid) as lease:
            current = self._current_record(record)
            if (
                current is None
                or current.state is not DispatchState.PRINTING
                or current.verified is None
                or current.start is None
            ):
                return record if current is None else current
            return await self._finalize_observed_start(current, current.start, lease)

    async def _finalize_observed_start(
        self,
        record: DispatchJournalRecord,
        result: StartOperationResult,
        lease: PrinterAdmissionLease,
    ) -> DispatchJournalRecord:
        """Require fresh exact job evidence before reporting any post-start state."""
        if record.verified is None or result.confirmation is None:
            return self._terminal(
                record, DispatchState.OUTCOME_UNKNOWN, DispatchFailureCode.START_AMBIGUOUS
            )
        confirmation = result.confirmation
        observation = await self._starter.observe(record.verified, admission_lease=lease)
        if observation is None:
            return self._terminal(
                record, DispatchState.OUTCOME_UNKNOWN, DispatchFailureCode.START_AMBIGUOUS
            )
        latest_job = observation.latest_job
        if latest_job is None or not _observation_matches_confirmation(observation, confirmation):
            return self._terminal(
                record, DispatchState.OUTCOME_UNKNOWN, DispatchFailureCode.START_AMBIGUOUS
            )
        refreshed = StartOperationResult(
            operation_id=result.operation_id,
            idempotency_key=result.idempotency_key,
            state=StartState.CONFIRMED,
            confirmation=StartConfirmation(
                path=confirmation.path,
                eventtime=observation.eventtime,
                phase=_phase_for_history(latest_job.status),
                file_position=observation.file_position,
                job=latest_job,
            ),
        )
        return self._finalize_start(record, refreshed)

    def _finalize_start(
        self,
        record: DispatchJournalRecord,
        result: StartOperationResult,
    ) -> DispatchJournalRecord:
        if result.confirmation is None:
            return self._terminal(
                record, DispatchState.OUTCOME_UNKNOWN, DispatchFailureCode.START_AMBIGUOUS
            )
        status = result.confirmation.job.status
        state = (
            DispatchState.PRINTING
            if status is JobHistoryStatus.IN_PROGRESS
            else DispatchState.COMPLETED
            if status is JobHistoryStatus.COMPLETED
            else DispatchState.CANCELLED
            if status is JobHistoryStatus.CANCELLED
            else DispatchState.FAILED
        )
        failure = (
            DispatchFailure(code=DispatchFailureCode.CANCELLED)
            if state is DispatchState.CANCELLED
            else DispatchFailure(code=DispatchFailureCode.PRINT_FAILED)
            if state is DispatchState.FAILED
            else None
        )
        completed = self._journal.replace(
            record,
            _changed(record, state=state, start=result, failure=failure),
        )
        self._remove_after_terminal(completed)
        return completed

    def _qualify(
        self,
        record: DispatchJournalRecord,
        profile: SafetyProfile,
        archive: bytes,
    ) -> tuple[ValidatedGcodeCandidate | None, ArtifactQualification | None]:
        request = record.request
        intent = ArtifactIntent(
            contract_version=CONTRACT_VERSION,
            operation_id=request.operation_id,
            idempotency_key=request.idempotency_key,
            artifact=request.artifact,
            selected_plate=request.selected_plate,
            target=request.target,
        )
        inspected = inspect_gcode_3mf(intent, archive, limits=self._limits)
        if not isinstance(inspected, ValidatedGcodeCandidate):
            return None, None
        approval = ArtifactTargetApproval(
            contract_version=CONTRACT_VERSION,
            approval_id=str(uuid.uuid4()),
            authority_id=record.grant.principal_id,
            operation_id=request.operation_id,
            idempotency_key=request.idempotency_key,
            artifact=request.artifact,
            selected_plate=inspected.inspection.selected_plate,
            target=request.target,
            compatibility=profile.compatibility,
        )
        assessed = assess_artifact_intent(
            intent,
            (inspected.inspection,),
            (approval,),
            current_profile=profile,
            manual_override=None,
            limits=self._limits,
        )
        if assessed.state is not ArtifactOperationState.QUALIFIED or assessed.qualification is None:
            return None, None
        return inspected, assessed.qualification

    def _current_profile(
        self, request: DispatchRequest
    ) -> tuple[SafetyProfile | None, DispatchFailureCode]:
        try:
            record = self._registry.get(request.printer_uuid)
        except Exception:
            return None, DispatchFailureCode.PRINTER_UNAVAILABLE
        if record is None or record.lifecycle is not PrinterLifecycle.ACTIVE:
            return None, DispatchFailureCode.PRINTER_UNAVAILABLE
        if not record.dispatch_enabled or not record.identity.capabilities.dispatch_eligible:
            return None, DispatchFailureCode.DISPATCH_DISABLED
        profiles = tuple(
            profile
            for profile in record.safety_profiles
            if profile.slicer_profile_id == request.slicer_profile_id
        )
        if len(profiles) != 1 or target_for_safety_profile(profiles[0]) != request.target:
            return None, DispatchFailureCode.PROFILE_STALE
        return profiles[0], DispatchFailureCode.INTERNAL_FAILURE

    def _current_record(self, prior: DispatchJournalRecord) -> DispatchJournalRecord | None:
        current = self._journal.lookup(prior.request.operation_id, prior.request.idempotency_key)
        if current is None or current.grant != prior.grant or current.request != prior.request:
            return None
        return current

    def _terminal(
        self,
        record: DispatchJournalRecord,
        state: DispatchState,
        code: DispatchFailureCode,
    ) -> DispatchJournalRecord:
        changed = _changed(record, state=state, failure=DispatchFailure(code=code))
        changed = self._journal.replace(record, changed)
        if state in {DispatchState.FAILED, DispatchState.CANCELLED}:
            self._remove_after_terminal(changed)
        return changed

    def _remove_after_terminal(self, record: DispatchJournalRecord) -> None:
        if record.state not in {
            DispatchState.PRINTING,
            DispatchState.COMPLETED,
            DispatchState.CANCELLED,
            DispatchState.FAILED,
        }:
            return
        try:
            self._spool.remove(record.request)
        except DispatchSpoolError:
            return

    def _finish_receiving(
        self,
        request: DispatchRequest,
        receiving: _ReceivingOperation,
    ) -> None:
        """Wake a concurrent pre-start cancellation after intake stops touching source."""
        if self._receiving.get(_operation_key(request)) is receiving:
            del self._receiving[_operation_key(request)]
        receiving.done.set()


def _changed(  # noqa: PLR0913 -- lifecycle evidence fields are individually explicit.
    record: DispatchJournalRecord,
    *,
    state: DispatchState,
    qualification: object | None = None,
    verified: object | None = None,
    start: object | None = None,
    failure: DispatchFailure | None = None,
) -> DispatchJournalRecord:
    """Make one validated lifecycle transition while preserving immutable identity."""
    updates: dict[str, object] = {"state": state, "failure": failure}
    if qualification is not None:
        updates["qualification"] = qualification
    if verified is not None:
        updates["verified"] = verified
    if start is not None:
        updates["start"] = start
    return DispatchJournalRecord.model_validate(record.model_dump() | updates)


def _unrecorded(request: DispatchRequest, code: DispatchFailureCode) -> DispatchOperationResult:
    """Build a bounded pre-acceptance denial without making it a durable operation."""
    return DispatchOperationResult(
        operation_id=request.operation_id,
        printer_uuid=request.printer_uuid,
        state=DispatchState.FAILED,
        target=request.target,
        archive_sha256=request.archive_sha256,
        archive_size_bytes=request.archive_size_bytes,
        failure=DispatchFailure(code=code),
    )


def _operation_key(request: DispatchRequest) -> tuple[str, str]:
    """Address an in-process intake with the exact durable composite key."""
    return request.operation_id, request.idempotency_key


def _needs_start_reconciliation(record: DispatchJournalRecord) -> bool:
    """Identify records whose start call may already have happened."""
    return record.state is DispatchState.STARTING or (
        record.state is DispatchState.OUTCOME_UNKNOWN
        and record.verified is not None
        and record.failure is not None
        and record.failure.code is DispatchFailureCode.START_AMBIGUOUS
    )


def _requires_retained_source(record: DispatchJournalRecord) -> bool:
    """Keep exact bytes for every safe continuation or unresolved action fence."""
    if record.state is DispatchState.OUTCOME_UNKNOWN:
        return record.start is None
    return record.state in {
        DispatchState.ACCEPTED,
        DispatchState.UPLOADING,
        DispatchState.VERIFIED,
        DispatchState.STARTING,
    }


async def _wait_for_receiving(receiving: asyncio.Event | None) -> bool:
    """Wait only when the durable receiving row has a live intake owner."""
    if receiving is None:
        return False
    await receiving.wait()
    return True


def _receive_was_cancelled(receiving: _ReceivingOperation) -> bool:
    """Read the cancellation event across an awaited hostile source boundary."""
    return receiving.cancel_requested


def _observation_matches_confirmation(
    observation: StartObservation,
    confirmation: StartConfirmation,
) -> bool:
    """Require one current observation to name the exact immutable started job."""
    job = observation.latest_job
    if (
        job is None
        or job.job_id != confirmation.job.job_id
        or job.start_time != confirmation.job.start_time
        or job.filename != confirmation.path
    ):
        return False
    expected = _phase_for_history(job.status)
    if job.status is JobHistoryStatus.IN_PROGRESS:
        return observation.phase in {PrinterPhase.PRINTING, PrinterPhase.PAUSED} and (
            observation.filename == confirmation.path
        )
    return observation.phase in {PrinterPhase.IDLE, expected} and (
        observation.phase is PrinterPhase.IDLE or observation.filename == confirmation.path
    )


def _phase_for_history(status: JobHistoryStatus) -> PrinterPhase:
    if status is JobHistoryStatus.IN_PROGRESS:
        return PrinterPhase.PRINTING
    if status is JobHistoryStatus.COMPLETED:
        return PrinterPhase.COMPLETED
    if status is JobHistoryStatus.CANCELLED:
        return PrinterPhase.CANCELLED
    return PrinterPhase.ERROR
