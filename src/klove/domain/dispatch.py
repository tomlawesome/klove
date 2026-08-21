"""Transport-neutral, registry-bound artifact dispatch contracts.

The request and durable record types intentionally contain only bounded,
non-secret identities.  They are consumed by the internal coordinator; no
northbound transport is implied by this module.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from klove.domain.artifacts import (
    ArtifactFormat,
    ArtifactIntake,
    ArtifactQualification,
    ArtifactTarget,
    BoundedIdentifier,
    CanonicalUuid4,
    PlateSelection,
    SafetyProfileFingerprint,
    Sha256Digest,
)
from klove.domain.start import StartOperationResult, StartState
from klove.domain.upload import MoonrakerGcodePath, VerifiedUpload

DISPATCH_CONTRACT_VERSION: Literal["1"] = "1"


class DispatchGrant(BaseModel):
    """Trusted ingress context for one already-authorized exact printer."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    principal_id: BoundedIdentifier
    printer_uuid: CanonicalUuid4


class DispatchRequest(BaseModel):
    """One bounded hostile submission after its transport has authenticated it."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_version: Literal["1"] = DISPATCH_CONTRACT_VERSION
    operation_id: CanonicalUuid4
    idempotency_key: CanonicalUuid4
    printer_uuid: CanonicalUuid4
    slicer_profile_id: BoundedIdentifier
    safety_profile_generation: int = Field(ge=1, le=9_223_372_036_854_775_807)
    safety_profile_fingerprint: SafetyProfileFingerprint
    selected_plate: PlateSelection
    archive_sha256: Sha256Digest
    archive_size_bytes: int = Field(gt=0, le=4 * 1024 * 1024 * 1024)

    @property
    def artifact(self) -> ArtifactIntake:
        """Derive the internal artifact identity without accepting a second ID."""
        return ArtifactIntake(
            artifact_id=self.operation_id,
            format=ArtifactFormat.GCODE_3MF,
            archive_sha256=self.archive_sha256,
            compressed_size_bytes=self.archive_size_bytes,
        )

    @property
    def target(self) -> ArtifactTarget:
        """Return the exact target fields that must match the live registry profile."""
        return ArtifactTarget(
            printer_uuid=self.printer_uuid,
            slicer_profile_id=self.slicer_profile_id,
            safety_profile_generation=self.safety_profile_generation,
            safety_profile_fingerprint=self.safety_profile_fingerprint,
        )


class DispatchState(StrEnum):
    """Durable coordinator lifecycle states, including all uncertainty fences."""

    RECEIVING = "receiving"
    ACCEPTED = "accepted"
    UPLOADING = "uploading"
    VERIFIED = "verified"
    STARTING = "starting"
    PRINTING = "printing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class DispatchFailureCode(StrEnum):
    """Closed failure classes that never reflect source or transport data."""

    ACCESS_DENIED = "access_denied"
    PRINTER_UNAVAILABLE = "printer_unavailable"
    DISPATCH_DISABLED = "dispatch_disabled"
    PROFILE_STALE = "profile_stale"
    OPERATION_CONFLICT = "operation_conflict"
    PRINTER_FENCED = "printer_fenced"
    CAPACITY_EXHAUSTED = "capacity_exhausted"
    SOURCE_INVALID = "source_invalid"
    SOURCE_MISSING = "source_missing"
    QUALIFICATION_DENIED = "qualification_denied"
    UPLOAD_DENIED = "upload_denied"
    UPLOAD_AMBIGUOUS = "upload_ambiguous"
    START_DENIED = "start_denied"
    START_AMBIGUOUS = "start_ambiguous"
    PRINT_FAILED = "print_failed"
    CANCELLED = "cancelled"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    RECONCILIATION_PENDING = "reconciliation_pending"
    INTERNAL_FAILURE = "internal_failure"


_DURABLE_FAILED_CODES: Final = frozenset(
    {
        DispatchFailureCode.PRINTER_UNAVAILABLE,
        DispatchFailureCode.DISPATCH_DISABLED,
        DispatchFailureCode.PROFILE_STALE,
        DispatchFailureCode.CAPACITY_EXHAUSTED,
        DispatchFailureCode.SOURCE_INVALID,
        DispatchFailureCode.SOURCE_MISSING,
        DispatchFailureCode.QUALIFICATION_DENIED,
        DispatchFailureCode.UPLOAD_DENIED,
        DispatchFailureCode.START_DENIED,
        DispatchFailureCode.PRINT_FAILED,
    }
)
_OUTCOME_UNKNOWN_CODES: Final = frozenset(
    {
        DispatchFailureCode.UPLOAD_AMBIGUOUS,
        DispatchFailureCode.START_AMBIGUOUS,
        DispatchFailureCode.INTERNAL_FAILURE,
    }
)
_PRE_RESERVATION_FAILED_CODES: Final = frozenset(
    {
        DispatchFailureCode.ACCESS_DENIED,
        DispatchFailureCode.OPERATION_CONFLICT,
        DispatchFailureCode.PRINTER_FENCED,
        DispatchFailureCode.STORAGE_UNAVAILABLE,
        DispatchFailureCode.RECONCILIATION_PENDING,
    }
)
_RESULT_FAILED_CODES: Final = _DURABLE_FAILED_CODES | _PRE_RESERVATION_FAILED_CODES


class DispatchFailure(BaseModel):
    """One bounded coordinator failure without hostile text or remote responses."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    code: DispatchFailureCode


