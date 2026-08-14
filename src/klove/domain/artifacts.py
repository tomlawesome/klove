"""Versioned, fail-closed contracts for artifact inspection and qualification."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

CONTRACT_VERSION: Literal["3"] = "3"

CanonicalUuid4 = Annotated[
    str,
    StringConstraints(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]
Sha256Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
SafetyProfileFingerprint = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
BoundedIdentifier = Annotated[str, StringConstraints(min_length=1, max_length=255)]
PlateIdentifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$"),
]
ArchivePath = Annotated[str, StringConstraints(min_length=1, max_length=1024)]


class ArtifactFormat(StrEnum):
    """Artifact formats accepted by the first validation contract."""

    GCODE_3MF = "gcode_3mf"


class GcodeFlavor(StrEnum):
    """Known slicer dialect claims; configured Klove targets accept only Klipper."""

    KLIPPER = "klipper"
    MARLIN = "marlin"
    REPRAP = "reprap"


class ArtifactBoundary(StrEnum):
    """Stable locations at which artifact evidence can be denied."""

    INTAKE = "intake"
    ARCHIVE = "archive"
    SELECTED_PLATE = "selected_plate"
    TARGET = "target"
    EVIDENCE = "evidence"


class ArtifactFailureCode(StrEnum):
    """Bounded failure classes that never contain hostile source text."""

    LIMIT_EXCEEDED = "limit_exceeded"
    EVIDENCE_UNKNOWN = "evidence_unknown"
    EVIDENCE_MISSING = "evidence_missing"
    EVIDENCE_STALE = "evidence_stale"
    EVIDENCE_CONTRADICTORY = "evidence_contradictory"
    EVIDENCE_AMBIGUOUS = "evidence_ambiguous"
    MALFORMED = "malformed"
    UNSAFE_ARCHIVE = "unsafe_archive"
    UNSUPPORTED_ARCHIVE = "unsupported_archive"
    INTEGRITY_FAILED = "integrity_failed"
    INVALID_GCODE = "invalid_gcode"
    INCOMPATIBLE_GCODE = "incompatible_gcode"
    TARGET_MISMATCH = "target_mismatch"
    SLICER_PROFILE_MISMATCH = "slicer_profile_mismatch"
    NOZZLE_MISMATCH = "nozzle_mismatch"
    BUILD_VOLUME_MISMATCH = "build_volume_mismatch"
    BUILD_PLATE_MISMATCH = "build_plate_mismatch"
    GCODE_FLAVOR_MISMATCH = "gcode_flavor_mismatch"
    MANUAL_OVERRIDE_NOT_AUTOMATIC = "manual_override_not_automatic"


class ArtifactOperationState(StrEnum):
    """States available before any upload or print-start transport exists."""

    RECEIVED = "received"
    QUALIFIED = "qualified"
    DENIED = "denied"


class ArtifactManualOverrideReason(StrEnum):
    """Closed reasons for a human review record that cannot authorize automation."""

    MISSING_PROOF = "missing_proof"
    PROFILE_MISMATCH = "profile_mismatch"
    RECOVERY_REVIEW = "recovery_review"


class ArtifactLimits(BaseModel):
    """Finite operator-configured limits shared by validation stages."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    max_zip_entries: int = Field(default=512, ge=1, le=100_000)
    max_zip_metadata_bytes: int = Field(default=16 * 1024 * 1024, ge=22, le=128 * 1024 * 1024)
    max_archive_compressed_bytes: int = Field(
        default=512 * 1024 * 1024,
        ge=1,
        le=4 * 1024 * 1024 * 1024,
    )
    max_archive_expanded_bytes: int = Field(
        default=2 * 1024 * 1024 * 1024,
        ge=1,
        le=8 * 1024 * 1024 * 1024,
    )
    max_compression_ratio: int = Field(default=100, ge=1, le=10_000)
    max_gcode_bytes: int = Field(
        default=1024 * 1024 * 1024,
        ge=1,
        le=4 * 1024 * 1024 * 1024,
    )
    max_gcode_header_bytes: int = Field(default=64 * 1024, ge=64, le=16 * 1024 * 1024)
    max_gcode_line_bytes: int = Field(default=8 * 1024, ge=1, le=1024 * 1024)
    upload_timeout_seconds: float = Field(default=300.0, gt=0, le=3_600, allow_inf_nan=False)
    metadata_wait_seconds: float = Field(default=30.0, gt=0, le=300, allow_inf_nan=False)
    metadata_poll_interval_seconds: float = Field(default=0.1, gt=0, le=5, allow_inf_nan=False)
    upload_idempotency_capacity: int = Field(default=1024, ge=1, le=100_000)

    @model_validator(mode="after")
    def gcode_fits_expanded_archive(self) -> ArtifactLimits:
        """Reject a selected-file limit larger than the whole expanded archive."""
        if self.max_gcode_bytes > self.max_archive_expanded_bytes:
            raise ValueError("max_gcode_bytes must not exceed max_archive_expanded_bytes")
        if self.max_gcode_header_bytes > self.max_gcode_bytes:
            raise ValueError("max_gcode_header_bytes must not exceed max_gcode_bytes")
        if self.max_gcode_line_bytes > self.max_gcode_bytes:
            raise ValueError("max_gcode_line_bytes must not exceed max_gcode_bytes")
        if self.metadata_poll_interval_seconds > self.metadata_wait_seconds:
            raise ValueError("metadata poll interval must not exceed metadata wait")
        return self


