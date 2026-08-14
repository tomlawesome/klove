"""Typed print-start evidence, durable state, and reconciliation policy."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from klove.domain.artifacts import CanonicalUuid4
from klove.domain.models import JobHistoryStatus, JobIdentitySnapshot, PrinterPhase
from klove.domain.upload import MoonrakerGcodePath, VerifiedUpload


class StartState(StrEnum):
    """Externally meaningful terminal states for one start operation."""

    CONFIRMED = "confirmed"
    DENIED = "denied"
    OUTCOME_UNKNOWN = "outcome_unknown"


class StartBoundary(StrEnum):
    """Stable boundaries at which print-start evidence can fail closed."""

    RECONCILIATION = "reconciliation"
    IDEMPOTENCY = "idempotency"
    TARGET = "target"
    REMOTE_FILE = "remote_file"
    PRINTER_STATE = "printer_state"
    JOURNAL = "journal"
    TRANSPORT = "transport"
    INTERNAL = "internal"


class StartFailureCode(StrEnum):
    """Bounded non-reflective print-start failure classifications."""

    RECONCILIATION_PENDING = "reconciliation_pending"
    DISPATCH_DISABLED = "dispatch_disabled"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    PRINTER_FENCED = "printer_fenced"
    TARGET_STALE = "target_stale"
    REMOTE_MISMATCH = "remote_mismatch"
    PRINTER_NOT_IDLE = "printer_not_idle"
    JOURNAL_UNAVAILABLE = "journal_unavailable"
    TRANSPORT_AMBIGUOUS = "transport_ambiguous"
    CONFIRMATION_TIMEOUT = "confirmation_timeout"
    INTERNAL_FAILURE = "internal_failure"


class StartFailure(BaseModel):
    """One bounded denial or ambiguous start reason."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    boundary: StartBoundary
    code: StartFailureCode


class StartObservation(BaseModel):
    """One coherent live-state sample bracketed by an immutable history identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    eventtime: float = Field(ge=0, allow_inf_nan=False)
    phase: PrinterPhase
    filename: str = Field(max_length=1024)
    file_position: int = Field(ge=0)
    latest_job: JobIdentitySnapshot | None = None

    @model_validator(mode="after")
    def active_filename_is_present(self) -> StartObservation:
        """Require a filename whenever Klipper reports a non-idle job phase."""
        if self.phase is not PrinterPhase.IDLE and not self.filename:
            raise ValueError("non-idle observations require a filename")
        return self


class StartPreflight(BaseModel):
    """Exact idle and prior-history evidence durably recorded before dispatch."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    observation: StartObservation

    @model_validator(mode="after")
    def printer_is_idle(self) -> StartPreflight:
        """Prevent non-idle evidence from becoming a dispatch reservation."""
        if self.observation.phase is not PrinterPhase.IDLE:
            raise ValueError("print start requires an idle printer")
        return self


class StartConfirmation(BaseModel):
    """Later exact history proof that the operation's remote path started."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: MoonrakerGcodePath
    eventtime: float = Field(ge=0, allow_inf_nan=False)
    phase: PrinterPhase
    file_position: int = Field(ge=0)
    job: JobIdentitySnapshot

    @model_validator(mode="after")
    def history_matches_confirmation(self) -> StartConfirmation:
        """Bind the canonical phase and exact filename to one history job."""
        if self.job.filename != self.path:
            raise ValueError("confirmation history filename does not match the path")
        expected = _phase_for_status(self.job.status)
        if self.phase not in expected:
            raise ValueError("confirmation phase contradicts history status")
        return self


class StartOperationResult(BaseModel):
    """Terminal start result for one operation and idempotency identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operation_id: CanonicalUuid4
    idempotency_key: CanonicalUuid4
    state: StartState
    failure: StartFailure | None = None
    confirmation: StartConfirmation | None = None

    @model_validator(mode="after")
    def payload_matches_state(self) -> StartOperationResult:
        """Keep ambiguous or denied operations from carrying start confirmation."""
        if self.state is StartState.CONFIRMED:
            if self.confirmation is None:
                raise ValueError("confirmed starts require confirmation evidence")
            if self.failure is not None:
                raise ValueError("confirmed starts must not contain a failure")
        else:
            if self.failure is None:
                raise ValueError("non-confirmed starts require a failure")
            if self.confirmation is not None:
                raise ValueError("non-confirmed starts must not contain confirmation")
        return self


