"""Typed job-control policy with explicit fail-closed evidence checks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from klove.domain.models import PrinterPhase, PrinterSnapshot


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
    filename: str
    file_position: int


@dataclass(frozen=True, slots=True)
class LiveControlState:
    """A strictly validated live Moonraker control preflight."""

    eventtime: float
    phase: PrinterPhase
    filename: str
    file_position: int


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


def authorize_cached_intent(  # noqa: PLR0911 -- each denial is deliberately explicit.
    intent: ControlIntent,
    snapshot: PrinterSnapshot | None,
) -> CachedControlEvidence | str:
    """Return exact cached evidence or a stable denial code."""
    if snapshot is None:
        return "printer_unknown"
    if snapshot.state_token != intent.state_token:
        return "state_token_mismatch"
    if not snapshot.connected or snapshot.reason != "observed" or snapshot.eventtime is None:
        return "printer_unavailable"
    if snapshot.capabilities is None or not snapshot.capabilities.dispatch_eligible:
        return "capability_unavailable"
    if snapshot.phase not in _ALLOWED_PHASES[intent.operation]:
        return "transition_denied"
    print_stats = snapshot.status.get("print_stats", {})
    virtual_sdcard = snapshot.status.get("virtual_sdcard", {})
    filename = print_stats.get("filename")
    file_position = virtual_sdcard.get("file_position")
    if not isinstance(filename, str) or not filename:
        return "job_identity_unavailable"
    if isinstance(file_position, bool) or not isinstance(file_position, int) or file_position < 0:
        return "job_identity_unavailable"
    return CachedControlEvidence(
        phase=snapshot.phase,
        eventtime=snapshot.eventtime,
        filename=filename,
        file_position=file_position,
    )


def validate_live_preflight(cached: CachedControlEvidence, live: LiveControlState) -> str | None:
    """Require the live poll to match and be no older than cached caller evidence."""
    if live.phase is not cached.phase:
        return "preflight_state_mismatch"
    if live.filename != cached.filename:
        return "preflight_job_mismatch"
    if live.eventtime < cached.eventtime or live.file_position < cached.file_position:
        return "preflight_stale"
    return None


def reconcile_postcondition(
    operation: ControlOperation,
    live: LiveControlState,
    *,
    preflight: LiveControlState,
) -> ReconciliationDecision:
    """Bind post-dispatch evidence to the preflight job and target transition."""
    if live.filename != preflight.filename or live.eventtime < preflight.eventtime:
        return ReconciliationDecision.AMBIGUOUS
    if operation is not ControlOperation.CANCEL and live.file_position < preflight.file_position:
        return ReconciliationDecision.AMBIGUOUS
    if live.eventtime > preflight.eventtime and live.phase is _TARGET_PHASES[operation]:
        return ReconciliationDecision.CONFIRMED
    return ReconciliationDecision.PENDING