class BuildVolume(BaseModel):
    """Exact configured rectangular build envelope in integer micrometres."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    x_micrometres: int = Field(ge=1_000, le=5_000_000)
    y_micrometres: int = Field(ge=1_000, le=5_000_000)
    z_micrometres: int = Field(ge=1_000, le=5_000_000)


class ArtifactCompatibility(BaseModel):
    """Safety-relevant slicer settings that must match configured policy exactly."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    gcode_flavor: GcodeFlavor
    nozzle_diameter_micrometres: int = Field(ge=50, le=5_000)
    build_volume: BuildVolume
    build_plate_id: BoundedIdentifier

    @field_validator("gcode_flavor", mode="before")
    @classmethod
    def gcode_flavor_is_exact_known_text(cls, value: object) -> object:
        """Parse exact enum text without enabling broader strict-mode coercion."""
        if type(value) is str:
            return GcodeFlavor(value)
        return value

    @field_validator("build_plate_id")
    @classmethod
    def build_plate_id_is_bounded_text(cls, value: str) -> str:
        """Reject aliases hidden by whitespace or control characters."""
        return _validate_bounded_text(value)


class SafetyProfile(BaseModel):
    """One current operator-configured printer and slicer safety profile."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile_version: Literal["1"] = "1"
    printer_uuid: CanonicalUuid4
    generation: int = Field(ge=1, le=9_223_372_036_854_775_807)
    slicer_profile_id: BoundedIdentifier
    compatibility: ArtifactCompatibility

    @field_validator("slicer_profile_id")
    @classmethod
    def slicer_profile_id_is_bounded_text(cls, value: str) -> str:
        """Reject ambiguous profile identifiers rather than normalizing them."""
        return _validate_bounded_text(value)

    @model_validator(mode="after")
    def target_dialect_is_klipper(self) -> SafetyProfile:
        """Prevent a non-Klipper profile from becoming configured target policy."""
        if self.compatibility.gcode_flavor is not GcodeFlavor.KLIPPER:
            raise ValueError("configured safety profiles require Klipper G-code")
        return self


def safety_profile_fingerprint(profile: SafetyProfile) -> SafetyProfileFingerprint:
    """Hash one canonical JSON representation of all safety-relevant settings."""
    canonical = json.dumps(
        profile.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


class ArtifactIntake(BaseModel):
    """Identity and bounded size of one untrusted archive at intake."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_id: CanonicalUuid4
    format: ArtifactFormat
    archive_sha256: Sha256Digest
    compressed_size_bytes: int = Field(gt=0)


