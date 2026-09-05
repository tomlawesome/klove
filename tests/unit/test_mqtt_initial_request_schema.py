from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).parents[1] / "fixtures" / "grove-observations"
PROFILE = FIXTURES / "mqtt-initial-request-schema"
MANIFEST = FIXTURES / "mqtt-initial-request-schema.manifest.json"


def profile_document() -> dict[str, object]:
    loaded = json.loads(PROFILE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_initial_request_evidence_is_exact_sanitized_schema_only() -> None:
    document = profile_document()

    assert set(document) == {"profile_version", "transport", "observed"}
    assert document["profile_version"] == 1
    assert document["transport"] == "mqtt-over-tls"
    observed = document["observed"]
    assert isinstance(observed, dict)
    assert set(observed) == {"initial_requests", "first_puback_after_next_initial_publish"}
    assert observed["first_puback_after_next_initial_publish"] is True
    assert observed["initial_requests"] == [
        {
            "topic": "device/{serial}/request",
            "qos": 1,
            "dup": False,
            "retain": False,
            "command": "pushall",
            "members": {
                "type": "object",
                "members": [
                    {
                        "name": "pushing",
                        "present": True,
                        "schema": {
                            "type": "object",
                            "members": [
                                {
                                    "name": "command",
                                    "present": True,
                                    "schema": {"type": "string"},
                                }
                            ],
                        },
                    }
                ],
            },
            "generated_sequence_marker": False,
            "payload_bytes": 35,
            "puback_order": "after_next_initial_publish",
        },
        {
            "topic": "device/{serial}/request",
            "qos": 1,
            "dup": False,
            "retain": False,
            "command": "get_version",
            "members": {
                "type": "object",
                "members": [
                    {
                        "name": "info",
                        "present": True,
                        "schema": {
                            "type": "object",
                            "members": [
                                {
                                    "name": "sequence_id",
                                    "present": True,
                                    "schema": {"type": "string"},
                                },
                                {
                                    "name": "command",
                                    "present": True,
                                    "schema": {"type": "string"},
                                },
                            ],
                        },
                    }
                ],
            },
            "generated_sequence_marker": True,
            "payload_bytes": 56,
            "puback_order": "after_publish",
        },
        {
            "topic": "device/{serial}/request",
            "qos": 1,
            "dup": False,
            "retain": False,
            "command": "extrusion_cali_get",
            "members": {
                "type": "object",
                "members": [
                    {
                        "name": "print",
                        "present": True,
                        "schema": {
                            "type": "object",
                            "members": [
                                {
                                    "name": "command",
                                    "present": True,
                                    "schema": {"type": "string"},
                                },
                                {
                                    "name": "filament_id",
                                    "present": True,
                                    "schema": {"type": "string"},
                                },
                                {
                                    "name": "nozzle_diameter",
                                    "present": True,
                                    "schema": {"type": "string"},
                                },
                                {
                                    "name": "sequence_id",
                                    "present": True,
                                    "schema": {"type": "string"},
                                },
                            ],
                        },
                    }
                ],
            },
            "generated_sequence_marker": True,
            "payload_bytes": 109,
            "puback_order": "after_publish",
        },
    ]


def test_initial_request_evidence_excludes_payloads_and_sensitive_capture_material() -> None:
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


def test_manifest_classifies_each_retained_request_value() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert manifest["retained_values"] == [
        {"locator": "transport", "classification": "required_protocol_token"},
        {
            "locator": "observed.initial_requests[].topic",
            "classification": "required_protocol_token",
        },
        {
            "locator": "observed.initial_requests[].command",
            "classification": "required_protocol_token",
        },
        {
            "locator": "observed.initial_requests[].qos",
            "classification": "externally_observed_fact",
        },
        {
            "locator": "observed.initial_requests[].dup",
            "classification": "externally_observed_fact",
        },
        {
            "locator": "observed.initial_requests[].retain",
            "classification": "externally_observed_fact",
        },
        {
            "locator": "observed.initial_requests[].members",
            "classification": "externally_observed_fact",
        },
        {
            "locator": "observed.initial_requests[].generated_sequence_marker",
            "classification": "externally_observed_fact",
        },
        {
            "locator": "observed.initial_requests[].payload_bytes",
            "classification": "externally_observed_fact",
        },
        {
            "locator": "observed.initial_requests[].puback_order",
            "classification": "externally_observed_fact",
        },
        {
            "locator": "observed.first_puback_after_next_initial_publish",
            "classification": "externally_observed_fact",
        },
    ]
