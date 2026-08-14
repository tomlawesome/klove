"""Typed, non-actuating Moonraker upload and remote-file evidence."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from klove.domain.artifacts import (
    ArtifactQualification,
    CanonicalUuid4,
    Sha256Digest,
)

MOONRAKER_GCODE_PATH_PATTERN = (
    r"^klove/[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\.gcode$"
)
MoonrakerGcodePath = Annotated[
    str,
    StringConstraints(pattern=MOONRAKER_GCODE_PATH_PATTERN, max_length=49),
]
FileProcessorName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$"),
]


class UploadState(StrEnum):
    """Terminal states for one bounded upload operation."""

    VERIFIED = "verified"
    DENIED = "denied"
    OUTCOME_UNKNOWN = "outcome_unknown"


class UploadBoundary(StrEnum):
    """Stable boundaries at which upload evidence can fail closed."""

    IDEMPOTENCY = "idempotency"
    QUALIFICATION = "qualification"
    SOURCE = "source"
    TRANSPORT = "transport"
    METADATA = "metadata"
    REMOTE_FILE = "remote_file"


class UploadFailureCode(StrEnum):
    """Non-reflective upload failure classifications."""

    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    CAPACITY_EXHAUSTED = "capacity_exhausted"
    TARGET_STALE = "target_stale"
    SOURCE_INVALID = "source_invalid"
    TRANSPORT_AMBIGUOUS = "transport_ambiguous"
    METADATA_TIMEOUT = "metadata_timeout"
    REMOTE_MISMATCH = "remote_mismatch"


class UploadFailure(BaseModel):
    """One bounded denial or ambiguity reason."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    boundary: UploadBoundary
    code: UploadFailureCode


class MoonrakerUploadReceipt(BaseModel):
    """Strict fields accepted from Moonraker's successful upload response."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: MoonrakerGcodePath
    size_bytes: int = Field(gt=0)
    modified: Decimal = Field(gt=0, allow_inf_nan=False)


class MoonrakerGcodeMetadata(BaseModel):
    """Selected metadata fields used as remote identity and safety evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: MoonrakerGcodePath
    size_bytes: int = Field(gt=0)
    modified: Decimal = Field(gt=0, allow_inf_nan=False)
    metadata_uuid: CanonicalUuid4
    file_processors: tuple[FileProcessorName, ...] = Field(max_length=16)
    nozzle_diameter_micrometres: int = Field(ge=50, le=5_000)
    gcode_start_byte: int = Field(ge=0)
    gcode_end_byte: int = Field(gt=0)
    job_id: None
    print_start_time: None

    @model_validator(mode="after")
    def command_offsets_fit_file(self) -> MoonrakerGcodeMetadata:
        """Require one non-empty command region wholly within the remote file."""
        if self.gcode_start_byte >= self.gcode_end_byte:
            raise ValueError("G-code command offsets are not increasing")
        if self.gcode_end_byte > self.size_bytes:
            raise ValueError("G-code command offsets exceed the file")
        return self


class RemoteFileDigest(BaseModel):
    """Evidence obtained by streaming the exact remote file back through SHA-256."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    size_bytes: int = Field(gt=0)
    sha256: Sha256Digest


class VerifiedUpload(BaseModel):
    """One exact target-bound remote file proven coherent and not started."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    root: Literal["gcodes"] = "gcodes"
    path: MoonrakerGcodePath
    qualification: ArtifactQualification
    receipt: MoonrakerUploadReceipt
    metadata: MoonrakerGcodeMetadata
    remote_file: RemoteFileDigest

    @model_validator(mode="after")
    def evidence_is_exactly_bound(self) -> VerifiedUpload:
        """Keep every remote identity bound to the qualified selected bytes."""
        expected_path = f"klove/{self.qualification.operation_id}.gcode"
        expected_plate = self.qualification.selected_plate
        if self.path != expected_path:
            raise ValueError("upload path does not match the operation")
        if self.receipt.path != self.path or self.metadata.path != self.path:
            raise ValueError("remote identities do not match the upload path")
        if (
            self.receipt.size_bytes != expected_plate.gcode_size_bytes
            or self.metadata.size_bytes != expected_plate.gcode_size_bytes
            or self.remote_file.size_bytes != expected_plate.gcode_size_bytes
        ):
            raise ValueError("remote sizes do not match the selected G-code")
        if self.receipt.modified != self.metadata.modified:
            raise ValueError("remote timestamps do not match")
        if self.remote_file.sha256 != expected_plate.gcode_sha256:
            raise ValueError("remote digest does not match the selected G-code")
        if self.metadata.file_processors:
            raise ValueError("processed G-code cannot retain exact source identity")
        return self


class UploadOperationResult(BaseModel):
    """Terminal result for one operation and idempotency identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operation_id: CanonicalUuid4
    idempotency_key: CanonicalUuid4
    state: UploadState
    failure: UploadFailure | None = None
    verified: VerifiedUpload | None = None

    @model_validator(mode="after")
    def payload_matches_state(self) -> UploadOperationResult:
        """Prevent an ambiguous or denied operation from carrying upload authority."""
        if self.state is UploadState.VERIFIED:
            if self.verified is None:
                raise ValueError("verified uploads require remote-file evidence")
            if self.failure is not None:
                raise ValueError("verified uploads must not contain a failure")
        else:
            if self.failure is None:
                raise ValueError("non-verified uploads require a failure")
            if self.verified is not None:
                raise ValueError("non-verified uploads must not contain remote-file evidence")
        if self.verified is not None and (
            self.verified.qualification.operation_id != self.operation_id
            or self.verified.qualification.idempotency_key != self.idempotency_key
        ):
            raise ValueError("verified upload identity does not match the operation")
        return self