class DispatchJournalRecord(BaseModel):
    """The complete secret-free durable coordinator evidence for one operation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    grant: DispatchGrant
    request: DispatchRequest
    state: DispatchState
    qualification: ArtifactQualification | None = None
    verified: VerifiedUpload | None = None
    start: StartOperationResult | None = None
    failure: DispatchFailure | None = None

    @model_validator(mode="after")
    def evidence_matches_one_operation(self) -> DispatchJournalRecord:  # noqa: PLR0912
        """Reject substituted evidence and impossible lifecycle combinations."""
        request = self.request
        if self.grant.printer_uuid != request.printer_uuid:
            raise ValueError("grant does not match request printer")
        if self.qualification is not None and (
            self.qualification.operation_id != request.operation_id
            or self.qualification.idempotency_key != request.idempotency_key
            or self.qualification.artifact != request.artifact
            or self.qualification.target != request.target
        ):
            raise ValueError("qualification does not match dispatch request")
        if self.verified is not None and self.verified.qualification != self.qualification:
            raise ValueError("verified upload does not match qualification")
        if self.start is not None and (
            self.start.operation_id != request.operation_id
            or self.start.idempotency_key != request.idempotency_key
        ):
            raise ValueError("start result does not match dispatch request")
        if self.start is not None:
            confirmation = self.start.confirmation
            if (
                self.verified is None
                or self.start.state is not StartState.CONFIRMED
                or confirmation is None
                or confirmation.path != self.verified.path
                or confirmation.job.filename != self.verified.path
            ):
                raise ValueError("start evidence does not match the verified upload")
        has_qualification = self.qualification is not None
        has_verified = self.verified is not None
        if self.state in {DispatchState.RECEIVING, DispatchState.ACCEPTED}:
            valid = not has_qualification and not has_verified and self.start is None
        elif self.state is DispatchState.UPLOADING:
            valid = has_qualification and not has_verified and self.start is None
        elif self.state in {DispatchState.VERIFIED, DispatchState.STARTING}:
            valid = has_qualification and has_verified and self.start is None
        elif self.state in {DispatchState.PRINTING, DispatchState.COMPLETED}:
            valid = (
                has_qualification
                and has_verified
                and self.start is not None
                and self.start.state is StartState.CONFIRMED
            )
        else:
            valid = True
        if not valid:
            raise ValueError("dispatch lifecycle evidence is inconsistent")
        if self.state is DispatchState.FAILED:
            if self.failure is None or self.failure.code not in _DURABLE_FAILED_CODES:
                raise ValueError("failed dispatches require one durable failure code")
        elif self.state is DispatchState.OUTCOME_UNKNOWN:
            if self.failure is None or self.failure.code not in _OUTCOME_UNKNOWN_CODES:
                raise ValueError("unknown dispatches require one ambiguity failure code")
        elif self.state is DispatchState.CANCELLED:
            if self.failure is None or self.failure.code is not DispatchFailureCode.CANCELLED:
                raise ValueError("cancelled dispatches require their exact failure")
        elif self.failure is not None:
            raise ValueError("non-terminal dispatches cannot contain a failure")
        return self


class DispatchOperationResult(BaseModel):
    """Safe lookup result for an exact authenticated operation identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_version: Literal["1"] = DISPATCH_CONTRACT_VERSION
    operation_id: CanonicalUuid4
    printer_uuid: CanonicalUuid4
    state: DispatchState
    target: ArtifactTarget
    archive_sha256: Sha256Digest
    archive_size_bytes: int = Field(gt=0, le=4 * 1024 * 1024 * 1024)
    selected_gcode_sha256: Sha256Digest | None = None
    remote_path: MoonrakerGcodePath | None = None
    failure: DispatchFailure | None = None

    @model_validator(mode="after")
    def result_is_bound_to_its_target(self) -> DispatchOperationResult:
        """Prevent a result from leaking a substituted target or partial payload."""
        if self.printer_uuid != self.target.printer_uuid:
            raise ValueError("result printer does not match its target")
        if self.remote_path is not None and (
            self.remote_path != f"klove/{self.operation_id}.gcode"
        ):
            raise ValueError("result remote path does not match its operation")
        if self.state is DispatchState.FAILED:
            if self.failure is None or self.failure.code not in _RESULT_FAILED_CODES:
                raise ValueError("failed result requires one admitted failure code")
        elif self.state is DispatchState.OUTCOME_UNKNOWN:
            if self.failure is None or self.failure.code not in _OUTCOME_UNKNOWN_CODES:
                raise ValueError("unknown result requires one ambiguity failure code")
        elif self.state is DispatchState.CANCELLED:
            if self.failure is None or self.failure.code is not DispatchFailureCode.CANCELLED:
                raise ValueError("cancelled result requires its exact failure code")
        elif self.failure is not None:
            raise ValueError("non-failed result cannot include a failure")
        return self


def result_for_record(record: DispatchJournalRecord) -> DispatchOperationResult:
    """Project durable evidence to the small non-secret lookup representation."""
    qualification = record.qualification
    verified = record.verified
    return DispatchOperationResult(
        operation_id=record.request.operation_id,
        printer_uuid=record.request.printer_uuid,
        state=record.state,
        target=record.request.target,
        archive_sha256=record.request.archive_sha256,
        archive_size_bytes=record.request.archive_size_bytes,
        selected_gcode_sha256=(
            qualification.selected_plate.gcode_sha256 if qualification is not None else None
        ),
        remote_path=verified.path if verified is not None else None,
        failure=record.failure,
    )
