"""Strict, non-runtime interpretation of one sanitized MQTT message capture."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Final, Literal

MAX_MESSAGE_PROFILE_BYTES: Final = 64 * 1024
_EXPECTED_SHA256: Final = "5db8e86f7d163b29cb67e6f97cae852cbab9127560515b3dfb1b07816ffee375"
_EXPECTED_KEYS: Final = frozenset(
    {
        "profile_version",
        "transport",
        "upstream_revision",
        "generated_serial",
        "observed",
        "not_observed",
    }
)


@dataclass(frozen=True, slots=True)
class MqttMessageSchemaProfile:
    """One exact bounded capture; it confers no listener or control authority."""

    report_topic: str
    observed_reply_commands: frozenset[str]
    not_observed: frozenset[str]
    field_evidence: Literal["observed_fields_only"]


@dataclass(frozen=True, slots=True)
class MqttMessageSchemaAssessment:
    """A non-enumerating result for an untrusted retained-capture document."""

    accepted: bool
    profile: MqttMessageSchemaProfile | None = None
    code: Literal["message_profile_invalid", "message_profile_too_large"] | None = None


def assess_mqtt_message_schema(raw: bytes) -> MqttMessageSchemaAssessment:
    """Accept only the byte-exact sanitized observation profile."""
    if len(raw) > MAX_MESSAGE_PROFILE_BYTES:
        return MqttMessageSchemaAssessment(False, code="message_profile_too_large")
    try:
        document = json.loads(
            raw.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        return MqttMessageSchemaAssessment(False, code="message_profile_invalid")
    profile = _decode(document, raw)
    if profile is None:
        return MqttMessageSchemaAssessment(False, code="message_profile_invalid")
    return MqttMessageSchemaAssessment(True, profile=profile)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _decode(value: object, raw: bytes) -> MqttMessageSchemaProfile | None:
    if not isinstance(value, dict) or set(value) != _EXPECTED_KEYS:
        return None
    if hashlib.sha256(raw).hexdigest() != _EXPECTED_SHA256:
        return None
    if b'"required_fields"' in raw or b'"item_fields"' in raw:
        return None
    observed = value["observed"]
    not_observed = value["not_observed"]
    if (
        value["profile_version"] != 1
        or value["transport"] != "mqtt-over-tls"
        or value["generated_serial"] != "{serial}"
        or not isinstance(observed, dict)
        or not isinstance(not_observed, list)
        or not all(isinstance(item, str) for item in not_observed)
    ):
        return None
    replies = observed.get("request_replies")
    topic = observed.get("report_topic")
    if not isinstance(replies, dict) or not isinstance(topic, str):
        return None
    return MqttMessageSchemaProfile(
        report_topic=topic,
        observed_reply_commands=frozenset(replies),
        not_observed=frozenset(not_observed),
        field_evidence="observed_fields_only",
    )
