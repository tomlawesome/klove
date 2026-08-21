from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from klove.northbound.mqtt import messages
from klove.northbound.mqtt.messages import (
    MAX_MESSAGE_PROFILE_BYTES,
    assess_mqtt_message_schema,
)

PROFILE = Path(__file__).parents[1] / "fixtures" / "grove-observations" / "mqtt-message-schema"


def test_exact_sanitized_message_schema_is_non_runtime_evidence_only() -> None:
    assessment = assess_mqtt_message_schema(PROFILE.read_bytes())

    assert assessment.accepted is True
    assert assessment.code is None
    assert assessment.profile is not None
    assert assessment.profile.report_topic == "device/{serial}/report"
    assert assessment.profile.observed_reply_commands == frozenset({"pushall", "get_version"})
    assert assessment.profile.field_evidence == "observed_fields_only"
    assert {
        "Grove initial pushall request schema",
        "Grove initial get_version request schema",
        "Grove initial extrusion_cali_get request schema",
        "pause request schema after public control",
        "resume request schema after public control",
        "stop request schema after public control",
    }.issubset(assessment.profile.not_observed)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b"{",
        b'{"profile_version":1,"profile_version":1}',
        b"\xff",
        b"x" * (MAX_MESSAGE_PROFILE_BYTES + 1),
        json.dumps({"profile_version": 1}).encode(),
        PROFILE.read_bytes().replace(b"push_status", b"push_statuS"),
    ],
)
def test_changed_malformed_or_oversized_message_schema_fails_closed(raw: bytes) -> None:
    assessment = assess_mqtt_message_schema(raw)

    assert assessment.accepted is False
    assert assessment.profile is None
    assert assessment.code in {"message_profile_invalid", "message_profile_too_large"}


@given(st.binary(max_size=MAX_MESSAGE_PROFILE_BYTES))
def test_arbitrary_bounded_message_schema_bytes_never_raise(raw: bytes) -> None:
    assessment = assess_mqtt_message_schema(raw)

    assert assessment.accepted is (assessment.profile is not None)
    if assessment.profile is None:
        assert assessment.code == "message_profile_invalid"


@pytest.mark.parametrize(
    "document",
    [
        {"profile_version": 1},
        {
            "profile_version": 1,
            "transport": "mqtt-over-tls",
            "upstream_revision": "revision",
            "generated_serial": "{serial}",
            "observed": {},
            "not_observed": [],
        },
        {
            "profile_version": 1,
            "transport": "mqtt-over-tls",
            "upstream_revision": "revision",
            "generated_serial": "{serial}",
            "observed": {
                "report_topic": "topic",
                "request_replies": {},
                "required_fields": {},
            },
            "not_observed": [],
        },
        {
            "profile_version": 1,
            "transport": "mqtt-over-tls",
            "upstream_revision": "revision",
            "generated_serial": "{serial}",
            "observed": {"report_topic": "topic", "request_replies": {}},
            "not_observed": [1],
        },
        {
            "profile_version": 1,
            "transport": "mqtt-over-tls",
            "upstream_revision": "revision",
            "generated_serial": "{serial}",
            "observed": {"report_topic": 1, "request_replies": {}},
            "not_observed": [],
        },
        {
            "profile_version": 1,
            "transport": "mqtt-over-tls",
            "upstream_revision": "revision",
            "generated_serial": "{serial}",
            "observed": {"report_topic": "topic", "request_replies": []},
            "not_observed": [],
        },
    ],
)
def test_structure_checks_remain_fail_closed_when_exact_digest_is_rebound(
    document: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = json.dumps(document, separators=(",", ":")).encode()
    monkeypatch.setattr(messages, "_EXPECTED_SHA256", hashlib.sha256(raw).hexdigest())

    assessment = assess_mqtt_message_schema(raw)

    assert assessment == messages.MqttMessageSchemaAssessment(
        False, code="message_profile_invalid"
    )
