from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).parents[1] / "fixtures" / "grove-observations"
PROFILE = FIXTURES / "mqtt-control-request-schema"
MANIFEST = FIXTURES / "mqtt-control-request-schema.manifest.json"


def profile_document() -> dict[str, object]:
    loaded = json.loads(PROFILE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_control_request_evidence_is_exact_sanitized_schema_only() -> None:
    document = profile_document()

    assert set(document) == {"profile_version", "transport", "observed"}
    assert document["profile_version"] == 1
    assert document["transport"] == "mqtt-over-tls"
    observed = document["observed"]
    assert isinstance(observed, dict)
    assert set(observed) == {"control_requests", "post_control_quiescence_observed"}
    assert observed["post_control_quiescence_observed"] is True
    assert observed["control_requests"] == [
        {
            "operation": "pause",
            "topic": "device/{serial}/request",
            "qos": 1,
            "dup": False,
            "retain": False,
            "members": _print_request_schema(),
            "generated_sequence_marker": True,
            "payload_bytes": 51,
            "request_occurrence": "first_observed_publish",
            "puback_after_publish": True,
        },
        {
            "operation": "resume",
            "topic": "device/{serial}/request",
            "qos": 1,
            "dup": False,
            "retain": False,
            "members": _print_request_schema(),
            "generated_sequence_marker": True,
            "payload_bytes": 52,
            "request_occurrence": "first_observed_publish",
            "puback_after_publish": True,
        },
        {
            "operation": "stop",
            "topic": "device/{serial}/request",
            "qos": 1,
            "dup": False,
            "retain": False,
            "members": _print_request_schema(),
            "generated_sequence_marker": True,
            "payload_bytes": 50,
            "request_occurrence": "first_observed_publish",
            "puback_after_publish": True,
        },
        {
            "operation": "pause",
            "topic": "device/{serial}/request",
            "qos": 1,
            "dup": False,
            "retain": False,
            "members": _print_request_schema(),
            "generated_sequence_marker": True,
            "payload_bytes": 51,
            "request_occurrence": "separate_publish_same_payload",
            "prior_publish_dup": False,
            "matches_prior_publish": False,
            "puback_after_publish": True,
        },
    ]


def _print_request_schema() -> dict[str, object]:
    return {
        "type": "object",
        "members": [
            {
                "name": "print",
                "present": True,
                "schema": {
                    "type": "object",
                    "members": [
                        {"name": "command", "present": True, "schema": {"type": "string"}},
                        {
                            "name": "sequence_id",
                            "present": True,
                            "schema": {"type": "string"},
                        },
                    ],
                },
            }
        ],
    }


def test_control_request_evidence_excludes_payloads_and_sensitive_capture_material() -> None:
    serialized = PROFILE.read_text(encoding="ascii")

    for forbidden in (
        '"payload":',
        "packet_id",
        "MQTTOBS0000001",
        "TEST0000",
        "BEGIN CERTIFICATE",
        "PRIVATE KEY",
    ):
        assert forbidden not in serialized


def test_manifest_classifies_each_retained_control_value() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert manifest["retained_values"] == [
        {"locator": "transport", "classification": "required_protocol_token"},
        {
            "locator": "observed.control_requests[].operation",
            "classification": "required_protocol_token",
        },
        {
            "locator": "observed.control_requests[].topic",
            "classification": "required_protocol_token",
        },
        {
            "locator": "observed.control_requests[].qos",
            "classification": "externally_observed_fact",
        },
        {
            "locator": "observed.control_requests[].dup",
            "classification": "externally_observed_fact",
        },
        {
            "locator": "observed.control_requests[].retain",
            "classification": "externally_observed_fact",
        },
        {
            "locator": "observed.control_requests[].members",
            "classification": "externally_observed_fact",
        },
        {
            "locator": "observed.control_requests[].generated_sequence_marker",
            "classification": "externally_observed_fact",
        },
        {
            "locator": "observed.control_requests[].payload_bytes",
            "classification": "externally_observed_fact",
        },
        {
            "locator": "observed.control_requests[].request_occurrence",
            "classification": "externally_observed_fact",
        },
        {
            "locator": "observed.control_requests[].prior_publish_dup",
            "classification": "externally_observed_fact",
        },
        {
            "locator": "observed.control_requests[].matches_prior_publish",
            "classification": "externally_observed_fact",
        },
        {
            "locator": "observed.control_requests[].puback_after_publish",
            "classification": "externally_observed_fact",
        },
        {
            "locator": "observed.post_control_quiescence_observed",
            "classification": "externally_observed_fact",
        },
    ]
