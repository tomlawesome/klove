from __future__ import annotations

from typing import Any

import pytest

from klove.domain.control import (
    CachedControlEvidence,
    ControlIntent,
    ControlOperation,
    LiveControlState,
    ReconciliationDecision,
    authorize_cached_intent,
    reconcile_postcondition,
    validate_cached_recheck,
    validate_live_preflight,
)
from klove.domain.discovery import discover_capabilities
from klove.domain.models import (
    JobHistoryStatus,
    JobIdentitySnapshot,
    PrinterPhase,
    PrinterSnapshot,
    initial_snapshot,
)


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
            "job": JobIdentitySnapshot(
                job_id="000001",
                filename="job.gcode",
                start_time=1_700_000_000.0,
                status=JobHistoryStatus.IN_PROGRESS,
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
        job_id="000001",
        job_start_time=1_700_000_000.0,
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
        (snapshot().model_copy(update={"job": None}), None, "job_identity_unavailable"),
        (
            snapshot().model_copy(
                update={
                    "job": JobIdentitySnapshot(
                        job_id="000001",
                        filename="job.gcode",
                        start_time=1_700_000_000.0,
                        status=JobHistoryStatus.COMPLETED,
                    )
                }
            ),
            None,
            "job_identity_unavailable",
        ),
        (
            snapshot().model_copy(
                update={
                    "job": JobIdentitySnapshot(
                        job_id="000001",
                        filename="other.gcode",
                        start_time=1_700_000_000.0,
                        status=JobHistoryStatus.IN_PROGRESS,
                    )
                }
            ),
            None,
            "job_identity_unavailable",
        ),
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
    cached = CachedControlEvidence(
        PrinterPhase.PRINTING, 10.0, "000001", 1_700_000_000.0, "job.gcode", 100
    )
    good = LiveControlState(
        10.0, PrinterPhase.PRINTING, "000001", 1_700_000_000.0, "job.gcode", 100
    )

    assert validate_live_preflight(cached, good) is None
    assert (
        validate_live_preflight(
            cached,
            LiveControlState(
                11.0, PrinterPhase.PAUSED, "000001", 1_700_000_000.0, "job.gcode", 100
            ),
        )
        == "preflight_state_mismatch"
    )
    assert (
        validate_live_preflight(
            cached,
            LiveControlState(
                11.0, PrinterPhase.PRINTING, "000002", 1_700_000_000.0, "job.gcode", 100
            ),
        )
        == "preflight_job_mismatch"
    )
    assert (
        validate_live_preflight(
            cached,
            LiveControlState(
                11.0,
                PrinterPhase.PRINTING,
                "000001",
                1_700_000_000.0,
                "job.gcode",
                100,
                JobHistoryStatus.CANCELLED,
            ),
        )
        == "preflight_job_mismatch"
    )
    assert (
        validate_live_preflight(
            cached,
            LiveControlState(
                11.0, PrinterPhase.PRINTING, "000001", 1_700_000_001.0, "job.gcode", 100
            ),
        )
        == "preflight_job_mismatch"
    )
    assert (
        validate_live_preflight(
            cached,
            LiveControlState(
                11.0,
                PrinterPhase.PRINTING,
                "000001",
                1_700_000_000.0,
                "other.gcode",
                100,
            ),
        )
        == "preflight_job_mismatch"
    )
    assert (
        validate_live_preflight(
            cached,
            LiveControlState(
                9.0, PrinterPhase.PRINTING, "000001", 1_700_000_000.0, "job.gcode", 100
            ),
        )
        == "preflight_stale"
    )
    assert (
        validate_live_preflight(
            cached,
            LiveControlState(
                11.0, PrinterPhase.PRINTING, "000001", 1_700_000_000.0, "job.gcode", 99
            ),
        )
        == "preflight_stale"
    )
    assert (
        validate_live_preflight(
            cached,
            LiveControlState(
                10.0, PrinterPhase.PRINTING, "000001", 1_700_000_000.0, "job.gcode", 101
            ),
        )
        == "preflight_stale"
    )


def test_cached_recheck_accepts_only_advances_bounded_by_preflight() -> None:
    initial = CachedControlEvidence(
        PrinterPhase.PRINTING, 10.0, "000001", 1_700_000_000.0, "job.gcode", 100
    )
    preflight = LiveControlState(
        11.0, PrinterPhase.PRINTING, "000001", 1_700_000_000.0, "job.gcode", 110
    )
    request = intent(snapshot())
    latest = snapshot().model_copy(
        update={
            "revision": 2,
            "eventtime": 11.0,
            "status": {
                "print_stats": {"filename": "job.gcode", "state": "printing"},
                "virtual_sdcard": {"file_position": 110},
            },
        }
    )

    assert validate_cached_recheck(request, initial, latest, preflight=preflight) is None
    assert (
        validate_cached_recheck(
            request,
            initial,
            snapshot().model_copy(update={"revision": 2}),
            preflight=preflight,
        )
        is None
    )
    assert validate_cached_recheck(request, initial, None, preflight=preflight) == "printer_unknown"
    assert (
        validate_cached_recheck(
            request,
            initial,
            latest.model_copy(update={"connected": False}),
            preflight=preflight,
        )
        == "state_token_mismatch"
    )
    changed_job = latest.model_copy(
        update={
            "job": latest.job.model_copy(update={"start_time": 1_700_000_001.0})
            if latest.job is not None
            else None
        }
    )
    assert (
        validate_cached_recheck(request, initial, changed_job, preflight=preflight)
        == "state_token_mismatch"
    )
    assert (
        validate_cached_recheck(
            request,
            initial,
            latest.model_copy(update={"control_revision": 1}),
            preflight=preflight,
        )
        == "state_token_mismatch"
    )


def test_cached_recheck_denies_every_unbounded_or_contradictory_advance() -> None:
    printing = CachedControlEvidence(
        PrinterPhase.PRINTING, 10.0, "000001", 1_700_000_000.0, "job.gcode", 100
    )
    paused = CachedControlEvidence(
        PrinterPhase.PAUSED, 10.0, "000001", 1_700_000_000.0, "job.gcode", 100
    )
    live_printing = LiveControlState(
        11.0, PrinterPhase.PRINTING, "000001", 1_700_000_000.0, "job.gcode", 110
    )

    def changed(
        *,
        phase: PrinterPhase = PrinterPhase.PRINTING,
        eventtime: float = 11.0,
        filename: str = "job.gcode",
        file_position: int = 110,
    ) -> PrinterSnapshot:
        return snapshot(phase).model_copy(
            update={
                "revision": 2,
                "eventtime": eventtime,
                "status": {
                    "print_stats": {"filename": filename, "state": phase.value},
                    "virtual_sdcard": {"file_position": file_position},
                },
            }
        )

    cases = (
        (
            ControlOperation.CANCEL,
            printing,
            changed(phase=PrinterPhase.PAUSED),
            live_printing,
        ),
        (ControlOperation.PAUSE, printing, changed(filename="other.gcode"), live_printing),
        (ControlOperation.PAUSE, printing, changed(eventtime=9.0), live_printing),
        (ControlOperation.PAUSE, printing, changed(file_position=99), live_printing),
        (
            ControlOperation.CANCEL,
            paused,
            changed(phase=PrinterPhase.PAUSED),
            live_printing,
        ),
        (
            ControlOperation.PAUSE,
            printing,
            changed(),
            LiveControlState(
                11.0,
                PrinterPhase.PRINTING,
                "000001",
                1_700_000_000.0,
                "other.gcode",
                110,
            ),
        ),
        (ControlOperation.PAUSE, printing, changed(eventtime=12.0), live_printing),
        (ControlOperation.PAUSE, printing, changed(file_position=111), live_printing),
        (
            ControlOperation.PAUSE,
            printing,
            changed(eventtime=10.0, file_position=101),
            live_printing,
        ),
        (
            ControlOperation.PAUSE,
            printing,
            changed(eventtime=11.0, file_position=109),
            live_printing,
        ),
    )
    for index, (operation, initial, latest, preflight) in enumerate(cases):
        request = ControlIntent(
            printer_id="voron",
            operation=operation,
            state_token=snapshot(initial.phase).state_token,
            idempotency_key=f"{index:08x}-0000-4000-8000-000000000000",
        )
        assert (
            validate_cached_recheck(request, initial, latest, preflight=preflight)
            == "state_token_mismatch"
        )


@pytest.mark.parametrize(
    ("operation", "target"),
    [
        (ControlOperation.PAUSE, PrinterPhase.PAUSED),
        (ControlOperation.RESUME, PrinterPhase.PRINTING),
        (ControlOperation.CANCEL, PrinterPhase.CANCELLED),
    ],
)
def test_reconciliation_confirms_only_target_phase_after_preflight(
    operation: ControlOperation, target: PrinterPhase
) -> None:
    preflight = LiveControlState(
        10.0, PrinterPhase.PRINTING, "000001", 1_700_000_000.0, "job.gcode", 100
    )
    assert (
        reconcile_postcondition(
            operation,
            LiveControlState(
                11.0,
                target,
                "000001",
                1_700_000_000.0,
                "job.gcode",
                100,
                JobHistoryStatus.CANCELLED
                if operation is ControlOperation.CANCEL
                else JobHistoryStatus.IN_PROGRESS,
            ),
            preflight=preflight,
        )
        is ReconciliationDecision.CONFIRMED
    )
    assert (
        reconcile_postcondition(
            operation,
            LiveControlState(
                10.0,
                target,
                "000001",
                1_700_000_000.0,
                "job.gcode",
                100,
                JobHistoryStatus.CANCELLED
                if operation is ControlOperation.CANCEL
                else JobHistoryStatus.IN_PROGRESS,
            ),
            preflight=preflight,
        )
        is ReconciliationDecision.PENDING
    )
    assert (
        reconcile_postcondition(
            operation,
            LiveControlState(11.0, PrinterPhase.ERROR, "000001", 1_700_000_000.0, "job.gcode", 100),
            preflight=preflight,
        )
        is ReconciliationDecision.PENDING
    )


def test_reconciliation_rejects_ambiguous_job_evidence() -> None:
    preflight = LiveControlState(
        10.0, PrinterPhase.PRINTING, "000001", 1_700_000_000.0, "job.gcode", 100
    )

    for live in (
        LiveControlState(11.0, PrinterPhase.PAUSED, "000002", 1_700_000_000.0, "job.gcode", 100),
        LiveControlState(11.0, PrinterPhase.PAUSED, "000001", 1_700_000_001.0, "job.gcode", 100),
        LiveControlState(11.0, PrinterPhase.PAUSED, "000001", 1_700_000_000.0, "other.gcode", 100),
        LiveControlState(9.0, PrinterPhase.PAUSED, "000001", 1_700_000_000.0, "job.gcode", 100),
        LiveControlState(11.0, PrinterPhase.PAUSED, "000001", 1_700_000_000.0, "job.gcode", 99),
        LiveControlState(10.0, PrinterPhase.PAUSED, "000001", 1_700_000_000.0, "job.gcode", 101),
    ):
        assert (
            reconcile_postcondition(ControlOperation.PAUSE, live, preflight=preflight)
            is ReconciliationDecision.AMBIGUOUS
        )

    assert (
        reconcile_postcondition(
            ControlOperation.CANCEL,
            LiveControlState(
                11.0,
                PrinterPhase.CANCELLED,
                "000001",
                1_700_000_000.0,
                "job.gcode",
                0,
                JobHistoryStatus.CANCELLED,
            ),
            preflight=preflight,
        )
        is ReconciliationDecision.CONFIRMED
    )
    assert (
        reconcile_postcondition(
            ControlOperation.CANCEL,
            LiveControlState(
                11.0,
                PrinterPhase.CANCELLED,
                "000001",
                1_700_000_000.0,
                "job.gcode",
                0,
            ),
            preflight=preflight,
        )
        is ReconciliationDecision.PENDING
    )
    assert (
        reconcile_postcondition(
            ControlOperation.PAUSE,
            LiveControlState(
                11.0,
                PrinterPhase.PAUSED,
                "000001",
                1_700_000_000.0,
                "job.gcode",
                100,
                JobHistoryStatus.COMPLETED,
            ),
            preflight=preflight,
        )
        is ReconciliationDecision.AMBIGUOUS
    )
    for live in (
        LiveControlState(11.0, PrinterPhase.CANCELLED, "000002", 1_700_000_000.0, "job.gcode", 0),
        LiveControlState(11.0, PrinterPhase.CANCELLED, "000001", 1_700_000_001.0, "job.gcode", 0),
        LiveControlState(11.0, PrinterPhase.CANCELLED, "000001", 1_700_000_000.0, "other.gcode", 0),
        LiveControlState(9.0, PrinterPhase.CANCELLED, "000001", 1_700_000_000.0, "job.gcode", 0),
    ):
        assert (
            reconcile_postcondition(ControlOperation.CANCEL, live, preflight=preflight)
            is ReconciliationDecision.AMBIGUOUS
        )
