"""Strict, non-listening interpretation of the retained MQTT observation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final, Literal

MAX_PROFILE_BYTES: Final = 64 * 1024
_PROFILE_VERSION: Final = 1
_TRANSPORT: Final = "mqtt-over-tls"
_UPSTREAM_REVISION: Final = "cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4"
_REQUIRED_NOT_OBSERVED: Final = frozenset(
    {
        "request payload upper bound",
        "full request schema",
        "full report schema",
    }
)
_REQUIRED_SUBSCRIPTIONS: Final = frozenset(
    {
        ("device/{serial}/report", 0),
        ("device/{serial}/request", 0),
    }
)


@dataclass(frozen=True, slots=True)
class MqttObservationProfile:
    """The minimal immutable fact set retained from the black-box capture."""

    upstream_revision: str
    tls_version: str
    tls_cipher_suite: str
    tls_session_reused: bool
    mqtt_protocol_level: int
    clean_session: bool
    keepalive_seconds: int
    client_id_pattern: str
    client_id_printer_id_is_decimal: bool
    client_id_session_counter_is_decimal: bool
    username: str
    password_is_access_code: bool
    will_present: bool
    subscriptions: frozenset[tuple[str, int]]
    server_to_client_topics: frozenset[str]
    publish_qos: int
    publish_retain: bool
    publish_dup: bool
    next_packet_before_first_puback: bool
    initial_commands: frozenset[str]
    initial_publish_payload_bytes: tuple[int, ...]
    qos1_connected_hold_seconds: int
    qos1_retransmissions_during_hold: int
    pingreq_times_ms: tuple[int, ...]
    reconnect_duplicate_flag: bool
    reconnect_reuses_prior_packet_id: bool
    reconnect_reuses_prior_payload_hash: bool
    reconnect_fresh_initial_precedes_outstanding: bool
    second_reconnect_retransmits_all_unacked: bool


@dataclass(frozen=True, slots=True)
class MqttProfileAssessment:
    """A non-enumerating result for an untrusted profile document."""

    accepted: bool
    profile: MqttObservationProfile | None = None
    code: Literal["profile_invalid", "profile_too_large"] | None = None


@dataclass(frozen=True, slots=True)
class MqttListenerDisposition:
    """A fixed fail-closed result; this slice owns no socket or MQTT state."""

    enabled: Literal[False] = False
    code: Literal["mqtt_runtime_disabled"] = "mqtt_runtime_disabled"


def assess_mqtt_observation_profile(raw: bytes) -> MqttProfileAssessment:
    """Accept only the approved retained fact shape; reject all other input."""
    if len(raw) > MAX_PROFILE_BYTES:
        return MqttProfileAssessment(accepted=False, code="profile_too_large")
    try:
        document = json.loads(
            raw.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        return MqttProfileAssessment(accepted=False, code="profile_invalid")
    profile = _decode_profile(document)
    if profile is None:
        return MqttProfileAssessment(accepted=False, code="profile_invalid")
    return MqttProfileAssessment(accepted=True, profile=profile)


def mqtt_listener_disposition(_profile: MqttObservationProfile) -> MqttListenerDisposition:
    """Keep MQTT uncomposed until ADR 0009 is accepted and fully evidenced."""
    return MqttListenerDisposition()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON member")
        document[key] = value
    return document


def _decode_profile(value: object) -> MqttObservationProfile | None:
    if not isinstance(value, dict) or set(value) != {
        "profile_version",
        "transport",
        "upstream_revision",
        "generated_serial",
        "observed",
        "not_observed",
    }:
        return None
    if (
        type(value["profile_version"]) is not int
        or value["profile_version"] != _PROFILE_VERSION
        or value["transport"] != _TRANSPORT
        or value["upstream_revision"] != _UPSTREAM_REVISION
        or not _valid_generated_serial(value["generated_serial"])
        or not _valid_not_observed(value["not_observed"])
    ):
        return None
    return _decode_observed(value["observed"])


def _valid_generated_serial(value: object) -> bool:
    return isinstance(value, str) and value.isascii() and 1 <= len(value) <= 50


def _valid_not_observed(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == len(_REQUIRED_NOT_OBSERVED)
        and all(isinstance(item, str) for item in value)
        and frozenset(value) == _REQUIRED_NOT_OBSERVED
    )


def _decode_observed(value: object) -> MqttObservationProfile | None:
    if not isinstance(value, dict) or set(value) != {
        "tls_versions",
        "tls_cipher_suite",
        "tls_session_reused",
        "mqtt_protocol_name",
        "mqtt_protocol_level",
        "clean_session",
        "keepalive_seconds",
        "client_id_pattern",
        "client_id_printer_id_is_decimal",
        "client_id_session_counter_is_decimal",
        "username",
        "password_is_access_code",
        "will_present",
        "subscriptions",
        "server_to_client_topics",
        "publish_qos",
        "publish_retain",
        "publish_dup",
        "next_packet_before_first_puback",
        "initial_commands",
        "initial_publish_payload_bytes",
        "client_sessions_observed",
        "qos1_connected_hold_seconds",
        "qos1_retransmissions_during_hold",
        "pingreq_times_ms",
        "reconnect_duplicate_flag",
        "reconnect_reuses_prior_packet_id",
        "reconnect_reuses_prior_payload_hash",
        "reconnect_fresh_initial_precedes_outstanding",
        "second_reconnect_retransmits_all_unacked",
    }:
        return None
    tls_versions = value["tls_versions"]
    keepalive = value["keepalive_seconds"]
    protocol_level = value["mqtt_protocol_level"]
    publish_qos = value["publish_qos"]
    payload_bytes = value["initial_publish_payload_bytes"]
    sessions = value["client_sessions_observed"]
    hold_seconds = value["qos1_connected_hold_seconds"]
    retransmissions = value["qos1_retransmissions_during_hold"]
    pingreq_times = value["pingreq_times_ms"]
    if (
        tls_versions != ["TLSv1.3"]
        or value["tls_cipher_suite"] != "TLS_AES_256_GCM_SHA384"
        or value["tls_session_reused"] is not False
        or value["mqtt_protocol_name"] != "MQTT"
        or type(protocol_level) is not int
        or protocol_level != 4
        or value["clean_session"] is not True
        or type(keepalive) is not int
        or keepalive != 30
        or value["client_id_pattern"] != "bambuddy_{serial}_{printer-id}_{session-counter}"
        or value["client_id_printer_id_is_decimal"] is not True
        or value["client_id_session_counter_is_decimal"] is not True
        or value["username"] != "bblp"
        or value["password_is_access_code"] is not True
        or value["will_present"] is not False
        or type(publish_qos) is not int
        or publish_qos != 1
        or value["publish_retain"] is not False
        or value["publish_dup"] is not False
        or value["next_packet_before_first_puback"] is not True
        or not isinstance(payload_bytes, list)
        or any(type(item) is not int for item in payload_bytes)
        or payload_bytes != [35, 56, 109]
        or type(sessions) is not int
        or sessions != 5
        or type(hold_seconds) is not int
        or hold_seconds != 75
        or type(retransmissions) is not int
        or retransmissions != 0
        or not _exact_int_list(pingreq_times, (30086, 60119))
        or value["reconnect_duplicate_flag"] is not True
        or value["reconnect_reuses_prior_packet_id"] is not True
        or value["reconnect_reuses_prior_payload_hash"] is not True
        or value["reconnect_fresh_initial_precedes_outstanding"] is not True
        or value["second_reconnect_retransmits_all_unacked"] is not True
    ):
        return None
    subscriptions = _decode_subscriptions(value["subscriptions"])
    topics = _exact_string_set(value["server_to_client_topics"], {"device/{serial}/request"})
    commands = _exact_string_set(
        value["initial_commands"], {"pushall", "get_version", "extrusion_cali_get"}
    )
    if subscriptions is None or topics is None or commands is None:
        return None
    return MqttObservationProfile(
        upstream_revision=_UPSTREAM_REVISION,
        tls_version="TLSv1.3",
        tls_cipher_suite="TLS_AES_256_GCM_SHA384",
        tls_session_reused=False,
        mqtt_protocol_level=4,
        clean_session=True,
        keepalive_seconds=30,
        client_id_pattern="bambuddy_{serial}_{printer-id}_{session-counter}",
        client_id_printer_id_is_decimal=True,
        client_id_session_counter_is_decimal=True,
        username="bblp",
        password_is_access_code=True,
        will_present=False,
        subscriptions=subscriptions,
        server_to_client_topics=topics,
        publish_qos=1,
        publish_retain=False,
        publish_dup=False,
        next_packet_before_first_puback=True,
        initial_commands=commands,
        initial_publish_payload_bytes=(35, 56, 109),
        qos1_connected_hold_seconds=75,
        qos1_retransmissions_during_hold=0,
        pingreq_times_ms=(30086, 60119),
        reconnect_duplicate_flag=True,
        reconnect_reuses_prior_packet_id=True,
        reconnect_reuses_prior_payload_hash=True,
        reconnect_fresh_initial_precedes_outstanding=True,
        second_reconnect_retransmits_all_unacked=True,
    )


def _decode_subscriptions(value: object) -> frozenset[tuple[str, int]] | None:
    if not isinstance(value, list) or len(value) != len(_REQUIRED_SUBSCRIPTIONS):
        return None
    subscriptions: set[tuple[str, int]] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"topic", "qos"}:
            return None
        topic = item["topic"]
        qos = item["qos"]
        if not isinstance(topic, str) or isinstance(qos, bool) or not isinstance(qos, int):
            return None
        subscriptions.add((topic, qos))
    result = frozenset(subscriptions)
    return result if result == _REQUIRED_SUBSCRIPTIONS else None


def _exact_string_set(value: object, expected: set[str]) -> frozenset[str] | None:
    if not isinstance(value, list) or len(value) != len(expected):
        return None
    if not all(isinstance(item, str) for item in value):
        return None
    result = frozenset(value)
    return result if result == expected else None


def _exact_int_list(value: object, expected: tuple[int, ...]) -> bool:
    return (
        isinstance(value, list)
        and len(value) == len(expected)
        and all(type(item) is int for item in value)
        and tuple(value) == expected
    )
