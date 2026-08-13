"""Versioned, fail-closed contracts for future artifact validation."""

from __future__ import annotations

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

CONTRACT_VERSION: Literal["2"] = "2"

CanonicalUuid4 = Annotated[
    str,
    StringConstraints(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]
Sha256Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
SafetyProfileFingerprint = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
PrinterIdentifier = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$"),
]
BoundedIdentifier = Annotated[str, StringConstraints(min_length=1, max_length=255)]
PlateIdentifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$"),
]
ArchivePath = Annotated[str, StringConstraints(min_length=1, max_length=1024)]


class ArtifactFormat(StrEnum):
    """Artifact formats accepted by the first validation contract."""

    GCODE_3MF = "gcode_3mf"


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


class ArtifactOperationState(StrEnum):
    """States available before any upload or print-start transport exists."""

    RECEIVED = "received"
    VALIDATED = "validated"
    DENIED = "denied"


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
    metadata_wait_seconds: float = Field(default=30.0, gt=0, le=300, allow_inf_nan=False)

    @model_validator(mode="after")
    def gcode_fits_expanded_archive(self) -> ArtifactLimits:
        """Reject a selected-file limit larger than the whole expanded archive."""
        if self.max_gcode_bytes > self.max_archive_expanded_bytes:
            raise ValueError("max_gcode_bytes must not exceed max_archive_expanded_bytes")
        if self.max_gcode_header_bytes > self.max_gcode_bytes:
            raise ValueError("max_gcode_header_bytes must not exceed max_gcode_bytes")
        if self.max_gcode_line_bytes > self.max_gcode_bytes:
            raise ValueError("max_gcode_line_bytes must not exceed max_gcode_bytes")
        return self


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

    printer_id: PrinterIdentifier
    slicer_profile_id: BoundedIdentifier
    safety_profile_fingerprint: SafetyProfileFingerprint

    @field_validator("slicer_profile_id")
    @classmethod
    def slicer_profile_id_is_bounded_text(cls, value: str) -> str:
        """Reject ambiguous surrounding whitespace and control characters."""
        return _validate_bounded_text(value)


class ArtifactIntent(BaseModel):
    """One versioned, idempotent artifact-validation request."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_version: Literal["2"]
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

    contract_version: Literal["2"]
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


class ArtifactValidationEvidence(ArtifactInspectionEvidence):
    """Complete inspection evidence with independently bound target/profile evidence."""

    target: ArtifactTarget


class ArtifactFailure(BaseModel):
    """A structured denial that cannot echo hostile input."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    boundary: ArtifactBoundary
    code: ArtifactFailureCode


class ArtifactOperationResult(BaseModel):
    """Versioned artifact-operation state and optional structured denial."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_version: Literal["2"]
    operation_id: CanonicalUuid4
    state: ArtifactOperationState
    failure: ArtifactFailure | None = None

    @model_validator(mode="after")
    def failure_matches_state(self) -> ArtifactOperationResult:
        """Require exactly one denial reason only for a denied operation."""
        if self.state is ArtifactOperationState.DENIED:
            if self.failure is None:
                raise ValueError("denied operations require a failure")
        elif self.failure is not None:
            raise ValueError("non-denied operations must not contain a failure")
        return self


def assess_artifact_intent(  # noqa: PLR0911 -- each denial is deliberately explicit.
    intent: ArtifactIntent,
    candidates: tuple[ArtifactValidationEvidence, ...] | None,
    *,
    current_target: ArtifactTarget | None,
    limits: ArtifactLimits,
) -> ArtifactOperationResult:
    """Accept only one current candidate exactly matching the complete intent."""
    if intent.artifact.compressed_size_bytes > limits.max_archive_compressed_bytes:
        return _denied(intent, ArtifactBoundary.INTAKE, ArtifactFailureCode.LIMIT_EXCEEDED)
    if candidates is None:
        return _denied(intent, ArtifactBoundary.EVIDENCE, ArtifactFailureCode.EVIDENCE_UNKNOWN)
    if not candidates:
        return _denied(intent, ArtifactBoundary.EVIDENCE, ArtifactFailureCode.EVIDENCE_MISSING)
    if len(candidates) != 1:
        return _denied(intent, ArtifactBoundary.EVIDENCE, ArtifactFailureCode.EVIDENCE_AMBIGUOUS)
    if current_target is None:
        return _denied(intent, ArtifactBoundary.TARGET, ArtifactFailureCode.EVIDENCE_UNKNOWN)
    if current_target.printer_id != intent.target.printer_id:
        return _denied(
            intent,
            ArtifactBoundary.TARGET,
            ArtifactFailureCode.EVIDENCE_CONTRADICTORY,
        )
    if current_target != intent.target:
        return _denied(intent, ArtifactBoundary.TARGET, ArtifactFailureCode.EVIDENCE_STALE)

    candidate = candidates[0]
    if candidate.artifact != intent.artifact:
        return _denied(
            intent,
            ArtifactBoundary.INTAKE,
            ArtifactFailureCode.EVIDENCE_CONTRADICTORY,
        )
    if (
        candidate.selected_plate.plate_id != intent.selected_plate.plate_id
        or candidate.selected_plate.archive_path != intent.selected_plate.archive_path
    ):
        return _denied(
            intent,
            ArtifactBoundary.SELECTED_PLATE,
            ArtifactFailureCode.EVIDENCE_CONTRADICTORY,
        )
    if candidate.target != intent.target:
        return _denied(
            intent,
            ArtifactBoundary.TARGET,
            ArtifactFailureCode.EVIDENCE_CONTRADICTORY,
        )
    if (
        candidate.zip_entry_count > limits.max_zip_entries
        or candidate.archive_expanded_bytes > limits.max_archive_expanded_bytes
        or candidate.highest_ratio_entry.expanded_bytes
        > limits.max_compression_ratio * candidate.highest_ratio_entry.compressed_bytes
    ):
        return _denied(intent, ArtifactBoundary.ARCHIVE, ArtifactFailureCode.LIMIT_EXCEEDED)
    if candidate.selected_plate.gcode_size_bytes > limits.max_gcode_bytes:
        return _denied(
            intent,
            ArtifactBoundary.SELECTED_PLATE,
            ArtifactFailureCode.LIMIT_EXCEEDED,
        )
    return ArtifactOperationResult(
        contract_version=CONTRACT_VERSION,
        operation_id=intent.operation_id,
        state=ArtifactOperationState.VALIDATED,
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
