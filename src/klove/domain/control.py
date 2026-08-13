"""Typed job-control policy with explicit fail-closed evidence checks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from klove.domain.models import JobHistoryStatus, PrinterPhase, PrinterSnapshot


class ControlOperation(StrEnum):
    """The only printer-changing operations in the first control slice."""

    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"


class ControlStatus(StrEnum):
    """Stable outcome classes returned to callers."""

    CONFIRMED = "confirmed"
    DENIED = "denied"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ReconciliationDecision(StrEnum):
    """Internal interpretation of one post-dispatch poll."""

    CONFIRMED = "confirmed"
    PENDING = "pending"
    AMBIGUOUS = "ambiguous"


class ControlResult(BaseModel):
    """A bounded result that never reflects remote error text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: ControlOperation
    status: ControlStatus
    code: str


@dataclass(frozen=True, slots=True)
class ControlIntent:
    """One authenticated, target-bound, idempotent control request."""

    printer_id: str
    operation: ControlOperation
    state_token: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CachedControlEvidence:
    """The exact cached job evidence authorized by a caller."""

    phase: PrinterPhase
    eventtime: float
    job_id: str
    job_start_time: float
    filename: str
    file_position: int


@dataclass(frozen=True, slots=True)
class LiveControlState:
    """A strictly validated live Moonraker control preflight."""

    eventtime: float
    phase: PrinterPhase
    job_id: str
    job_start_time: float
    filename: str
    file_position: int
    job_status: JobHistoryStatus = JobHistoryStatus.IN_PROGRESS


_ALLOWED_PHASES = {
    ControlOperation.PAUSE: frozenset({PrinterPhase.PRINTING}),
    ControlOperation.RESUME: frozenset({PrinterPhase.PAUSED}),
    ControlOperation.CANCEL: frozenset({PrinterPhase.PRINTING, PrinterPhase.PAUSED}),
}
_TARGET_PHASES = {
    ControlOperation.PAUSE: PrinterPhase.PAUSED,
    ControlOperation.RESUME: PrinterPhase.PRINTING,
    ControlOperation.CANCEL: PrinterPhase.CANCELLED,
}
_TARGET_HISTORY_STATES = {
    ControlOperation.PAUSE: JobHistoryStatus.IN_PROGRESS,
    ControlOperation.RESUME: JobHistoryStatus.IN_PROGRESS,
    ControlOperation.CANCEL: JobHistoryStatus.CANCELLED,
}
_ALLOWED_RECONCILIATION_HISTORY = {
    ControlOperation.PAUSE: frozenset({JobHistoryStatus.IN_PROGRESS}),
    ControlOperation.RESUME: frozenset({JobHistoryStatus.IN_PROGRESS}),
    ControlOperation.CANCEL: frozenset({JobHistoryStatus.IN_PROGRESS, JobHistoryStatus.CANCELLED}),
}


def authorize_cached_intent(
    intent: ControlIntent,
    snapshot: PrinterSnapshot | None,
) -> CachedControlEvidence | str:
    """Return exact cached evidence or a stable denial code."""
    if snapshot is None:
        return "printer_unknown"
    if snapshot.state_token != intent.state_token:
        return "state_token_mismatch"
    return _control_evidence(intent.operation, snapshot)


def _control_evidence(
    operation: ControlOperation, snapshot: PrinterSnapshot
) -> CachedControlEvidence | str:
    if not snapshot.connected or snapshot.reason != "observed" or snapshot.eventtime is None:
        return "printer_unavailable"
    if snapshot.capabilities is None or not snapshot.capabilities.dispatch_eligible:
        return "capability_unavailable"
    if snapshot.phase not in _ALLOWED_PHASES[operation]:
        return "transition_denied"
    print_stats = snapshot.status.get("print_stats", {})
    virtual_sdcard = snapshot.status.get("virtual_sdcard", {})
    filename = print_stats.get("filename")
    file_position = virtual_sdcard.get("file_position")
    job = snapshot.job
    if (
        not isinstance(filename, str)
        or not filename
        or job is None
        or job.status is not JobHistoryStatus.IN_PROGRESS
        or job.filename != filename
    ):
        return "job_identity_unavailable"
    if isinstance(file_position, bool) or not isinstance(file_position, int) or file_position < 0:
        return "job_identity_unavailable"
    return CachedControlEvidence(
        phase=snapshot.phase,
        eventtime=snapshot.eventtime,
        job_id=job.job_id,
        job_start_time=job.start_time,
        filename=filename,
        file_position=file_position,
    )


def validate_cached_recheck(
    intent: ControlIntent,
    initial: CachedControlEvidence,
    snapshot: PrinterSnapshot | None,
    *,
    preflight: LiveControlState,
) -> str | None:
    """Require the exact token and bound benign monitor advances by preflight."""
    latest = authorize_cached_intent(intent, snapshot)
    if isinstance(latest, str):
        return latest
    if (
        latest.phase is not initial.phase
        or latest.job_id != initial.job_id
        or latest.job_start_time != initial.job_start_time
        or latest.filename != initial.filename
        or latest.eventtime < initial.eventtime
        or latest.file_position < initial.file_position
        or latest.phase is not preflight.phase
        or latest.job_id != preflight.job_id
        or latest.job_start_time != preflight.job_start_time
        or latest.filename != preflight.filename
        or latest.eventtime > preflight.eventtime
        or latest.file_position > preflight.file_position
        or (latest.eventtime == initial.eventtime and latest.file_position != initial.file_position)
        or (
            latest.eventtime == preflight.eventtime
            and latest.file_position != preflight.file_position
        )
    ):
        return "state_token_mismatch"
    return None


def validate_live_preflight(cached: CachedControlEvidence, live: LiveControlState) -> str | None:
    """Require the live poll to match and be no older than cached caller evidence."""
    if live.phase is not cached.phase:
        return "preflight_state_mismatch"
    if (
        live.job_id != cached.job_id
        or live.job_start_time != cached.job_start_time
        or live.filename != cached.filename
        or live.job_status is not JobHistoryStatus.IN_PROGRESS
    ):
        return "preflight_job_mismatch"
    if live.eventtime < cached.eventtime or live.file_position < cached.file_position:
        return "preflight_stale"
    if live.eventtime == cached.eventtime and live.file_position != cached.file_position:
        return "preflight_stale"
    return None


def reconcile_postcondition(
    operation: ControlOperation,
    live: LiveControlState,
    *,
    preflight: LiveControlState,
) -> ReconciliationDecision:
    """Bind post-dispatch evidence to the preflight job and target transition."""
    if (
        live.job_id != preflight.job_id
        or live.job_start_time != preflight.job_start_time
        or live.filename != preflight.filename
        or live.eventtime < preflight.eventtime
        or (live.eventtime == preflight.eventtime and live.file_position != preflight.file_position)
    ):
        return ReconciliationDecision.AMBIGUOUS
    if operation is not ControlOperation.CANCEL and live.file_position < preflight.file_position:
        return ReconciliationDecision.AMBIGUOUS
    if live.job_status not in _ALLOWED_RECONCILIATION_HISTORY[operation]:
        return ReconciliationDecision.AMBIGUOUS
    if (
        live.eventtime > preflight.eventtime
        and live.phase is _TARGET_PHASES[operation]
        and live.job_status is _TARGET_HISTORY_STATES[operation]
    ):
        return ReconciliationDecision.CONFIRMED
    return ReconciliationDecision.PENDING
