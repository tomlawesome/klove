"""Strict, bounded evidence for the durable job-control owner journal."""

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

from klove.domain.artifacts import CanonicalUuid4
from klove.domain.control import ControlOperation
from klove.domain.models import JobHistoryStatus, PrinterPhase

CONTROL_JOURNAL_CONTRACT_VERSION: Literal["1"] = "1"
StateToken = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
JobId = Annotated[str, StringConstraints(pattern=r"^[0-9A-F]{6,16}$")]


class ControlJournalState(StrEnum):
    """Durable owner states and their terminal meanings."""

    DISPATCHING = "dispatching"
    OUTCOME_UNKNOWN = "outcome_unknown"
    CONFIRMED = "confirmed"
    DENIED_BEFORE_DISPATCH = "denied_before_dispatch"
    EVIDENCE_SUPERSEDED = "evidence_superseded"


class ControlJournalFailureCode(StrEnum):
    """Closed, non-reflective reasons for non-confirmed owner evidence."""

    PRECHECK_DENIED = "precheck_denied"
    PRE_FLIGHT_UNAVAILABLE = "preflight_unavailable"
    STATE_TOKEN_CHANGED = "state_token_changed"  # noqa: S105
    TRANSPORT_AMBIGUOUS = "transport_ambiguous"
    CONFIRMATION_TIMEOUT = "confirmation_timeout"
    JOURNAL_UNAVAILABLE = "journal_unavailable"
    INTERNAL_AMBIGUOUS = "internal_ambiguous"


class ControlJournalFailure(BaseModel):
    """One bounded failure classification without exception or peer text."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    code: ControlJournalFailureCode


class ControlJournalIdentity(BaseModel):
    """Immutable identity used to relate this owner store to the registry."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    installation_id: CanonicalUuid4
    store_id: CanonicalUuid4
    schema_version: int = Field(ge=1)


class ControlJournalEvidence(BaseModel):
    """One immutable job-bound control observation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    state_token: StateToken | None = None
    eventtime: float = Field(ge=0)
    phase: PrinterPhase
    job_id: JobId
    job_start_time: float = Field(ge=0)
    filename: str = Field(min_length=1, max_length=1024)
    file_position: int = Field(ge=0, le=4 * 1024 * 1024 * 1024)
    job_status: JobHistoryStatus = JobHistoryStatus.IN_PROGRESS

    @field_validator("filename")
    @classmethod
    def filename_is_safe_text(cls, value: str) -> str:
        """Reject NUL and control characters from durable evidence."""
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
            raise ValueError("filename contains control characters")
        return value


class ControlJournalRecord(BaseModel):
    """Complete bounded owner evidence sufficient for restart reconciliation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_version: Literal["1"] = CONTROL_JOURNAL_CONTRACT_VERSION
    operation_id: CanonicalUuid4
    idempotency_key: CanonicalUuid4
    printer_uuid: CanonicalUuid4
    operation: ControlOperation
    state_token: StateToken
    preflight: ControlJournalEvidence
    state: ControlJournalState
    failure: ControlJournalFailure | None = None
    terminal_evidence: ControlJournalEvidence | None = None

    @property
    def is_unresolved(self) -> bool:
        """Whether this row still fences its printer."""
        return self.state in {
            ControlJournalState.DISPATCHING,
            ControlJournalState.OUTCOME_UNKNOWN,
        }

    @property
    def is_terminal(self) -> bool:
        """Whether this row has a closed owner outcome."""
        return not self.is_unresolved

    @model_validator(mode="after")
    def validate_terminal_payload(self) -> ControlJournalRecord:
        state = self.state
        failure = self.failure
        evidence = self.terminal_evidence
        if state is ControlJournalState.DISPATCHING:
            if failure is not None or evidence is not None:
                raise ValueError("dispatching rows cannot contain terminal evidence")
        elif state is ControlJournalState.OUTCOME_UNKNOWN:
            if not isinstance(failure, ControlJournalFailure) or evidence is not None:
                raise ValueError("unknown rows require one failure")
        elif state is ControlJournalState.DENIED_BEFORE_DISPATCH:
            if not isinstance(failure, ControlJournalFailure) or evidence is not None:
                raise ValueError("denied rows require one failure")
        elif state in {
            ControlJournalState.CONFIRMED,
            ControlJournalState.EVIDENCE_SUPERSEDED,
        }:
            if (
                failure is not None
                or not isinstance(evidence, ControlJournalEvidence)
                or evidence.state_token is None
            ):
                raise ValueError("resolved evidence is incomplete")
        else:
            raise ValueError("unknown control journal state")
        return self


# Short aliases make the evidence names convenient at the owner boundary while
# retaining the explicit journal contract names for callers and type checkers.
ControlEvidence = ControlJournalEvidence
ControlFailure = ControlJournalFailure

__all__ = [
    "CONTROL_JOURNAL_CONTRACT_VERSION",
    "ControlEvidence",
    "ControlFailure",
    "ControlJournalEvidence",
    "ControlJournalFailure",
    "ControlJournalFailureCode",
    "ControlJournalIdentity",
    "ControlJournalRecord",
    "ControlJournalState",
    "JobId",
    "StateToken",
]
