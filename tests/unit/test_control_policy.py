from __future__ import annotations

from typing import Any

import pytest

from klove.domain.control import (
    CachedControlEvidence,
    ControlIntent,
    ControlOperation,
    LiveControlState,
    authorize_cached_intent,
    is_confirmed,
    validate_live_preflight,
)
from klove.domain.discovery import discover_capabilities
from klove.domain.models import PrinterPhase, PrinterSnapshot, initial_snapshot


def snapshot(
    phase: PrinterPhase = PrinterPhase.PRINTING,
    *,
    filename: object = "job.gcode",
    file_position: object = 100,
) -> PrinterSnapshot:
    return initial_snapshot("voron").model_copy(
        update={
            "revision": 1,
            "connected": True,
            "phase": phase,
            "reason": "observed",
            "eventtime": 10.0,
            "capabilities": discover_capabilities(
                {"pause_resume", "print_stats", "virtual_sdcard"}
            ),
            "status": {
                "print_stats": {"filename": filename, "state": phase.value},
                "virtual_sdcard": {"file_position": file_position},
            },
        }
    )


def intent(
    current: PrinterSnapshot,
    operation: ControlOperation = ControlOperation.PAUSE,
    **overrides: Any,
) -> ControlIntent:
    values: dict[str, object] = {
        "printer_id": "voron",
        "operation": operation,
        "state_token": current.state_token,
        "idempotency_key": "00000000-0000-4000-8000-000000000000",
    }
    values.update(overrides)
    return ControlIntent(**values)  # type: ignore[arg-type]


def test_valid_cached_job_evidence_is_returned() -> None:
    current = snapshot()

    assert authorize_cached_intent(intent(current), current) == CachedControlEvidence(
        phase=PrinterPhase.PRINTING,
        eventtime=10.0,
        filename="job.gcode",
        file_position=100,
    )
    paused = snapshot(PrinterPhase.PAUSED)
    assert isinstance(
        authorize_cached_intent(intent(paused, ControlOperation.RESUME), paused),
        CachedControlEvidence,
    )
    assert isinstance(
        authorize_cached_intent(intent(paused, ControlOperation.CANCEL), paused),
        CachedControlEvidence,
    )


@pytest.mark.parametrize(
    ("current", "state_token_override", "code"),
    [
        (None, None, "printer_unknown"),
        (snapshot(), "0" * 64, "state_token_mismatch"),
        (snapshot().model_copy(update={"connected": False}), None, "printer_unavailable"),
        (snapshot().model_copy(update={"reason": "stale"}), None, "printer_unavailable"),
        (snapshot().model_copy(update={"eventtime": None}), None, "printer_unavailable"),
        (snapshot().model_copy(update={"capabilities": None}), None, "capability_unavailable"),
        (
            snapshot().model_copy(update={"capabilities": discover_capabilities({"print_stats"})}),
            None,
            "capability_unavailable",
        ),
        (snapshot(PrinterPhase.IDLE), None, "transition_denied"),
        (snapshot(filename=""), None, "job_identity_unavailable"),
        (snapshot(filename=1), None, "job_identity_unavailable"),
        (snapshot(file_position=True), None, "job_identity_unavailable"),
        (snapshot(file_position=1.5), None, "job_identity_unavailable"),
        (snapshot(file_position=-1), None, "job_identity_unavailable"),
    ],
)
def test_missing_or_ambiguous_cached_evidence_is_denied(
    current: PrinterSnapshot | None,
    state_token_override: str | None,
    code: str,
) -> None:
    basis = current or snapshot()
    request = intent(basis)
    if state_token_override is not None:
        request = ControlIntent(
            request.printer_id,
            request.operation,
            state_token_override,
            request.idempotency_key,
        )

    assert authorize_cached_intent(request, current) == code


def test_live_preflight_must_match_job_and_move_forward() -> None:
    cached = CachedControlEvidence(PrinterPhase.PRINTING, 10.0, "job.gcode", 100)
    good = LiveControlState(10.0, PrinterPhase.PRINTING, "job.gcode", 100)

    assert validate_live_preflight(cached, good) is None
    assert (
        validate_live_preflight(
            cached, LiveControlState(11.0, PrinterPhase.PAUSED, "job.gcode", 100)
        )
        == "preflight_state_mismatch"
    )
    assert (
        validate_live_preflight(
            cached, LiveControlState(11.0, PrinterPhase.PRINTING, "other.gcode", 100)
        )
        == "preflight_job_mismatch"
    )
    assert (
        validate_live_preflight(
            cached, LiveControlState(9.0, PrinterPhase.PRINTING, "job.gcode", 100)
        )
        == "preflight_stale"
    )
    assert (
        validate_live_preflight(
            cached, LiveControlState(11.0, PrinterPhase.PRINTING, "job.gcode", 99)
        )
        == "preflight_stale"
    )


@pytest.mark.parametrize(
    ("operation", "target"),
    [
        (ControlOperation.PAUSE, PrinterPhase.PAUSED),
        (ControlOperation.RESUME, PrinterPhase.PRINTING),
        (ControlOperation.CANCEL, PrinterPhase.CANCELLED),
    ],
)
def test_confirmation_requires_target_phase_after_preflight(
    operation: ControlOperation, target: PrinterPhase
) -> None:
    assert is_confirmed(
        operation,
        LiveControlState(11.0, target, "job.gcode", 100),
        after_eventtime=10.0,
    )
    assert not is_confirmed(
        operation,
        LiveControlState(10.0, target, "job.gcode", 100),
        after_eventtime=10.0,
    )
    assert not is_confirmed(
        operation,
        LiveControlState(11.0, PrinterPhase.ERROR, "job.gcode", 100),
        after_eventtime=10.0,
    )
