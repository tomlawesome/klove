"""Durable identities for authenticated Grove MQTT control ingress."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from klove.domain.artifacts import CanonicalUuid4
from klove.domain.control import ControlOperation, ControlResult, ControlStatus

_DecimalIdentity = Annotated[str, StringConstraints(pattern=r"^[0-9]{1,64}$")]
_HexDigest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class MqttIngressState(StrEnum):
    """Crash-safe command states; a reservation may already have dispatched."""

    RESERVED = "reserved"
    COMPLETE = "complete"
    OUTCOME_UNKNOWN = "outcome_unknown"


class MqttIngressRecord(BaseModel):
    """Secret-free binding between one Grove identity and one control operation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    record_version: Literal["1"] = "1"
    printer_uuid: CanonicalUuid4
    printer_record_revision: int = Field(ge=1, le=9_223_372_036_854_775_807)
    sequence_id: _DecimalIdentity
    operation: ControlOperation
    payload_hash: _HexDigest
    state_token: _HexDigest
    idempotency_key: CanonicalUuid4
    state: MqttIngressState
    result: ControlResult | None = None

    @model_validator(mode="after")
    def result_matches_state_and_operation(self) -> MqttIngressRecord:
        """Reject impossible recovery evidence or a substituted operation result."""
        if self.result is not None and self.result.operation is not self.operation:
            raise ValueError("MQTT ingress result operation does not match")
        if self.state is MqttIngressState.RESERVED:
            valid = self.result is None
        elif self.state is MqttIngressState.OUTCOME_UNKNOWN:
            valid = self.result is not None and self.result.status is ControlStatus.OUTCOME_UNKNOWN
        else:
            valid = (
                self.result is not None and self.result.status is not ControlStatus.OUTCOME_UNKNOWN
            )
        if not valid:
            raise ValueError("MQTT ingress result does not match its durable state")
        return self


def completed_ingress(
    reserved: MqttIngressRecord,
    result: ControlResult,
) -> MqttIngressRecord:
    """Build the only terminal transition from an exact reservation."""
    if (
        reserved.state is not MqttIngressState.RESERVED
        or result.operation is not reserved.operation
    ):
        raise ValueError("invalid MQTT ingress completion")
    state = (
        MqttIngressState.OUTCOME_UNKNOWN
        if result.status is ControlStatus.OUTCOME_UNKNOWN
        else MqttIngressState.COMPLETE
    )
    return reserved.model_copy(update={"state": state, "result": result})


def unknown_ingress(reserved: MqttIngressRecord) -> MqttIngressRecord:
    """Conservatively recover a reservation whose dispatch boundary is unknown."""
    return completed_ingress(
        reserved,
        ControlResult(
            operation=reserved.operation,
            status=ControlStatus.OUTCOME_UNKNOWN,
            code="outcome_unknown",
        ),
    )
