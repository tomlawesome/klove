from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from klove.domain.artifacts import (
    ArtifactBoundary,
    ArtifactCompatibility,
    ArtifactFailure,
    ArtifactFailureCode,
    ArtifactInspectionEvidence,
    ArtifactIntent,
    ArtifactLimits,
    ArtifactManualOverrideRecord,
    ArtifactOperationResult,
    ArtifactOperationState,
    ArtifactTargetApproval,
    BuildVolume,
    GcodeFlavor,
    SafetyProfile,
    SelectedPlate,
    assess_artifact_intent,
    safety_profile_fingerprint,
    target_for_safety_profile,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "artifacts"


def load_fixture(model: type[BaseModel], name: str) -> BaseModel:
    return model.model_validate_json((FIXTURES / name).read_text(encoding="utf-8"))


def intent() -> ArtifactIntent:
    return ArtifactIntent.model_validate_json(
        (FIXTURES / "accepted-intent.json").read_text(encoding="utf-8")
    )


def inspection() -> ArtifactInspectionEvidence:
    return ArtifactInspectionEvidence.model_validate_json(
        (FIXTURES / "accepted-evidence.json").read_text(encoding="utf-8")
    )


def approval() -> ArtifactTargetApproval:
    return ArtifactTargetApproval.model_validate_json(
        (FIXTURES / "accepted-approval.json").read_text(encoding="utf-8")
    )


def manual_override() -> ArtifactManualOverrideRecord:
    return ArtifactManualOverrideRecord.model_validate_json(
        (FIXTURES / "accepted-override.json").read_text(encoding="utf-8")
    )


def compatibility() -> ArtifactCompatibility:
    return ArtifactCompatibility(
        gcode_flavor=GcodeFlavor.KLIPPER,
        nozzle_diameter_micrometres=400,
        build_volume=BuildVolume(
            x_micrometres=350_000,
            y_micrometres=350_000,
            z_micrometres=350_000,
        ),
        build_plate_id="textured-pei",
    )


def profile(**updates: object) -> SafetyProfile:
    values: dict[str, object] = {
        "printer_uuid": "11111111-1111-4111-8111-111111111111",
        "generation": 7,
        "slicer_profile_id": "klipper-voron-24-0.4",
        "compatibility": compatibility(),
    }
    values.update(updates)
    return SafetyProfile.model_validate(values)


def qualify(  # noqa: PLR0913 -- compact policy-test helper.
    *,
    request: ArtifactIntent | None = None,
    inspections: tuple[ArtifactInspectionEvidence, ...] | None = None,
    approvals: tuple[ArtifactTargetApproval, ...] | None = None,
    current_profile: SafetyProfile | None = None,
    override: ArtifactManualOverrideRecord | None = None,
    limits: ArtifactLimits | None = None,
) -> ArtifactOperationResult:
    return assess_artifact_intent(
        request or intent(),
        (inspection(),) if inspections is None else inspections,
        (approval(),) if approvals is None else approvals,
        current_profile=profile() if current_profile is None else current_profile,
        manual_override=override,
        limits=limits or ArtifactLimits(),
    )


@pytest.mark.parametrize(
    ("model", "name"),
    [
        (ArtifactIntent, "accepted-intent.json"),
        (ArtifactInspectionEvidence, "accepted-evidence.json"),
        (ArtifactTargetApproval, "accepted-approval.json"),
        (ArtifactManualOverrideRecord, "accepted-override.json"),
        (ArtifactOperationResult, "accepted-denial.json"),
    ],
)
def test_accepted_contract_fixtures_are_strict_and_versioned(
    model: type[BaseModel], name: str
) -> None:
    result = load_fixture(model, name)

    assert result.model_dump(mode="json")["contract_version"] == "3"


@pytest.mark.parametrize(
    ("model", "name"),
    [
        (ArtifactIntent, "rejected-intake.json"),
        (ArtifactIntent, "rejected-selection.json"),
        (ArtifactIntent, "rejected-target.json"),
        (ArtifactIntent, "rejected-operation.json"),
        (ArtifactInspectionEvidence, "rejected-evidence.json"),
        (ArtifactInspectionEvidence, "rejected-evidence-ratio.json"),
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
    assert schema["properties"]["contract_version"]["const"] == "3"
    assert all(
        schema["$defs"][name]["additionalProperties"] is False
        for name in ("ArtifactIntake", "ArtifactTarget", "PlateSelection")
    )

    mutations: tuple[tuple[str, str, object], ...] = (
        ("artifact", "compressed_size_bytes", True),
        ("artifact", "artifact_id", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
        ("artifact", "format", "zip"),
        ("target", "printer_uuid", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
        ("target", "safety_profile_generation", True),
    )
    for owner_name, field, value in mutations:
        request = intent().model_dump(mode="json")
        request[owner_name][field] = value
        with pytest.raises(ValidationError):
            ArtifactIntent.model_validate_json(json.dumps(request))

    request = intent().model_dump(mode="json")
    request["contract_version"] = 2
    with pytest.raises(ValidationError):
        ArtifactIntent.model_validate_json(json.dumps(request))


def test_contract_models_reject_aliases_and_internal_contradictions() -> None:
    current = inspection()
    data = current.model_dump(mode="json")
    data["selected_plate"]["gcode_size_bytes"] = data["archive_expanded_bytes"] + 1
    with pytest.raises(ValidationError, match="exceeds expanded archive"):
        ArtifactInspectionEvidence.model_validate_json(json.dumps(data))

    for field, value in (
        ("compressed_bytes", 0),
        ("compressed_bytes", current.artifact.compressed_size_bytes + 1),
        ("expanded_bytes", current.archive_expanded_bytes + 1),
    ):
        data = current.model_dump(mode="json")
        data["highest_ratio_entry"][field] = value
        with pytest.raises(ValidationError):
            ArtifactInspectionEvidence.model_validate_json(json.dumps(data))

    for path in (
        "/Metadata/plate.gcode",
        "Metadata\\plate.gcode",
        "C:/Metadata/plate.gcode",
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

    for owner, field in (
        (intent(), "slicer_profile_id"),
        (approval(), "authority_id"),
        (manual_override(), "actor_id"),
        (compatibility(), "build_plate_id"),
    ):
        data = owner.model_dump(mode="json")
        target: dict[str, Any] = data["target"] if field == "slicer_profile_id" else data
        target[field] = "bad\nvalue"
        with pytest.raises(ValidationError):
            type(owner).model_validate_json(json.dumps(data))


def test_safety_profile_has_canonical_exact_fingerprint_and_klipper_dialect() -> None:
    current = profile()

    assert safety_profile_fingerprint(current) == (
        "dc188fe71b16d85b19acf7b7ec3a5f4c4381850347cf6c6aa98b257d09264481"
    )
    assert target_for_safety_profile(current) == intent().target

    incompatible = compatibility().model_copy(update={"gcode_flavor": GcodeFlavor.MARLIN})
    with pytest.raises(ValidationError, match="require Klipper"):
        profile(compatibility=incompatible)
    with pytest.raises(ValidationError):
        BuildVolume(x_micrometres=350_000.0, y_micrometres=350_000, z_micrometres=350_000)  # type: ignore[arg-type]


def test_artifact_limits_require_upload_polling_to_fit_the_metadata_window() -> None:
    limits = ArtifactLimits(
        upload_timeout_seconds=1,
        metadata_wait_seconds=0.2,
        metadata_poll_interval_seconds=0.1,
        upload_idempotency_capacity=1,
    )
    assert limits.upload_timeout_seconds == 1
    assert limits.upload_idempotency_capacity == 1
    with pytest.raises(ValidationError, match="poll interval"):
        ArtifactLimits(metadata_wait_seconds=0.1, metadata_poll_interval_seconds=0.2)


def test_operation_result_requires_exact_state_payload() -> None:
    operation_id = intent().operation_id
    received = ArtifactOperationResult(
        contract_version="3",
        operation_id=operation_id,
        state=ArtifactOperationState.RECEIVED,
    )
    assert received.failure is None
    assert received.qualification is None

    qualified = qualify()
    assert qualified.qualification is not None
    qualification = qualified.qualification

    with pytest.raises(ValidationError, match="require a failure"):
        ArtifactOperationResult(
            contract_version="3",
            operation_id=operation_id,
            state=ArtifactOperationState.DENIED,
        )
    with pytest.raises(ValidationError, match="must not contain a qualification"):
        ArtifactOperationResult(
            contract_version="3",
            operation_id=operation_id,
            state=ArtifactOperationState.DENIED,
            failure=ArtifactFailure(
                boundary=ArtifactBoundary.EVIDENCE,
                code=ArtifactFailureCode.EVIDENCE_MISSING,
            ),
            qualification=qualification,
        )
    with pytest.raises(ValidationError, match="must not contain a failure"):
        ArtifactOperationResult(
            contract_version="3",
            operation_id=operation_id,
            state=ArtifactOperationState.RECEIVED,
            failure=ArtifactFailure(
                boundary=ArtifactBoundary.EVIDENCE,
                code=ArtifactFailureCode.EVIDENCE_MISSING,
            ),
        )
    with pytest.raises(ValidationError, match="require a qualification"):
        ArtifactOperationResult(
            contract_version="3",
            operation_id=operation_id,
            state=ArtifactOperationState.QUALIFIED,
        )
    with pytest.raises(ValidationError, match="received operations"):
        ArtifactOperationResult(
            contract_version="3",
            operation_id=operation_id,
            state=ArtifactOperationState.RECEIVED,
            qualification=qualification,
        )
