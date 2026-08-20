"""Versioned, fail-closed contracts for artifact inspection and qualification.

Stable public facade: re-exports the contract enums/models/fingerprints from
``artifact_contract_models`` and the qualification/intent policy from
``artifact_policy`` so existing imports of ``klove.domain.artifacts`` keep
working unchanged.
"""

from __future__ import annotations

from klove.domain.artifact_contract_models import (
    CONTRACT_VERSION,
    ArchivePath,
    ArtifactBoundary,
    ArtifactCompatibility,
    ArtifactFailure,
    ArtifactFailureCode,
    ArtifactFormat,
    ArtifactInspectionEvidence,
    ArtifactIntake,
    ArtifactIntent,
    ArtifactLimits,
    ArtifactManualOverrideReason,
    ArtifactManualOverrideRecord,
    ArtifactOperationResult,
    ArtifactOperationState,
    ArtifactQualification,
    ArtifactTarget,
    ArtifactTargetApproval,
    BoundedIdentifier,
    BuildVolume,
    CanonicalUuid4,
    CompressionRatioEvidence,
    GcodeFlavor,
    PlateIdentifier,
    PlateSelection,
    SafetyProfile,
    SafetyProfileFingerprint,
    SelectedPlate,
    Sha256Digest,
    safety_profile_fingerprint,
    target_for_safety_profile,
)
from klove.domain.artifact_policy import assess_artifact_intent

__all__ = [
    "CONTRACT_VERSION",
    "ArchivePath",
    "ArtifactBoundary",
    "ArtifactCompatibility",
    "ArtifactFailure",
    "ArtifactFailureCode",
    "ArtifactFormat",
    "ArtifactInspectionEvidence",
    "ArtifactIntake",
    "ArtifactIntent",
    "ArtifactLimits",
    "ArtifactManualOverrideReason",
    "ArtifactManualOverrideRecord",
    "ArtifactOperationResult",
    "ArtifactOperationState",
    "ArtifactQualification",
    "ArtifactTarget",
    "ArtifactTargetApproval",
    "BoundedIdentifier",
    "BuildVolume",
    "CanonicalUuid4",
    "CompressionRatioEvidence",
    "GcodeFlavor",
    "PlateIdentifier",
    "PlateSelection",
    "SafetyProfile",
    "SafetyProfileFingerprint",
    "SelectedPlate",
    "Sha256Digest",
    "assess_artifact_intent",
    "safety_profile_fingerprint",
    "target_for_safety_profile",
]
