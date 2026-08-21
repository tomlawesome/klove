"""Typed durable actuator-fence references owned by the registry catalogue."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from klove.domain.artifacts import CanonicalUuid4

FenceText = Annotated[str, StringConstraints(min_length=1, max_length=255)]
BoundedUnixMilliseconds = Annotated[int, Field(ge=0, le=9_223_372_036_854_775_807)]


class FenceKind(StrEnum):
    """The three actuator boundaries represented by the catalogue."""

    CONTROL = "control"
    PRINT_START = "print_start"
    COORDINATOR = "coordinator"


class FenceOwnerStore(StrEnum):
    """The closed set of owner journals accepted by ADR 0013."""

    CONTROL_JOURNAL = "control_journal"
    START_JOURNAL = "start_journal"
    DISPATCH_JOURNAL = "dispatch_journal"


class FenceState(StrEnum):
    """Catalogue states; resolved is terminal."""

    PREPARED = "prepared"
    ACTIVE = "active"
    RESOLVED = "resolved"


class FenceResolutionCode(StrEnum):
    """Closed terminal reasons that contain no owner error text."""

    NEVER_DISPATCHED = "never_dispatched"
    CONFIRMED = "confirmed"
    DENIED_BEFORE_DISPATCH = "denied_before_dispatch"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class FenceStoreMetadata(BaseModel):
    """Validated immutable identity for one owner store installation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    owner_store: FenceOwnerStore
    installation_uuid: CanonicalUuid4
    store_id: CanonicalUuid4
    schema_version: int = Field(ge=1, le=9_223_372_036_854_775_807)


class RegistryStoreMetadata(BaseModel):
    """Immutable identity of the registry catalogue installation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    installation_uuid: CanonicalUuid4
    store_id: CanonicalUuid4
    schema_version: int = Field(ge=1, le=9_223_372_036_854_775_807)


class FenceReference(BaseModel):
    """One catalogue reference, decoded into a state-specific subtype."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    fence_reference_id: CanonicalUuid4
    printer_uuid: CanonicalUuid4
    fence_kind: FenceKind
    owner_store: FenceOwnerStore
    store_id: CanonicalUuid4
    owner_schema_version: int = Field(ge=1, le=9_223_372_036_854_775_807)
    operation_id: CanonicalUuid4
    state: FenceState
    created_at_unix_ms: BoundedUnixMilliseconds
    transitioned_at_unix_ms: BoundedUnixMilliseconds
    resolution_code: FenceResolutionCode | None = None

    @model_validator(mode="after")
    def validate_reference(self) -> FenceReference:
        expected_owner = {
            FenceKind.CONTROL: FenceOwnerStore.CONTROL_JOURNAL,
            FenceKind.PRINT_START: FenceOwnerStore.START_JOURNAL,
            FenceKind.COORDINATOR: FenceOwnerStore.DISPATCH_JOURNAL,
        }[self.fence_kind]
        if self.owner_store is not expected_owner:
            raise ValueError("fence kind and owner store do not match")
        if self.transitioned_at_unix_ms < self.created_at_unix_ms:
            raise ValueError("fence transition predates creation")
        if self.state is FenceState.RESOLVED and self.resolution_code is None:
            raise ValueError("resolved fences require a resolution code")
        if self.state is not FenceState.RESOLVED and self.resolution_code is not None:
            raise ValueError("unresolved fences cannot have a resolution code")
        return self


class PreparedFenceReference(FenceReference):
    """A reference committed before the owner journal reservation."""

    state: Literal[FenceState.PREPARED] = FenceState.PREPARED
    resolution_code: None = None


class ActiveFenceReference(FenceReference):
    """A reference whose exact owner reservation has been read back."""

    state: Literal[FenceState.ACTIVE] = FenceState.ACTIVE
    resolution_code: None = None


class ResolvedFenceReference(FenceReference):
    """A terminal reference; terminal rows are retained forever."""

    state: Literal[FenceState.RESOLVED] = FenceState.RESOLVED
    resolution_code: FenceResolutionCode


class FenceTransition(BaseModel):
    """One immutable state transition event."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    fence_reference_id: CanonicalUuid4
    sequence: int = Field(ge=1, le=9_223_372_036_854_775_807)
    from_state: FenceState | None
    to_state: FenceState
    transitioned_at_unix_ms: BoundedUnixMilliseconds
    resolution_code: FenceResolutionCode | None = None

    @model_validator(mode="after")
    def validate_transition(self) -> FenceTransition:
        if self.sequence == 1 and self.from_state is not None:
            raise ValueError("the first fence transition has no predecessor")
        if self.sequence != 1 and self.from_state is None:
            raise ValueError("later fence transitions require a predecessor")
        if self.from_state is FenceState.PREPARED and self.to_state not in {
            FenceState.ACTIVE,
            FenceState.RESOLVED,
        }:
            raise ValueError("prepared fences have no other successor")
        if self.from_state is FenceState.ACTIVE and self.to_state is not FenceState.RESOLVED:
            raise ValueError("active fences have only a terminal successor")
        if self.to_state is FenceState.RESOLVED and self.resolution_code is None:
            raise ValueError("resolved transitions require a resolution code")
        if self.to_state is not FenceState.RESOLVED and self.resolution_code is not None:
            raise ValueError("unresolved transitions cannot have a resolution code")
        return self


class FencePageCursor(BaseModel):
    """Exclusive stable cursor for one exact printer's catalogue page."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    fence_reference_id: CanonicalUuid4


def reference_subtype(reference: FenceReference) -> type[FenceReference]:
    """Return the state-specific type for a validated base reference."""
    return cast(
        type[FenceReference],
        {
            FenceState.PREPARED: PreparedFenceReference,
            FenceState.ACTIVE: ActiveFenceReference,
            FenceState.RESOLVED: ResolvedFenceReference,
        }[reference.state],
    )


__all__ = (
    "ActiveFenceReference",
    "FenceKind",
    "FenceOwnerStore",
    "FencePageCursor",
    "FenceReference",
    "FenceResolutionCode",
    "FenceState",
    "FenceStoreMetadata",
    "FenceTransition",
    "PreparedFenceReference",
    "RegistryStoreMetadata",
    "ResolvedFenceReference",
    "reference_subtype",
)
