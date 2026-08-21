from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from klove.northbound.mqtt import assess_mqtt_observation_profile, mqtt_listener_disposition
from klove.northbound.mqtt.profile import MAX_PROFILE_BYTES, MqttObservationProfile

PROFILE = Path(__file__).parents[1] / "fixtures" / "grove-observations" / "mqtt-client-profile"


def profile_document() -> dict[str, object]:
    loaded = json.loads(PROFILE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_exact_retained_profile_is_accepted_but_cannot_enable_a_listener() -> None:
    assessment = assess_mqtt_observation_profile(PROFILE.read_bytes())

    assert assessment.accepted is True
    assert assessment.code is None
    assert assessment.profile == MqttObservationProfile(
        upstream_revision="cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4",
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
        subscriptions=frozenset(
            {
                ("device/{serial}/report", 0),
                ("device/{serial}/request", 0),
            }
        ),
        server_to_client_topics=frozenset({"device/{serial}/request"}),
        publish_qos=1,
        publish_retain=False,
        publish_dup=False,
        next_packet_before_first_puback=True,
        initial_commands=frozenset({"pushall", "get_version", "extrusion_cali_get"}),
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
    assert mqtt_listener_disposition(assessment.profile).enabled is False
    assert mqtt_listener_disposition(assessment.profile).code == "mqtt_runtime_disabled"


@pytest.mark.parametrize(
    "change",
    [
        lambda document: document.__setitem__("extra", None),
        lambda document: document.__setitem__("profile_version", 1.0),
        lambda document: document.__setitem__("transport", "mqtt"),
        lambda document: document.__setitem__("generated_serial", "not unicode-\N{SNOWMAN}"),
        lambda document: document.__setitem__("not_observed", []),
        lambda document: document.__setitem__("observed", []),
        lambda document: document["observed"].__setitem__("keepalive_seconds", True),
        lambda document: document["observed"].__setitem__(
            "tls_cipher_suite", "TLS_AES_128_GCM_SHA256"
        ),
        lambda document: document["observed"].__setitem__("tls_session_reused", True),
        lambda document: document["observed"].__setitem__("mqtt_protocol_name", "MQIsdp"),
        lambda document: document["observed"].__setitem__("mqtt_protocol_level", 4.0),
        lambda document: document["observed"].__setitem__("clean_session", 1),
        lambda document: document["observed"].__setitem__("client_id_pattern", "bambuddy_{serial}"),
        lambda document: document["observed"].__setitem__("client_id_printer_id_is_decimal", False),
        lambda document: document["observed"].__setitem__(
            "client_id_session_counter_is_decimal", False
        ),
        lambda document: document["observed"].__setitem__("username", "guest"),
        lambda document: document["observed"].__setitem__("password_is_access_code", False),
        lambda document: document["observed"].__setitem__("will_present", True),
        lambda document: document["observed"].__setitem__("publish_qos", 1.0),
        lambda document: document["observed"].__setitem__("publish_retain", True),
        lambda document: document["observed"].__setitem__("publish_dup", True),
        lambda document: document["observed"].__setitem__("next_packet_before_first_puback", False),
        lambda document: document["observed"].__setitem__(
            "initial_publish_payload_bytes", [35, 56]
        ),
        lambda document: document["observed"].__setitem__(
            "initial_publish_payload_bytes", [35.0, 56, 109]
        ),
        lambda document: document["observed"].__setitem__("client_sessions_observed", 2.0),
        lambda document: document["observed"].__setitem__("qos1_connected_hold_seconds", 75.0),
        lambda document: document["observed"].__setitem__("qos1_retransmissions_during_hold", 1),
        lambda document: document["observed"].__setitem__("pingreq_times_ms", [30086.0, 60119]),
        lambda document: document["observed"].__setitem__("reconnect_duplicate_flag", False),
        lambda document: document["observed"].__setitem__(
            "reconnect_reuses_prior_packet_id", False
        ),
        lambda document: document["observed"].__setitem__(
            "reconnect_reuses_prior_payload_hash", False
        ),
        lambda document: document["observed"].__setitem__(
            "reconnect_fresh_initial_precedes_outstanding", False
        ),
        lambda document: document["observed"].__setitem__(
            "second_reconnect_retransmits_all_unacked", False
        ),
        lambda document: document["observed"].__setitem__("subscriptions", []),
        lambda document: document["observed"].__setitem__(
            "subscriptions", ["not a subscription", "also not a subscription"]
        ),
        lambda document: document["observed"].__setitem__(
            "subscriptions",
            [
                {"topic": "device/{serial}/report", "qos": True},
                {"topic": "device/{serial}/request", "qos": 0},
            ],
        ),
        lambda document: document["observed"].__setitem__("server_to_client_topics", ["#"]),
        lambda document: document["observed"].__setitem__("server_to_client_topics", [0]),
        lambda document: document["observed"].__setitem__(
            "initial_commands", ["print.project_file"]
        ),
    ],
)
def test_unretained_or_changed_profile_facts_are_rejected(change: object) -> None:
    document = profile_document()
    assert callable(change)
    change(document)

    assessment = assess_mqtt_observation_profile(json.dumps(document).encode())

    assert assessment.accepted is False
    assert assessment.profile is None
    assert assessment.code == "profile_invalid"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b"{",
        b'{"profile_version":1,"profile_version":1}',
        b"\xff",
        b"x" * (MAX_PROFILE_BYTES + 1),
    ],
)
def test_malformed_or_oversized_profiles_fail_closed(raw: bytes) -> None:
    assessment = assess_mqtt_observation_profile(raw)

    assert assessment.accepted is False
    assert assessment.profile is None
    assert assessment.code in {"profile_invalid", "profile_too_large"}


@given(st.binary(max_size=MAX_PROFILE_BYTES))
def test_arbitrary_bounded_bytes_never_raise_or_open_mqtt(raw: bytes) -> None:
    assessment = assess_mqtt_observation_profile(raw)

    assert assessment.accepted is (assessment.profile is not None)
    if assessment.profile is not None:
        assert mqtt_listener_disposition(assessment.profile).enabled is False
    else:
        assert assessment.code == "profile_invalid"


def test_deeply_nested_bounded_json_fails_closed() -> None:
    raw = ("[" * 10_000 + "]" * 10_000).encode()

    assessment = assess_mqtt_observation_profile(raw)

    assert assessment.accepted is False
    assert assessment.profile is None
    assert assessment.code == "profile_invalid"
