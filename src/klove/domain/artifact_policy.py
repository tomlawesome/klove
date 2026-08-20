"""Fail-closed qualification and intent policy for artifact operations."""

from __future__ import annotations

from klove.domain.artifact_contract_models import (
    CONTRACT_VERSION,
    ArtifactBoundary,
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
    SafetyProfile,
    safety_profile_fingerprint,
    target_for_safety_profile,
)


def assess_artifact_intent(  # noqa: PLR0911, PLR0912, PLR0913 -- explicit denials.
    intent: ArtifactIntent,
    inspections: tuple[ArtifactInspectionEvidence, ...] | None,
    approvals: tuple[ArtifactTargetApproval, ...] | None,
    *,
    current_profile: SafetyProfile | None,
    manual_override: ArtifactManualOverrideRecord | None,
    limits: ArtifactLimits,
) -> ArtifactOperationResult:
    """Qualify exact inspected bytes only with one current independent approval."""
    if intent.artifact.compressed_size_bytes > limits.max_archive_compressed_bytes:
        return _denied(intent, ArtifactBoundary.INTAKE, ArtifactFailureCode.LIMIT_EXCEEDED)
    if manual_override is not None:
        return _denied(
            intent,
            ArtifactBoundary.TARGET,
            ArtifactFailureCode.MANUAL_OVERRIDE_NOT_AUTOMATIC,
        )
    if inspections is None:
        return _denied(intent, ArtifactBoundary.EVIDENCE, ArtifactFailureCode.EVIDENCE_UNKNOWN)
    if not inspections:
        return _denied(intent, ArtifactBoundary.EVIDENCE, ArtifactFailureCode.EVIDENCE_MISSING)
    if len(inspections) != 1:
        return _denied(intent, ArtifactBoundary.EVIDENCE, ArtifactFailureCode.EVIDENCE_AMBIGUOUS)

    inspection = inspections[0]
    if inspection.artifact != intent.artifact:
        return _denied(
            intent,
            ArtifactBoundary.INTAKE,
            ArtifactFailureCode.EVIDENCE_CONTRADICTORY,
        )
    if (
        inspection.selected_plate.plate_id != intent.selected_plate.plate_id
        or inspection.selected_plate.archive_path != intent.selected_plate.archive_path
    ):
        return _denied(
            intent,
            ArtifactBoundary.SELECTED_PLATE,
            ArtifactFailureCode.EVIDENCE_CONTRADICTORY,
        )
    if (
        inspection.zip_entry_count > limits.max_zip_entries
        or inspection.archive_expanded_bytes > limits.max_archive_expanded_bytes
        or inspection.highest_ratio_entry.expanded_bytes
        > limits.max_compression_ratio * inspection.highest_ratio_entry.compressed_bytes
    ):
        return _denied(intent, ArtifactBoundary.ARCHIVE, ArtifactFailureCode.LIMIT_EXCEEDED)
    if inspection.selected_plate.gcode_size_bytes > limits.max_gcode_bytes:
        return _denied(
            intent,
            ArtifactBoundary.SELECTED_PLATE,
            ArtifactFailureCode.LIMIT_EXCEEDED,
        )

    if current_profile is None:
        return _denied(intent, ArtifactBoundary.TARGET, ArtifactFailureCode.EVIDENCE_UNKNOWN)
    current_target = target_for_safety_profile(current_profile)
    if intent.target.printer_uuid != current_target.printer_uuid:
        return _denied(intent, ArtifactBoundary.TARGET, ArtifactFailureCode.TARGET_MISMATCH)
    if intent.target.slicer_profile_id != current_target.slicer_profile_id:
        return _denied(
            intent,
            ArtifactBoundary.TARGET,
            ArtifactFailureCode.SLICER_PROFILE_MISMATCH,
        )
    if intent.target != current_target:
        return _denied(intent, ArtifactBoundary.TARGET, ArtifactFailureCode.EVIDENCE_STALE)

    if approvals is None:
        return _denied(intent, ArtifactBoundary.EVIDENCE, ArtifactFailureCode.EVIDENCE_UNKNOWN)
    if not approvals:
        return _denied(intent, ArtifactBoundary.EVIDENCE, ArtifactFailureCode.EVIDENCE_MISSING)
    if len(approvals) != 1:
        return _denied(intent, ArtifactBoundary.EVIDENCE, ArtifactFailureCode.EVIDENCE_AMBIGUOUS)

    approval = approvals[0]
    if (
        approval.operation_id != intent.operation_id
        or approval.idempotency_key != intent.idempotency_key
    ):
        return _denied(
            intent,
            ArtifactBoundary.EVIDENCE,
            ArtifactFailureCode.EVIDENCE_CONTRADICTORY,
        )
    if approval.artifact != inspection.artifact:
        return _denied(
            intent,
            ArtifactBoundary.INTAKE,
            ArtifactFailureCode.EVIDENCE_CONTRADICTORY,
        )
    if approval.selected_plate != inspection.selected_plate:
        return _denied(
            intent,
            ArtifactBoundary.SELECTED_PLATE,
            ArtifactFailureCode.EVIDENCE_CONTRADICTORY,
        )
    if approval.target.printer_uuid != current_profile.printer_uuid:
        return _denied(intent, ArtifactBoundary.TARGET, ArtifactFailureCode.TARGET_MISMATCH)
    if approval.target.slicer_profile_id != current_profile.slicer_profile_id:
        return _denied(
            intent,
            ArtifactBoundary.TARGET,
            ArtifactFailureCode.SLICER_PROFILE_MISMATCH,
        )
    if approval.target.safety_profile_generation != current_profile.generation:
        return _denied(intent, ArtifactBoundary.TARGET, ArtifactFailureCode.EVIDENCE_STALE)
    if approval.compatibility.gcode_flavor != current_profile.compatibility.gcode_flavor:
        return _denied(
            intent,
            ArtifactBoundary.TARGET,
            ArtifactFailureCode.GCODE_FLAVOR_MISMATCH,
        )
    if (
        approval.compatibility.nozzle_diameter_micrometres
        != current_profile.compatibility.nozzle_diameter_micrometres
    ):
        return _denied(intent, ArtifactBoundary.TARGET, ArtifactFailureCode.NOZZLE_MISMATCH)
    if approval.compatibility.build_volume != current_profile.compatibility.build_volume:
        return _denied(
            intent,
            ArtifactBoundary.TARGET,
            ArtifactFailureCode.BUILD_VOLUME_MISMATCH,
        )
    if approval.compatibility.build_plate_id != current_profile.compatibility.build_plate_id:
        return _denied(
            intent,
            ArtifactBoundary.TARGET,
            ArtifactFailureCode.BUILD_PLATE_MISMATCH,
        )

    approved_profile = SafetyProfile(
        printer_uuid=approval.target.printer_uuid,
        generation=approval.target.safety_profile_generation,
        slicer_profile_id=approval.target.slicer_profile_id,
        compatibility=approval.compatibility,
    )
    if approval.target.safety_profile_fingerprint != safety_profile_fingerprint(approved_profile):
        return _denied(
            intent,
            ArtifactBoundary.TARGET,
            ArtifactFailureCode.EVIDENCE_CONTRADICTORY,
        )

    return ArtifactOperationResult(
        contract_version=CONTRACT_VERSION,
        operation_id=intent.operation_id,
        state=ArtifactOperationState.QUALIFIED,
        qualification=ArtifactQualification(
            approval_id=approval.approval_id,
            authority_id=approval.authority_id,
            operation_id=intent.operation_id,
            idempotency_key=intent.idempotency_key,
            artifact=inspection.artifact,
            selected_plate=inspection.selected_plate,
            target=current_target,
        ),
    )


def _denied(
    intent: ArtifactIntent,
    boundary: ArtifactBoundary,
    code: ArtifactFailureCode,
) -> ArtifactOperationResult:
    """Build one bounded fail-closed result."""
    return ArtifactOperationResult(
        contract_version=CONTRACT_VERSION,
        operation_id=intent.operation_id,
        state=ArtifactOperationState.DENIED,
        failure=ArtifactFailure(boundary=boundary, code=code),
    )
