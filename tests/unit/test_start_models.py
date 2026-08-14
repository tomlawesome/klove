from __future__ import annotations

import pytest
from pydantic import ValidationError

from klove.domain.models import JobHistoryStatus, PrinterPhase
from klove.domain.start import (
    StartBoundary,
    StartConfirmation,
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

from ..start_helpers import history_job, observation, preflight, verified_upload


def confirmation(**updates: object) -> StartConfirmation:
    values: dict[str, object] = {
        "path": verified_upload().path,
        "eventtime": 11.0,
        "phase": PrinterPhase.PRINTING,
        "file_position": 100,
        "job": history_job(),
    }
    values.update(updates)
    return StartConfirmation.model_validate(values)


def record(**updates: object) -> StartJournalRecord:
    verified = verified_upload()
    values: dict[str, object] = {
        "operation_id": verified.qualification.operation_id,
        "idempotency_key": verified.qualification.idempotency_key,
        "printer_uuid": verified.qualification.target.printer_uuid,
        "verified": verified,
        "preflight": preflight(),
        "state": StartJournalState.DISPATCHING,
    }
    values.update(updates)
    return StartJournalRecord.model_validate(values)


def test_start_models_accept_exact_idle_preflight_and_confirmation() -> None:
    idle = preflight()
    confirmed = confirmation()
    result = StartOperationResult(
        operation_id=verified_upload().qualification.operation_id,
        idempotency_key=verified_upload().qualification.idempotency_key,
        state=StartState.CONFIRMED,
        confirmation=confirmed,
    )

    assert idle.observation.phase is PrinterPhase.IDLE
    assert result.confirmation == confirmed


def test_observation_and_preflight_reject_missing_or_non_idle_state() -> None:
    with pytest.raises(ValidationError, match="require a filename"):
        StartObservation(
            eventtime=1,
            phase=PrinterPhase.PRINTING,
            filename="",
            file_position=0,
        )
    with pytest.raises(ValidationError, match="requires an idle"):
        StartPreflight(observation=observation())


@pytest.mark.parametrize(
    "updates",
    [
        {"path": "klove/55555555-5555-4555-8555-555555555555.gcode"},
        {"phase": PrinterPhase.COMPLETED},
    ],
)
def test_confirmation_requires_exact_filename_and_history_phase(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        confirmation(**updates)


def test_operation_result_requires_exact_state_payload() -> None:
    verified = verified_upload()
    failure = StartFailure(
        boundary=StartBoundary.TRANSPORT,
        code=StartFailureCode.TRANSPORT_AMBIGUOUS,
    )
    denied = StartOperationResult(
        operation_id=verified.qualification.operation_id,
        idempotency_key=verified.qualification.idempotency_key,
        state=StartState.DENIED,
        failure=failure,
    )
    assert denied.failure == failure

    invalid = (
        {"state": StartState.CONFIRMED},
        {"state": StartState.CONFIRMED, "confirmation": confirmation(), "failure": failure},
        {"state": StartState.DENIED},
        {"state": StartState.DENIED, "failure": failure, "confirmation": confirmation()},
    )
    for values in invalid:
        with pytest.raises(ValidationError):
            StartOperationResult.model_validate(
                {
                    "operation_id": verified.qualification.operation_id,
                    "idempotency_key": verified.qualification.idempotency_key,
                    **values,
                }
            )


@pytest.mark.parametrize("field", ["operation_id", "idempotency_key", "printer_uuid"])
def test_journal_record_rejects_identity_aliases(field: str) -> None:
    updates = {field: "55555555-5555-4555-8555-555555555555"}
    with pytest.raises(ValidationError, match="identity"):
        record(**updates)


def test_journal_record_requires_the_exact_state_payload() -> None:
    failure = StartFailure(
        boundary=StartBoundary.TRANSPORT,
        code=StartFailureCode.TRANSPORT_AMBIGUOUS,
    )
    unknown = record(state=StartJournalState.OUTCOME_UNKNOWN, failure=failure)
    confirmed = record(state=StartJournalState.CONFIRMED, confirmation=confirmation())
    assert unknown.failure == failure
    assert confirmed.confirmation is not None

    invalid = (
        {"state": StartJournalState.CONFIRMED},
        {
            "state": StartJournalState.CONFIRMED,
            "confirmation": confirmation(),
            "failure": failure,
        },
        {"state": StartJournalState.OUTCOME_UNKNOWN},
        {
            "state": StartJournalState.OUTCOME_UNKNOWN,
            "failure": failure,
            "confirmation": confirmation(),
        },
        {"state": StartJournalState.DISPATCHING, "failure": failure},
        {"state": StartJournalState.DISPATCHING, "confirmation": confirmation()},
    )
    for updates in invalid:
        with pytest.raises(ValidationError):
            record(**updates)


@pytest.mark.parametrize(
    ("status", "live_phase", "expected_phase"),
    [
        (JobHistoryStatus.IN_PROGRESS, PrinterPhase.PRINTING, PrinterPhase.PRINTING),
        (JobHistoryStatus.IN_PROGRESS, PrinterPhase.PAUSED, PrinterPhase.PAUSED),
        (JobHistoryStatus.IN_PROGRESS, PrinterPhase.IDLE, PrinterPhase.PRINTING),
        (JobHistoryStatus.COMPLETED, PrinterPhase.IDLE, PrinterPhase.COMPLETED),
        (JobHistoryStatus.CANCELLED, PrinterPhase.IDLE, PrinterPhase.CANCELLED),
        (JobHistoryStatus.ERROR, PrinterPhase.IDLE, PrinterPhase.ERROR),
        (JobHistoryStatus.KLIPPY_SHUTDOWN, PrinterPhase.IDLE, PrinterPhase.ERROR),
        (JobHistoryStatus.KLIPPY_DISCONNECT, PrinterPhase.IDLE, PrinterPhase.ERROR),
        (JobHistoryStatus.INTERRUPTED, PrinterPhase.IDLE, PrinterPhase.ERROR),
        (JobHistoryStatus.SERVER_EXIT, PrinterPhase.IDLE, PrinterPhase.ERROR),
    ],
)
def test_reconciliation_confirms_new_exact_history_across_live_and_restart_phases(
    status: JobHistoryStatus,
    live_phase: PrinterPhase,
    expected_phase: PrinterPhase,
) -> None:
    job = history_job(status=status)
    live = observation(
        phase=live_phase,
        filename=verified_upload().path if live_phase is not PrinterPhase.IDLE else "",
        latest_job=job,
    )

    decision, evidence = reconcile_start(verified_upload(), preflight(), live)

    assert decision is StartReconciliationDecision.CONFIRMED
    assert evidence is not None
    assert evidence.phase is expected_phase
    assert evidence.job == job


@pytest.mark.parametrize(
    "reason",
    ["eventtime", "same_job", "older_time", "live_filename", "live_phase"],
)
def test_reconciliation_rejects_replay_or_contradictory_live_evidence(reason: str) -> None:
    prior = history_job(job_id="000001", start_time=1_700_000_050.0)
    current = history_job()
    live = observation(latest_job=current)
    if reason == "eventtime":
        live = observation(eventtime=preflight(prior_job=prior).observation.eventtime)
    elif reason == "same_job":
        current = prior.model_copy(update={"filename": verified_upload().path})
        live = observation(latest_job=current)
    elif reason == "older_time":
        current = history_job(job_id="000002", start_time=prior.start_time)
        live = observation(latest_job=current)
    elif reason == "live_filename":
        live = observation(filename="other.gcode", latest_job=current)
    else:
        current = history_job(status=JobHistoryStatus.COMPLETED)
        live = observation(phase=PrinterPhase.PRINTING, latest_job=current)

    decision, evidence = reconcile_start(verified_upload(), preflight(prior_job=prior), live)

    assert decision is StartReconciliationDecision.AMBIGUOUS
    assert evidence is None


def test_reconciliation_distinguishes_unchanged_idle_from_other_job() -> None:
    prior = history_job(job_id="000001", filename="previous.gcode", start_time=1_700_000_050.0)
    idle = observation(
        phase=PrinterPhase.IDLE,
        filename="previous.gcode",
        file_position=0,
        latest_job=prior,
    )
    decision, evidence = reconcile_start(verified_upload(), preflight(prior_job=prior), idle)
    assert decision is StartReconciliationDecision.PENDING
    assert evidence is None

    other = idle.model_copy(
        update={
            "latest_job": history_job(
                job_id="000003", filename="other.gcode", start_time=1_700_000_200.0
            )
        }
    )
    decision, evidence = reconcile_start(verified_upload(), preflight(prior_job=prior), other)
    assert decision is StartReconciliationDecision.AMBIGUOUS
    assert evidence is None
