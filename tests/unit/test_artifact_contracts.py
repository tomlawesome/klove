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
    ArtifactQualification,
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


def assert_denied(
    result: ArtifactOperationResult,
    boundary: ArtifactBoundary,
    code: ArtifactFailureCode,
) -> None:
    assert result.state is ArtifactOperationState.DENIED
    assert result.failure == ArtifactFailure(boundary=boundary, code=code)
    assert result.qualification is None


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


def test_exact_current_inspection_and_approval_produce_non_actuating_qualification() -> None:
    result = qualify()
    expected_approval = approval()

    assert result.state is ArtifactOperationState.QUALIFIED
    assert result.failure is None
    assert result.qualification == ArtifactQualification(
        approval_id=expected_approval.approval_id,
        authority_id=expected_approval.authority_id,
        operation_id=intent().operation_id,
        idempotency_key=intent().idempotency_key,
        artifact=inspection().artifact,
        selected_plate=inspection().selected_plate,
        target=intent().target,
    )


def test_manual_override_is_explicit_per_file_and_never_qualifies_automation() -> None:
    record = manual_override()
    assert record.artifact == intent().artifact
    assert record.selected_plate == inspection().selected_plate
    assert record.automatic_dispatch_allowed is False
    assert_denied(
        qualify(override=record),
        ArtifactBoundary.TARGET,
        ArtifactFailureCode.MANUAL_OVERRIDE_NOT_AUTOMATIC,
    )

    data = record.model_dump(mode="json")
    data["automatic_dispatch_allowed"] = True
    with pytest.raises(ValidationError):
        ArtifactManualOverrideRecord.model_validate_json(json.dumps(data))


def test_unknown_missing_and_ambiguous_inspection_or_approval_is_denied() -> None:
    request = intent()
    limits = ArtifactLimits()
    current_profile = profile()

    for candidates, code in (
        (None, ArtifactFailureCode.EVIDENCE_UNKNOWN),
        ((), ArtifactFailureCode.EVIDENCE_MISSING),
        ((inspection(), inspection()), ArtifactFailureCode.EVIDENCE_AMBIGUOUS),
    ):
        assert_denied(
            assess_artifact_intent(
                request,
                candidates,
                (approval(),),
                current_profile=current_profile,
                manual_override=None,
                limits=limits,
            ),
            ArtifactBoundary.EVIDENCE,
            code,
        )

    for approvals, code in (
        (None, ArtifactFailureCode.EVIDENCE_UNKNOWN),
        ((), ArtifactFailureCode.EVIDENCE_MISSING),
        ((approval(), approval()), ArtifactFailureCode.EVIDENCE_AMBIGUOUS),
    ):
        assert_denied(
            assess_artifact_intent(
                request,
                (inspection(),),
                approvals,
                current_profile=current_profile,
                manual_override=None,
                limits=limits,
            ),
            ArtifactBoundary.EVIDENCE,
            code,
        )

    assert_denied(
        assess_artifact_intent(
            request,
            (inspection(),),
            (approval(),),
            current_profile=None,
            manual_override=None,
            limits=limits,
        ),
        ArtifactBoundary.TARGET,
        ArtifactFailureCode.EVIDENCE_UNKNOWN,
    )


@pytest.mark.parametrize(
    ("target_update", "code"),
    [
        (
            {"printer_uuid": "66666666-6666-4666-8666-666666666666"},
            ArtifactFailureCode.TARGET_MISMATCH,
        ),
        (
            {"slicer_profile_id": "near-match"},
            ArtifactFailureCode.SLICER_PROFILE_MISMATCH,
        ),
        ({"safety_profile_generation": 6}, ArtifactFailureCode.EVIDENCE_STALE),
        ({"safety_profile_fingerprint": "d" * 64}, ArtifactFailureCode.EVIDENCE_STALE),
    ],
)
def test_intent_must_match_exact_current_target(
    target_update: dict[str, object], code: ArtifactFailureCode
) -> None:
    request = intent().model_copy(
        update={"target": intent().target.model_copy(update=target_update)}
    )

    assert_denied(qualify(request=request), ArtifactBoundary.TARGET, code)


def test_every_safety_configuration_change_invalidates_prior_approval() -> None:
    changed_profiles = (
        profile(generation=8),
        profile(slicer_profile_id="new-exact-profile"),
        profile(
            compatibility=compatibility().model_copy(update={"nozzle_diameter_micrometres": 600})
        ),
        profile(
            compatibility=compatibility().model_copy(
                update={
                    "build_volume": BuildVolume(
                        x_micrometres=300_000,
                        y_micrometres=350_000,
                        z_micrometres=350_000,
                    )
                }
            )
        ),
        profile(compatibility=compatibility().model_copy(update={"build_plate_id": "smooth-pei"})),
    )

    for changed in changed_profiles:
        assert_denied(
            qualify(current_profile=changed),
            ArtifactBoundary.TARGET,
            ArtifactFailureCode.EVIDENCE_STALE
            if changed.slicer_profile_id == intent().target.slicer_profile_id
            else ArtifactFailureCode.SLICER_PROFILE_MISMATCH,
        )