class PlateSelection(BaseModel):
    """The one logical plate requested from an artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    plate_id: PlateIdentifier
    archive_path: ArchivePath

    @field_validator("plate_id")
    @classmethod
    def plate_id_is_bounded_text(cls, value: str) -> str:
        """Reject surrounding whitespace and control characters."""
        return _validate_bounded_text(value)

    @field_validator("archive_path")
    @classmethod
    def archive_path_is_canonical(cls, value: str) -> str:
        """Require the exact canonical G-code member selected by the caller."""
        return _validate_archive_path(value)


class SelectedPlate(BaseModel):
    """Identity of one G-code candidate produced by archive validation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    plate_id: PlateIdentifier
    archive_path: ArchivePath
    gcode_sha256: Sha256Digest
    gcode_size_bytes: int = Field(gt=0)

    @field_validator("plate_id")
    @classmethod
    def plate_id_is_bounded_text(cls, value: str) -> str:
        """Reject surrounding whitespace and control characters."""
        return _validate_bounded_text(value)

    @field_validator("archive_path")
    @classmethod
    def archive_path_is_canonical(cls, value: str) -> str:
        """Require one relative POSIX path without traversal or aliases."""
        return _validate_archive_path(value)


class ArtifactTarget(BaseModel):
    """Exact printer, slicer profile, and safety-profile binding."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    printer_uuid: CanonicalUuid4
    slicer_profile_id: BoundedIdentifier
    safety_profile_generation: int = Field(ge=1, le=9_223_372_036_854_775_807)
    safety_profile_fingerprint: SafetyProfileFingerprint

    @field_validator("slicer_profile_id")
    @classmethod
    def slicer_profile_id_is_bounded_text(cls, value: str) -> str:
        """Reject ambiguous surrounding whitespace and control characters."""
        return _validate_bounded_text(value)


def target_for_safety_profile(profile: SafetyProfile) -> ArtifactTarget:
    """Derive the only target identity accepted for a configured profile."""
    return ArtifactTarget(
        printer_uuid=profile.printer_uuid,
        slicer_profile_id=profile.slicer_profile_id,
        safety_profile_generation=profile.generation,
        safety_profile_fingerprint=safety_profile_fingerprint(profile),
    )


class ArtifactIntent(BaseModel):
    """One versioned, idempotent artifact-qualification request."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_version: Literal["3"]
    operation_id: CanonicalUuid4
    idempotency_key: CanonicalUuid4
    artifact: ArtifactIntake
    selected_plate: PlateSelection
    target: ArtifactTarget


class CompressionRatioEvidence(BaseModel):
    """Exact byte counts for the archive entry with the highest ratio."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    compressed_bytes: int = Field(gt=0)
    expanded_bytes: int = Field(gt=0)


class ArtifactInspectionEvidence(BaseModel):
    """Byte-exact archive evidence produced before target/profile binding."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_version: Literal["3"]
    artifact: ArtifactIntake
    selected_plate: SelectedPlate
    zip_entry_count: int = Field(gt=0)
    archive_expanded_bytes: int = Field(gt=0)
    highest_ratio_entry: CompressionRatioEvidence

    @model_validator(mode="after")
    def selected_gcode_fits_archive(self) -> ArtifactInspectionEvidence:
        """Reject internally contradictory archive metrics."""
        if self.selected_plate.gcode_size_bytes > self.archive_expanded_bytes:
            raise ValueError("selected G-code exceeds expanded archive size")
        ratio_entry = self.highest_ratio_entry
        if (
            ratio_entry.compressed_bytes > self.artifact.compressed_size_bytes
            or ratio_entry.expanded_bytes > self.archive_expanded_bytes
        ):
            raise ValueError("compression-ratio entry exceeds archive metrics")
        if (
            self.archive_expanded_bytes * ratio_entry.compressed_bytes
            > self.artifact.compressed_size_bytes * ratio_entry.expanded_bytes
        ):
            raise ValueError("compression-ratio evidence understates the archive ratio")
        return self


