from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from klove.domain.artifacts import (
    ArtifactIntake,
    ArtifactQualification,
    PlateSelection,
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
from klove.domain.start import StartConfirmation, StartOperationResult, StartState

from ..start_helpers import OPERATION_ID, history_job, safety_profile, verified_upload


def request(**updates: object) -> DispatchRequest:
    target = target_for_safety_profile(safety_profile())
    values: dict[str, object] = {
        "operation_id": OPERATION_ID,
        "idempotency_key": "33333333-3333-4333-8333-333333333333",
        "printer_uuid": target.printer_uuid,
        "slicer_profile_id": target.slicer_profile_id,
        "safety_profile_generation": target.safety_profile_generation,
        "safety_profile_fingerprint": target.safety_profile_fingerprint,
        "selected_plate": PlateSelection(plate_id="1", archive_path="Metadata/plate_1.gcode"),
        "archive_sha256": "sha256:" + hashlib.sha256(b"archive").hexdigest(),
        "archive_size_bytes": len(b"archive"),
    }
    values.update(updates)
    return DispatchRequest.model_validate(values)


def record(**updates: object) -> DispatchJournalRecord:
    submitted = request()
    values: dict[str, object] = {
        "grant": DispatchGrant(principal_id="actor", printer_uuid=submitted.printer_uuid),
        "request": submitted,
        "state": DispatchState.RECEIVING,
    }
    values.update(updates)
    return DispatchJournalRecord.model_validate(values)


def test_request_derives_only_its_operation_bound_artifact_and_target() -> None:
    submitted = request()

    assert submitted.artifact.artifact_id == submitted.operation_id
    assert submitted.target.printer_uuid == submitted.printer_uuid
    with pytest.raises(ValidationError):
        request(archive_size_bytes=0)


def test_journal_model_rejects_substituted_qualification_or_verified_upload() -> None:
    with pytest.raises(ValidationError, match="qualification"):
        record(state=DispatchState.UPLOADING, qualification=verified_upload().qualification)
    with pytest.raises(ValidationError, match="verified upload"):
        record(verified=verified_upload())


@pytest.mark.parametrize(
    "updates",
    [
        {
            "grant": DispatchGrant(
                principal_id="actor", printer_uuid="22222222-2222-4222-8222-222222222222"
            )
        },
        {"state": DispatchState.UPLOADING},
        {"state": DispatchState.FAILED},
        {
            "state": DispatchState.CANCELLED,
            "failure": DispatchFailure(code=DispatchFailureCode.SOURCE_INVALID),
        },
        {
            "state": DispatchState.FAILED,
            "failure": DispatchFailure(code=DispatchFailureCode.CANCELLED),
        },
        {
            "state": DispatchState.FAILED,
            "failure": DispatchFailure(code=DispatchFailureCode.ACCESS_DENIED),
        },
        {
            "state": DispatchState.OUTCOME_UNKNOWN,
            "failure": DispatchFailure(code=DispatchFailureCode.DISPATCH_DISABLED),
        },
        {
            "failure": DispatchFailure(code=DispatchFailureCode.SOURCE_INVALID),
        },
    ],
)
def test_journal_model_rejects_substitution_and_impossible_lifecycle_payloads(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        record(**updates)


def test_result_projection_and_result_model_reject_every_mismatched_payload() -> None:
    terminal = record(
        state=DispatchState.FAILED,
        failure=DispatchFailure(code=DispatchFailureCode.SOURCE_INVALID),
    )
    result = result_for_record(terminal)
    assert result.failure == terminal.failure
    with pytest.raises(ValidationError):
        DispatchOperationResult.model_validate(
            result.model_dump() | {"printer_uuid": "22222222-2222-4222-8222-222222222222"}
        )
    with pytest.raises(ValidationError):
        DispatchOperationResult.model_validate(result.model_dump() | {"failure": None})
    with pytest.raises(ValidationError):
        DispatchOperationResult.model_validate(
            result.model_dump() | {"state": DispatchState.ACCEPTED}
        )
    for state, code in (
        (DispatchState.FAILED, DispatchFailureCode.CANCELLED),
        (DispatchState.OUTCOME_UNKNOWN, DispatchFailureCode.DISPATCH_DISABLED),
        (DispatchState.CANCELLED, DispatchFailureCode.SOURCE_INVALID),
    ):
        with pytest.raises(ValidationError):
            DispatchOperationResult.model_validate(
                result.model_dump()
                | {"state": state, "failure": DispatchFailure(code=code).model_dump()}
            )


def test_models_reject_substituted_start_evidence_and_unbound_result_path() -> None:
    verified = verified_upload()
    source_qualification = verified.qualification
    qualification = ArtifactQualification(
        approval_id=source_qualification.approval_id,
        authority_id=source_qualification.authority_id,
        operation_id=source_qualification.operation_id,
        idempotency_key=source_qualification.idempotency_key,
        artifact=ArtifactIntake(
            artifact_id=source_qualification.operation_id,
            format=source_qualification.artifact.format,
            archive_sha256=source_qualification.artifact.archive_sha256,
            compressed_size_bytes=source_qualification.artifact.compressed_size_bytes,
        ),
        selected_plate=source_qualification.selected_plate,
        target=source_qualification.target,
    )
    verified = verified.model_copy(update={"qualification": qualification})
    evidenced_request = DispatchRequest(
        operation_id=qualification.operation_id,
        idempotency_key=qualification.idempotency_key,
        printer_uuid=qualification.target.printer_uuid,
        slicer_profile_id=qualification.target.slicer_profile_id,
        safety_profile_generation=qualification.target.safety_profile_generation,
        safety_profile_fingerprint=qualification.target.safety_profile_fingerprint,
        selected_plate=PlateSelection(
            plate_id=qualification.selected_plate.plate_id,
            archive_path=qualification.selected_plate.archive_path,
        ),
        archive_sha256=qualification.artifact.archive_sha256,
        archive_size_bytes=qualification.artifact.compressed_size_bytes,
    )
    wrong_path = "klove/55555555-5555-4555-8555-555555555555.gcode"
    substituted_start = StartOperationResult(
        operation_id=evidenced_request.operation_id,
        idempotency_key=evidenced_request.idempotency_key,
        state=StartState.CONFIRMED,
        confirmation=StartConfirmation(
            path=wrong_path,
            eventtime=2,
            phase=PrinterPhase.PRINTING,
            file_position=1,
            job=history_job(filename=wrong_path, status=JobHistoryStatus.IN_PROGRESS),
        ),
    )

    with pytest.raises(ValidationError, match="start evidence"):
        DispatchJournalRecord(
            grant=DispatchGrant(principal_id="actor", printer_uuid=evidenced_request.printer_uuid),
            request=evidenced_request,
            state=DispatchState.PRINTING,
            qualification=qualification,
            verified=verified,
            start=substituted_start,
        )
    mismatched_start = StartOperationResult(
        operation_id="55555555-5555-4555-8555-555555555555",
        idempotency_key=evidenced_request.idempotency_key,
        state=StartState.CONFIRMED,
        confirmation=StartConfirmation(
            path=verified.path,
            eventtime=2,
            phase=PrinterPhase.PRINTING,
            file_position=1,
            job=history_job(filename=verified.path, status=JobHistoryStatus.IN_PROGRESS),
        ),
    )
    with pytest.raises(ValidationError, match="start result"):
        DispatchJournalRecord(
            grant=DispatchGrant(principal_id="actor", printer_uuid=evidenced_request.printer_uuid),
            request=evidenced_request,
            state=DispatchState.PRINTING,
            qualification=qualification,
            verified=verified,
            start=mismatched_start,
        )
    with pytest.raises(ValidationError, match="remote path"):
        DispatchOperationResult(
            operation_id=evidenced_request.operation_id,
            printer_uuid=evidenced_request.printer_uuid,
            state=DispatchState.ACCEPTED,
            target=evidenced_request.target,
            archive_sha256=evidenced_request.archive_sha256,
            archive_size_bytes=evidenced_request.archive_size_bytes,
            remote_path="klove/55555555-5555-4555-8555-555555555555.gcode",
        )
    with pytest.raises(ValidationError):
        DispatchOperationResult(
            operation_id=evidenced_request.operation_id,
            printer_uuid=evidenced_request.printer_uuid,
            state=DispatchState.ACCEPTED,
            target=evidenced_request.target,
            archive_sha256=evidenced_request.archive_sha256,
            archive_size_bytes=4 * 1024 * 1024 * 1024 + 1,
        )


def test_models_accept_every_durable_terminal_and_evidenced_success_shape() -> None:
    verified = verified_upload()
    source_qualification = verified.qualification
    qualification = ArtifactQualification(
        approval_id=source_qualification.approval_id,
        authority_id=source_qualification.authority_id,
        operation_id=source_qualification.operation_id,
        idempotency_key=source_qualification.idempotency_key,
        artifact=ArtifactIntake(
            artifact_id=source_qualification.operation_id,
            format=source_qualification.artifact.format,
            archive_sha256=source_qualification.artifact.archive_sha256,
            compressed_size_bytes=source_qualification.artifact.compressed_size_bytes,
        ),
        selected_plate=source_qualification.selected_plate,
        target=source_qualification.target,
    )
    verified = verified.model_copy(update={"qualification": qualification})
    submitted = DispatchRequest(
        operation_id=qualification.operation_id,
        idempotency_key=qualification.idempotency_key,
        printer_uuid=qualification.target.printer_uuid,
        slicer_profile_id=qualification.target.slicer_profile_id,
        safety_profile_generation=qualification.target.safety_profile_generation,
        safety_profile_fingerprint=qualification.target.safety_profile_fingerprint,
        selected_plate=PlateSelection(
            plate_id=qualification.selected_plate.plate_id,
            archive_path=qualification.selected_plate.archive_path,
        ),
        archive_sha256=qualification.artifact.archive_sha256,
        archive_size_bytes=qualification.artifact.compressed_size_bytes,
    )
    granted = DispatchGrant(principal_id="actor", printer_uuid=submitted.printer_uuid)
    started = StartOperationResult(
        operation_id=submitted.operation_id,
        idempotency_key=submitted.idempotency_key,
        state=StartState.CONFIRMED,
        confirmation=StartConfirmation(
            path=verified.path,
            eventtime=2,
            phase=PrinterPhase.PRINTING,
            file_position=1,
            job=history_job(filename=verified.path, status=JobHistoryStatus.IN_PROGRESS),
        ),
    )
    verified_record = DispatchJournalRecord(
        grant=granted,
        request=submitted,
        state=DispatchState.VERIFIED,
        qualification=qualification,
        verified=verified,
    )
    printing = DispatchJournalRecord(
        grant=granted,
        request=submitted,
        state=DispatchState.PRINTING,
        qualification=qualification,
        verified=verified,
        start=started,
    )
    unknown = DispatchJournalRecord(
        grant=granted,
        request=submitted,
        state=DispatchState.OUTCOME_UNKNOWN,
        failure=DispatchFailure(code=DispatchFailureCode.START_AMBIGUOUS),
    )
    cancelled = DispatchJournalRecord(
        grant=granted,
        request=submitted,
        state=DispatchState.CANCELLED,
        failure=DispatchFailure(code=DispatchFailureCode.CANCELLED),
    )

    assert verified_record.verified == verified
    assert printing.start == started
    assert result_for_record(unknown).failure == unknown.failure
    assert result_for_record(cancelled).failure == cancelled.failure
    assert result_for_record(verified_record).failure is None