def test_inspection_must_bind_exact_artifact_selection_and_limits() -> None:
    current = inspection()
    changed_artifact = current.model_copy(
        update={
            "artifact": current.artifact.model_copy(update={"archive_sha256": "sha256:" + "d" * 64})
        }
    )
    changed_plate = current.model_copy(
        update={"selected_plate": current.selected_plate.model_copy(update={"plate_id": "plate-2"})}
    )
    changed_path = current.model_copy(
        update={
            "selected_plate": current.selected_plate.model_copy(
                update={"archive_path": "Metadata/plate_2.gcode"}
            )
        }
    )
    for changed, boundary in (
        (changed_artifact, ArtifactBoundary.INTAKE),
        (changed_plate, ArtifactBoundary.SELECTED_PLATE),
        (changed_path, ArtifactBoundary.SELECTED_PLATE),
    ):
        assert_denied(
            qualify(inspections=(changed,)),
            boundary,
            ArtifactFailureCode.EVIDENCE_CONTRADICTORY,
        )

    limits = ArtifactLimits(
        max_zip_entries=12,
        max_archive_compressed_bytes=intent().artifact.compressed_size_bytes,
        max_archive_expanded_bytes=2 * 1024 * 1024,
        max_compression_ratio=4,
        max_gcode_bytes=1024 * 1024,
    )
    oversized_intake = intent().model_copy(
        update={
            "artifact": intent().artifact.model_copy(
                update={"compressed_size_bytes": limits.max_archive_compressed_bytes + 1}
            )
        }
    )
    assert_denied(
        qualify(request=oversized_intake, limits=limits),
        ArtifactBoundary.INTAKE,
        ArtifactFailureCode.LIMIT_EXCEEDED,
    )

    for changed in (
        current.model_copy(update={"zip_entry_count": limits.max_zip_entries + 1}),
        current.model_copy(
            update={"archive_expanded_bytes": limits.max_archive_expanded_bytes + 1}
        ),
        current.model_copy(
            update={
                "highest_ratio_entry": current.highest_ratio_entry.model_copy(
                    update={
                        "expanded_bytes": (
                            limits.max_compression_ratio
                            * current.highest_ratio_entry.compressed_bytes
                            + 1
                        )
                    }
                )
            }
        ),
    ):
        assert_denied(
            qualify(inspections=(changed,), limits=limits),
            ArtifactBoundary.ARCHIVE,
            ArtifactFailureCode.LIMIT_EXCEEDED,
        )

    large_gcode = current.model_copy(
        update={
            "selected_plate": current.selected_plate.model_copy(
                update={"gcode_size_bytes": limits.max_gcode_bytes + 1}
            )
        }
    )
    assert_denied(
        qualify(inspections=(large_gcode,), limits=limits),
        ArtifactBoundary.SELECTED_PLATE,
        ArtifactFailureCode.LIMIT_EXCEEDED,
    )


@pytest.mark.parametrize("field", ["operation_id", "idempotency_key"])
def test_approval_must_bind_the_exact_operation(field: str) -> None:
    changed = approval().model_copy(update={field: "66666666-6666-4666-8666-666666666666"})

    assert_denied(
        qualify(approvals=(changed,)),
        ArtifactBoundary.EVIDENCE,
        ArtifactFailureCode.EVIDENCE_CONTRADICTORY,
    )


def test_approval_must_bind_exact_inspected_bytes() -> None:
    accepted = approval()
    changed_artifact = accepted.model_copy(
        update={
            "artifact": accepted.artifact.model_copy(
                update={"archive_sha256": "sha256:" + "d" * 64}
            )
        }
    )
    changed_plate = accepted.model_copy(
        update={
            "selected_plate": accepted.selected_plate.model_copy(
                update={"gcode_sha256": "sha256:" + "d" * 64}
            )
        }
    )

    assert_denied(
        qualify(approvals=(changed_artifact,)),
        ArtifactBoundary.INTAKE,
        ArtifactFailureCode.EVIDENCE_CONTRADICTORY,
    )
    assert_denied(
        qualify(approvals=(changed_plate,)),
        ArtifactBoundary.SELECTED_PLATE,
        ArtifactFailureCode.EVIDENCE_CONTRADICTORY,
    )


@pytest.mark.parametrize(
    ("changed", "code"),
    [
        (
            {
                "target": approval().target.model_copy(
                    update={"printer_uuid": "66666666-6666-4666-8666-666666666666"}
                )
            },
            ArtifactFailureCode.TARGET_MISMATCH,
        ),
        (
            {"target": approval().target.model_copy(update={"slicer_profile_id": "near-match"})},
            ArtifactFailureCode.SLICER_PROFILE_MISMATCH,
        ),
        (
            {"target": approval().target.model_copy(update={"safety_profile_generation": 6})},
            ArtifactFailureCode.EVIDENCE_STALE,
        ),
        (
            {
                "compatibility": compatibility().model_copy(
                    update={"gcode_flavor": GcodeFlavor.MARLIN}
                )
            },
            ArtifactFailureCode.GCODE_FLAVOR_MISMATCH,
        ),
        (
            {
                "compatibility": compatibility().model_copy(
                    update={"nozzle_diameter_micrometres": 600}
                )
            },
            ArtifactFailureCode.NOZZLE_MISMATCH,
        ),
        (
            {
                "compatibility": compatibility().model_copy(
                    update={
                        "build_volume": BuildVolume(
                            x_micrometres=300_000,
                            y_micrometres=350_000,
                            z_micrometres=350_000,
                        )
                    }
                )
            },
            ArtifactFailureCode.BUILD_VOLUME_MISMATCH,
        ),
        (
            {"compatibility": compatibility().model_copy(update={"build_plate_id": "smooth-pei"})},
            ArtifactFailureCode.BUILD_PLATE_MISMATCH,
        ),
        (
            {
                "target": approval().target.model_copy(
                    update={"safety_profile_fingerprint": "d" * 64}
                )
            },
            ArtifactFailureCode.EVIDENCE_CONTRADICTORY,
        ),
    ],
)
def test_approval_profile_mismatches_fail_closed(
    changed: dict[str, object], code: ArtifactFailureCode
) -> None:
    assert_denied(
        qualify(approvals=(approval().model_copy(update=changed),)),
        ArtifactBoundary.TARGET,
        code,
    )