class StartJournalState(StrEnum):
    """Durable states; `dispatching` always means the call may have happened."""

    DISPATCHING = "dispatching"
    CONFIRMED = "confirmed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class StartJournalRecord(BaseModel):
    """Strict durable operation data sufficient for restart reconciliation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operation_id: CanonicalUuid4
    idempotency_key: CanonicalUuid4
    printer_uuid: CanonicalUuid4
    verified: VerifiedUpload
    preflight: StartPreflight
    state: StartJournalState
    failure: StartFailure | None = None
    confirmation: StartConfirmation | None = None

    @model_validator(mode="after")
    def durable_identity_is_consistent(self) -> StartJournalRecord:
        """Reject a row whose keys, target, or state payload contradict its evidence."""
        qualification = self.verified.qualification
        if (
            qualification.operation_id != self.operation_id
            or qualification.idempotency_key != self.idempotency_key
            or qualification.target.printer_uuid != self.printer_uuid
        ):
            raise ValueError("journal identity does not match verified upload")
        if self.state is StartJournalState.CONFIRMED:
            if self.confirmation is None or self.failure is not None:
                raise ValueError("confirmed journal rows require only confirmation")
        elif self.state is StartJournalState.OUTCOME_UNKNOWN:
            if self.failure is None or self.confirmation is not None:
                raise ValueError("unknown journal rows require only a failure")
        elif self.failure is not None or self.confirmation is not None:
            raise ValueError("dispatching journal rows cannot contain a terminal payload")
        return self


class StartReconciliationDecision(StrEnum):
    """Read-only post-dispatch evaluation outcomes."""

    CONFIRMED = "confirmed"
    PENDING = "pending"
    AMBIGUOUS = "ambiguous"


def reconcile_start(
    verified: VerifiedUpload,
    preflight: StartPreflight,
    observation: StartObservation,
) -> tuple[StartReconciliationDecision, StartConfirmation | None]:
    """Require a new, later exact history identity before confirming start."""
    latest = observation.latest_job
    prior = preflight.observation.latest_job
    if latest is not None and latest.filename == verified.path:
        if observation.eventtime <= preflight.observation.eventtime:
            return StartReconciliationDecision.AMBIGUOUS, None
        later = prior is None or (
            latest.job_id != prior.job_id and latest.start_time > prior.start_time
        )
        if not later:
            return StartReconciliationDecision.AMBIGUOUS, None
        phases = _phase_for_status(latest.status)
        if observation.phase is not PrinterPhase.IDLE and (
            observation.filename != verified.path or observation.phase not in phases
        ):
            return StartReconciliationDecision.AMBIGUOUS, None
        phase = (
            observation.phase
            if observation.phase in phases
            else _canonical_phase_for_status(latest.status)
        )
        return (
            StartReconciliationDecision.CONFIRMED,
            StartConfirmation(
                path=verified.path,
                eventtime=observation.eventtime,
                phase=phase,
                file_position=observation.file_position,
                job=latest,
            ),
        )
    unchanged_history = latest == prior
    if observation.phase is PrinterPhase.IDLE and unchanged_history:
        return StartReconciliationDecision.PENDING, None
    return StartReconciliationDecision.AMBIGUOUS, None


def _phase_for_status(status: JobHistoryStatus) -> frozenset[PrinterPhase]:
    if status is JobHistoryStatus.IN_PROGRESS:
        return frozenset({PrinterPhase.PRINTING, PrinterPhase.PAUSED})
    if status is JobHistoryStatus.COMPLETED:
        return frozenset({PrinterPhase.COMPLETED})
    if status is JobHistoryStatus.CANCELLED:
        return frozenset({PrinterPhase.CANCELLED})
    return frozenset({PrinterPhase.ERROR})


def _canonical_phase_for_status(status: JobHistoryStatus) -> PrinterPhase:
    if status is JobHistoryStatus.IN_PROGRESS:
        return PrinterPhase.PRINTING
    return next(iter(_phase_for_status(status)))
