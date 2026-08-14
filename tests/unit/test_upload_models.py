from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from klove.domain.artifacts import ArtifactQualification, ArtifactTargetApproval
from klove.domain.upload import (
    MoonrakerGcodeMetadata,
    MoonrakerUploadReceipt,
    RemoteFileDigest,
    UploadBoundary,
    UploadFailure,
    UploadFailureCode,
    UploadOperationResult,
    UploadState,
    VerifiedUpload,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "artifacts"
PATH = "klove/22222222-2222-4222-8222-222222222222.gcode"


def qualification() -> ArtifactQualification:
    approval = ArtifactTargetApproval.model_validate_json(
        (FIXTURES / "accepted-approval.json").read_text(encoding="utf-8")
    )
    return ArtifactQualification(
        approval_id=approval.approval_id,
        authority_id=approval.authority_id,
        operation_id=approval.operation_id,
        idempotency_key=approval.idempotency_key,
        artifact=approval.artifact,
        selected_plate=approval.selected_plate,
        target=approval.target,
    )


def receipt(**updates: object) -> MoonrakerUploadReceipt:
    values: dict[str, object] = {
        "path": PATH,
        "size_bytes": qualification().selected_plate.gcode_size_bytes,
        "modified": Decimal("1700000000.25"),
    }
    values.update(updates)
    return MoonrakerUploadReceipt.model_validate(values)


def metadata(**updates: object) -> MoonrakerGcodeMetadata:
    values: dict[str, object] = {
        "path": PATH,
        "size_bytes": qualification().selected_plate.gcode_size_bytes,
        "modified": receipt().modified,
        "metadata_uuid": "44444444-4444-4444-8444-444444444444",
        "file_processors": (),
        "nozzle_diameter_micrometres": 400,
        "gcode_start_byte": 10,
        "gcode_end_byte": qualification().selected_plate.gcode_size_bytes,
        "job_id": None,
        "print_start_time": None,
    }
    values.update(updates)
    return MoonrakerGcodeMetadata.model_validate(values)


def remote(**updates: object) -> RemoteFileDigest:
    values: dict[str, object] = {
        "size_bytes": qualification().selected_plate.gcode_size_bytes,
        "sha256": qualification().selected_plate.gcode_sha256,
    }
    values.update(updates)
    return RemoteFileDigest.model_validate(values)


def verified(**updates: object) -> VerifiedUpload:
    values: dict[str, object] = {
        "path": PATH,
        "qualification": qualification(),
        "receipt": receipt(),
        "metadata": metadata(),
        "remote_file": remote(),
    }
    values.update(updates)
    return VerifiedUpload.model_validate(values)


def test_verified_upload_and_terminal_result_bind_every_identity() -> None:
    evidence = verified()
    result = UploadOperationResult(
        operation_id=qualification().operation_id,
        idempotency_key=qualification().idempotency_key,
        state=UploadState.VERIFIED,
        verified=evidence,
    )

    assert result.verified == evidence
    assert evidence.root == "gcodes"


@pytest.mark.parametrize(
    ("start", "end", "size", "message"),
    [
        (10, 10, 100, "not increasing"),
        (11, 10, 100, "not increasing"),
        (10, 101, 100, "exceed the file"),
    ],
)
def test_metadata_offsets_must_describe_one_bounded_command_region(
    start: int, end: int, size: int, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        metadata(gcode_start_byte=start, gcode_end_byte=end, size_bytes=size)


@pytest.mark.parametrize(
    "updates",
    [
        {"path": "klove/55555555-5555-4555-8555-555555555555.gcode"},
        {"receipt": receipt(path="klove/55555555-5555-4555-8555-555555555555.gcode")},
        {"metadata": metadata(path="klove/55555555-5555-4555-8555-555555555555.gcode")},
        {"receipt": receipt(size_bytes=qualification().selected_plate.gcode_size_bytes + 1)},
        {"metadata": metadata(size_bytes=qualification().selected_plate.gcode_size_bytes + 1)},
        {"remote_file": remote(size_bytes=qualification().selected_plate.gcode_size_bytes + 1)},
        {"metadata": metadata(modified=Decimal("1700000000.5"))},
        {"remote_file": remote(sha256="sha256:" + "0" * 64)},
        {"metadata": metadata(file_processors=("preprocess_cancellation",))},
    ],
)
def test_verified_upload_rejects_every_unbound_evidence(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        verified(**updates)


def test_operation_result_requires_exact_state_payload_and_identity() -> None:
    operation = qualification().operation_id
    key = qualification().idempotency_key
    failure = UploadFailure(
        boundary=UploadBoundary.METADATA,
        code=UploadFailureCode.METADATA_TIMEOUT,
    )
    denied = UploadOperationResult(
        operation_id=operation,
        idempotency_key=key,
        state=UploadState.DENIED,
        failure=failure,
    )
    unknown = denied.model_copy(update={"state": UploadState.OUTCOME_UNKNOWN})
    assert denied.failure == unknown.failure

    invalid: tuple[dict[str, object], ...] = (
        {"state": UploadState.VERIFIED},
        {"state": UploadState.VERIFIED, "verified": verified(), "failure": failure},
        {"state": UploadState.DENIED},
        {"state": UploadState.DENIED, "failure": failure, "verified": verified()},
        {
            "state": UploadState.VERIFIED,
            "verified": verified().model_copy(
                update={
                    "qualification": qualification().model_copy(
                        update={"idempotency_key": "55555555-5555-4555-8555-555555555555"}
                    )
                }
            ),
        },
    )
    for values in invalid:
        with pytest.raises(ValidationError):
            UploadOperationResult.model_validate(
                {"operation_id": operation, "idempotency_key": key, **values}
            )
