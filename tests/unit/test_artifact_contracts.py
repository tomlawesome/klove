from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from klove.domain.artifacts import (
    ArtifactBoundary,
    ArtifactFailure,
    ArtifactFailureCode,
    ArtifactIntent,
    ArtifactLimits,
    ArtifactOperationResult,
    ArtifactOperationState,
    ArtifactTarget,
    ArtifactValidationEvidence,
    SelectedPlate,
    assess_artifact_intent,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "artifacts"


def load_fixture(model: type[BaseModel], name: str) -> BaseModel:
    return model.model_validate_json((FIXTURES / name).read_text(encoding="utf-8"))


def intent() -> ArtifactIntent:
    return ArtifactIntent.model_validate_json(
        (FIXTURES / "accepted-intent.json").read_text(encoding="utf-8")
    )


def evidence() -> ArtifactValidationEvidence:
    return ArtifactValidationEvidence.model_validate_json(
        (FIXTURES / "accepted-evidence.json").read_text(encoding="utf-8")
    )


def assert_denied(
    result: ArtifactOperationResult,
    boundary: ArtifactBoundary,
    code: ArtifactFailureCode,
) -> None:
    assert result.state is ArtifactOperationState.DENIED
    assert result.failure == ArtifactFailure(boundary=boundary, code=code)


@pytest.mark.parametrize(
    ("model", "name"),
    [
        (ArtifactIntent, "accepted-intent.json"),
        (ArtifactValidationEvidence, "accepted-evidence.json"),
        (ArtifactOperationResult, "accepted-denial.json"),
    ],
)
def test_accepted_contract_fixtures_are_strict_and_versioned(
    model: type[BaseModel], name: str
) -> None:
    result = load_fixture(model, name)

    assert result.model_dump(mode="json")["contract_version"] == "1"


@pytest.mark.parametrize(
    ("model", "name"),
    [
        (ArtifactIntent, "rejected-intake.json"),
        (ArtifactIntent, "rejected-selection.json"),
        (ArtifactIntent, "rejected-target.json"),
        (ArtifactIntent, "rejected-operation.json"),
        (ArtifactValidationEvidence, "rejected-evidence.json"),
        (ArtifactValidationEvidence, "rejected-evidence-ratio.json"),
        (ArtifactOperationResult, "rejected-result.json"),
        (ArtifactIntent, "rejected-unknown-field.json"),
        (ArtifactIntent, "rejected-version.json"),
    ],
)
def test_rejected_contract_fixtures_cover_each_boundary(model: type[BaseModel], name: str) -> None:
    with pytest.raises(ValidationError):
        load_fixture(model, name)


def test_contract_schema_is_closed_and_json_aliases_are_rejected() -> None:
    schema = ArtifactIntent.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["contract_version"]["const"] == "1"
    assert all(
        schema["$defs"][name]["additionalProperties"] is False
        for name in ("ArtifactIntake", "ArtifactTarget", "PlateSelection")
    )

    mutations: tuple[tuple[str, str, object], ...] = (
        ("artifact", "compressed_size_bytes", True),
        ("artifact", "artifact_id", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
        ("artifact", "format", "zip"),
        ("target", "printer_id", "Voron-24"),
    )
    for owner_name, field, value in mutations:
        request = intent().model_dump(mode="json")
        request[owner_name][field] = value
        with pytest.raises(ValidationError):
            ArtifactIntent.model_validate_json(json.dumps(request))

    request = intent().model_dump(mode="json")
    request["contract_version"] = 1
    with pytest.raises(ValidationError):
        ArtifactIntent.model_validate_json(json.dumps(request))


def test_contract_models_reject_aliases_and_internal_contradictions() -> None:
    current = evidence()
    data = current.model_dump(mode="json")
    data["selected_plate"]["gcode_size_bytes"] = data["archive_expanded_bytes"] + 1
    with pytest.raises(ValidationError, match="exceeds expanded archive"):
        ArtifactValidationEvidence.model_validate_json(json.dumps(data))

    for field, value in (
        ("compressed_bytes", 0),
        ("compressed_bytes", current.artifact.compressed_size_bytes + 1),
        ("expanded_bytes", current.archive_expanded_bytes + 1),
    ):
        data = current.model_dump(mode="json")
        data["highest_ratio_entry"][field] = value
        with pytest.raises(ValidationError):
            ArtifactValidationEvidence.model_validate_json(json.dumps(data))

    for path in (
        "/Metadata/plate.gcode",
        "Metadata\\plate.gcode",
        "Metadata//plate.gcode",
        "Metadata/./plate.gcode",
        "Metadata/../plate.gcode",
        "Metadata/ plate.gcode",
        "Metadata/plate .gcode",
        "Metadata/plate.txt",
        "Metadata/plate.gcode ",
        "Metadata/plate\u007f.gcode",
        "Metadata/plate-\u202e.gcode",
    ):
        plate = current.selected_plate.model_dump(mode="json")
        plate["archive_path"] = path
        with pytest.raises(ValidationError):
            SelectedPlate.model_validate_json(json.dumps(plate))

    for field in ("plate_id", "slicer_profile_id"):
        request = intent().model_dump(mode="json")
        owner: dict[str, Any] = (
            request["selected_plate"] if field == "plate_id" else request["target"]
        )
        owner[field] = "bad\nvalue"
        with pytest.raises(ValidationError):
            ArtifactIntent.model_validate_json(json.dumps(request))


def test_operation_result_requires_failure_only_for_denial() -> None:
    operation_id = intent().operation_id
    received = ArtifactOperationResult(
        contract_version="1",
        operation_id=operation_id,
        state=ArtifactOperationState.RECEIVED,
    )
    assert received.failure is None

    with pytest.raises(ValidationError, match="require a failure"):
        ArtifactOperationResult(
            contract_version="1",
            operation_id=operation_id,
            state=ArtifactOperationState.DENIED,
        )
    with pytest.raises(ValidationError, match="must not contain"):
        ArtifactOperationResult(
            contract_version="1",
            operation_id=operation_id,
            state=ArtifactOperationState.VALIDATED,
            failure=ArtifactFailure(
                boundary=ArtifactBoundary.EVIDENCE,
                code=ArtifactFailureCode.EVIDENCE_MISSING,
            ),
        )


def test_exact_current_single_candidate_is_validated() -> None:
    request = intent()
    candidate = evidence()

    assert assess_artifact_intent(
        request,
        (candidate,),
        current_target=request.target,
        limits=ArtifactLimits(),
    ) == ArtifactOperationResult(
        contract_version="1",
        operation_id=request.operation_id,
        state=ArtifactOperationState.VALIDATED,
    )


def test_unknown_missing_ambiguous_and_stale_evidence_is_denied() -> None:
    request = intent()
    candidate = evidence()
    limits = ArtifactLimits()

    assert_denied(
        assess_artifact_intent(request, None, current_target=request.target, limits=limits),
        ArtifactBoundary.EVIDENCE,
        ArtifactFailureCode.EVIDENCE_UNKNOWN,
    )
    assert_denied(
        assess_artifact_intent(request, (), current_target=request.target, limits=limits),
        ArtifactBoundary.EVIDENCE,
        ArtifactFailureCode.EVIDENCE_MISSING,
    )
    assert_denied(
        assess_artifact_intent(
            request,
            (candidate, candidate),
            current_target=request.target,
            limits=limits,
        ),
        ArtifactBoundary.EVIDENCE,
        ArtifactFailureCode.EVIDENCE_AMBIGUOUS,
    )
    assert_denied(
        assess_artifact_intent(request, (candidate,), current_target=None, limits=limits),
        ArtifactBoundary.TARGET,
        ArtifactFailureCode.EVIDENCE_UNKNOWN,
    )
    stale = request.target.model_copy(update={"safety_profile_fingerprint": "d" * 64})
    assert_denied(
        assess_artifact_intent(request, (candidate,), current_target=stale, limits=limits),
        ArtifactBoundary.TARGET,
        ArtifactFailureCode.EVIDENCE_STALE,
    )


def test_contradictory_printer_artifact_plate_and_target_are_denied() -> None:
    request = intent()
    candidate = evidence()
    limits = ArtifactLimits()
    other_printer = request.target.model_copy(update={"printer_id": "other-printer"})
    assert_denied(
        assess_artifact_intent(
            request,
            (candidate,),
            current_target=other_printer,
            limits=limits,
        ),
        ArtifactBoundary.TARGET,
        ArtifactFailureCode.EVIDENCE_CONTRADICTORY,
    )

    changed_artifact = candidate.model_copy(
        update={
            "artifact": candidate.artifact.model_copy(
                update={"archive_sha256": "sha256:" + "d" * 64}
            )
        }
    )
    changed_plate = candidate.model_copy(
        update={
            "selected_plate": candidate.selected_plate.model_copy(update={"plate_id": "plate-2"})
        }
    )
    changed_target = candidate.model_copy(update={"target": other_printer})
    for changed, boundary in (
        (changed_artifact, ArtifactBoundary.INTAKE),
        (changed_plate, ArtifactBoundary.SELECTED_PLATE),
        (changed_target, ArtifactBoundary.TARGET),
    ):
        assert_denied(
            assess_artifact_intent(
                request,
                (changed,),
                current_target=request.target,
                limits=limits,
            ),
            boundary,
            ArtifactFailureCode.EVIDENCE_CONTRADICTORY,
        )


def test_each_configured_artifact_limit_fails_closed() -> None:
    request = intent()
    candidate = evidence()
    limits = ArtifactLimits(
        max_zip_entries=12,
        max_archive_compressed_bytes=request.artifact.compressed_size_bytes,
        max_archive_expanded_bytes=2 * 1024 * 1024,
        max_compression_ratio=4,
        max_gcode_bytes=1024 * 1024,
        metadata_wait_seconds=30.0,
    )
    oversized_intake = request.model_copy(
        update={
            "artifact": request.artifact.model_copy(
                update={"compressed_size_bytes": limits.max_archive_compressed_bytes + 1}
            )
        }
    )
    assert_denied(
        assess_artifact_intent(
            oversized_intake,
            (candidate,),
            current_target=oversized_intake.target,
            limits=limits,
        ),
        ArtifactBoundary.INTAKE,
        ArtifactFailureCode.LIMIT_EXCEEDED,
    )

    for update in (
        {"zip_entry_count": limits.max_zip_entries + 1},
        {"archive_expanded_bytes": limits.max_archive_expanded_bytes + 1},
    ):
        assert_denied(
            assess_artifact_intent(
                request,
                (candidate.model_copy(update=update),),
                current_target=request.target,
                limits=limits,
            ),
            ArtifactBoundary.ARCHIVE,
            ArtifactFailureCode.LIMIT_EXCEEDED,
        )

    excessive_ratio = candidate.highest_ratio_entry.model_copy(
        update={
            "expanded_bytes": (
                limits.max_compression_ratio * candidate.highest_ratio_entry.compressed_bytes + 1
            )
        }
    )
    assert_denied(
        assess_artifact_intent(
            request,
            (candidate.model_copy(update={"highest_ratio_entry": excessive_ratio}),),
            current_target=request.target,
            limits=limits,
        ),
        ArtifactBoundary.ARCHIVE,
        ArtifactFailureCode.LIMIT_EXCEEDED,
    )

    large_gcode = candidate.selected_plate.model_copy(
        update={"gcode_size_bytes": limits.max_gcode_bytes + 1}
    )
    assert_denied(
        assess_artifact_intent(
            request,
            (candidate.model_copy(update={"selected_plate": large_gcode}),),
            current_target=request.target,
            limits=limits,
        ),
        ArtifactBoundary.SELECTED_PLATE,
        ArtifactFailureCode.LIMIT_EXCEEDED,
    )


def test_current_slicer_profile_change_also_makes_evidence_stale() -> None:
    request = intent()
    candidate = evidence()
    changed = ArtifactTarget(
        printer_id=request.target.printer_id,
        slicer_profile_id="new-profile",
        safety_profile_fingerprint=request.target.safety_profile_fingerprint,
    )

    assert_denied(
        assess_artifact_intent(
            request,
            (candidate,),
            current_target=changed,
            limits=ArtifactLimits(),
        ),
        ArtifactBoundary.TARGET,
        ArtifactFailureCode.EVIDENCE_STALE,
    )
