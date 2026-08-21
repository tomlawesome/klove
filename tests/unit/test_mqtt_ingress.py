from __future__ import annotations

import pytest
from pydantic import ValidationError

from klove.domain.control import ControlOperation, ControlResult, ControlStatus
from klove.domain.mqtt_ingress import (
    MqttIngressRecord,
    MqttIngressState,
    completed_ingress,
    unknown_ingress,
)

PRINTER = "11111111-1111-4111-8111-111111111111"
KEY = "22222222-2222-4222-8222-222222222222"
DIGEST = "a" * 64


def record(**updates: object) -> MqttIngressRecord:
    values: dict[str, object] = {
        "printer_uuid": PRINTER,
        "printer_record_revision": 1,
        "sequence_id": "123",
        "operation": ControlOperation.PAUSE,
        "payload_hash": DIGEST,
        "state_token": "b" * 64,
        "idempotency_key": KEY,
        "state": MqttIngressState.RESERVED,
    }
    values.update(updates)
    return MqttIngressRecord.model_validate(values)


def result(
    status: ControlStatus = ControlStatus.CONFIRMED,
    operation: ControlOperation = ControlOperation.PAUSE,
) -> ControlResult:
    return ControlResult(operation=operation, status=status, code="bounded_code")


def test_record_is_strict_frozen_versioned_and_secret_free() -> None:
    pending = record()
    assert pending.record_version == "1"
    assert "password" not in pending.model_dump(mode="json")
    with pytest.raises(ValidationError):
        record(printer_record_revision="1")
    with pytest.raises(ValidationError):
        MqttIngressRecord.model_validate({**pending.model_dump(), "unexpected": True})
    with pytest.raises(ValidationError):
        pending.state = MqttIngressState.COMPLETE


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"printer_record_revision": 0}, "greater than or equal"),
        ({"printer_record_revision": 9_223_372_036_854_775_808}, "less than or equal"),
        ({"sequence_id": ""}, "string_pattern_mismatch"),
        ({"sequence_id": "1" * 65}, "string_pattern_mismatch"),
        ({"sequence_id": "+1"}, "string_pattern_mismatch"),
        ({"payload_hash": "A" * 64}, "string_pattern_mismatch"),
        ({"state_token": "b" * 63}, "string_pattern_mismatch"),
        ({"record_version": "2"}, "literal_error"),
    ],
)
def test_record_rejects_out_of_contract_identities(
    updates: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        record(**updates)


@pytest.mark.parametrize(
    "updates",
    [
        {"state": MqttIngressState.RESERVED, "result": result()},
        {"state": MqttIngressState.COMPLETE, "result": None},
        {
            "state": MqttIngressState.COMPLETE,
            "result": result(ControlStatus.OUTCOME_UNKNOWN),
        },
        {"state": MqttIngressState.OUTCOME_UNKNOWN, "result": None},
        {
            "state": MqttIngressState.OUTCOME_UNKNOWN,
            "result": result(ControlStatus.DENIED),
        },
        {
            "state": MqttIngressState.COMPLETE,
            "result": result(operation=ControlOperation.CANCEL),
        },
    ],
)
def test_record_rejects_impossible_state_result_pairs(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        record(**updates)


@pytest.mark.parametrize("status", [ControlStatus.CONFIRMED, ControlStatus.DENIED])
def test_completion_preserves_identity_and_binds_terminal_result(status: ControlStatus) -> None:
    pending = record()
    terminal = completed_ingress(pending, result(status))
    assert terminal.state is MqttIngressState.COMPLETE
    assert terminal.result == result(status)
    assert terminal.model_dump(exclude={"state", "result"}) == pending.model_dump(
        exclude={"state", "result"}
    )
    assert pending.state is MqttIngressState.RESERVED


def test_unknown_recovery_is_stable_and_bounded() -> None:
    recovered = unknown_ingress(record())
    assert recovered.state is MqttIngressState.OUTCOME_UNKNOWN
    assert recovered.result == ControlResult(
        operation=ControlOperation.PAUSE,
        status=ControlStatus.OUTCOME_UNKNOWN,
        code="outcome_unknown",
    )


@pytest.mark.parametrize(
    "pending,outcome",
    [
        (record(state=MqttIngressState.COMPLETE, result=result()), result()),
        (record(), result(operation=ControlOperation.RESUME)),
    ],
)
def test_completion_rejects_redispatch_or_substituted_operation(
    pending: MqttIngressRecord, outcome: ControlResult
) -> None:
    with pytest.raises(ValueError, match="invalid MQTT ingress completion"):
        completed_ingress(pending, outcome)