class ArtifactTargetApproval(BaseModel):
    """Trusted controller evidence binding exact inspected bytes to one profile."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_version: Literal["3"]
    approval_id: CanonicalUuid4
    authority_id: BoundedIdentifier
    operation_id: CanonicalUuid4
    idempotency_key: CanonicalUuid4
    artifact: ArtifactIntake
    selected_plate: SelectedPlate
    target: ArtifactTarget
    compatibility: ArtifactCompatibility

    @field_validator("authority_id")
    @classmethod
    def authority_id_is_bounded_text(cls, value: str) -> str:
        """Require an exact machine authority identifier without hidden aliases."""
        return _validate_bounded_text(value)


class ArtifactManualOverrideRecord(BaseModel):
    """Per-file human review evidence that is permanently barred from automation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_version: Literal["3"]
    override_id: CanonicalUuid4
    operation_id: CanonicalUuid4
    idempotency_key: CanonicalUuid4
    artifact: ArtifactIntake
    selected_plate: SelectedPlate
    target: ArtifactTarget
    actor_id: BoundedIdentifier
    recorded_at_epoch_ms: int = Field(ge=0, le=253_402_300_799_999)
    reason: ArtifactManualOverrideReason
    automatic_dispatch_allowed: Literal[False] = False

    @field_validator("actor_id")
    @classmethod
    def actor_id_is_bounded_text(cls, value: str) -> str:
        """Keep the audit principal exact and free of hidden control characters."""
        return _validate_bounded_text(value)


class ArtifactQualification(BaseModel):
    """Exact non-actuating authority that the later upload slice may consume."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    approval_id: CanonicalUuid4
    authority_id: BoundedIdentifier
    operation_id: CanonicalUuid4
    idempotency_key: CanonicalUuid4
    artifact: ArtifactIntake
    selected_plate: SelectedPlate
    target: ArtifactTarget


class ArtifactFailure(BaseModel):
    """A structured denial that cannot echo hostile input."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    boundary: ArtifactBoundary
    code: ArtifactFailureCode


class ArtifactOperationResult(BaseModel):
    """Versioned artifact-operation state and optional structured denial."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_version: Literal["3"]
    operation_id: CanonicalUuid4
    state: ArtifactOperationState
    failure: ArtifactFailure | None = None
    qualification: ArtifactQualification | None = None

    @model_validator(mode="after")
    def failure_matches_state(self) -> ArtifactOperationResult:
        """Require exactly the state-specific result payload and nothing else."""
        if self.state is ArtifactOperationState.DENIED:
            if self.failure is None:
                raise ValueError("denied operations require a failure")
            if self.qualification is not None:
                raise ValueError("denied operations must not contain a qualification")
        elif self.failure is not None:
            raise ValueError("non-denied operations must not contain a failure")
        elif self.state is ArtifactOperationState.QUALIFIED:
            if self.qualification is None:
                raise ValueError("qualified operations require a qualification")
        elif self.qualification is not None:
            raise ValueError("received operations must not contain a qualification")
        return self


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


def _validate_bounded_text(value: str) -> str:
    """Reject text aliases and non-printing characters at contract boundaries."""
    has_non_visible_ascii = any(not 32 <= ord(character) <= 126 for character in value)
    if value != value.strip() or has_non_visible_ascii:
        raise ValueError("value must contain bounded visible ASCII text")
    return value


def _validate_archive_path(value: str) -> str:
    """Require one exact relative POSIX G-code path without aliases."""
    value = _validate_bounded_text(value)
    if value.startswith("/") or "\\" in value or ":" in value or " " in value:
        raise ValueError("archive_path must be a relative POSIX path")
    if any(part in {"", ".", ".."} or part != part.strip() for part in value.split("/")):
        raise ValueError("archive_path must not contain empty or relative segments")
    if not value.endswith(".gcode"):
        raise ValueError("archive_path must identify one G-code file")
    return value
