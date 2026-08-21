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
        keepalive_seconds=30,
        subscriptions=frozenset(
            {
                ("device/{serial}/report", 0),
                ("device/{serial}/request", 0),
            }
        ),
        server_to_client_topics=frozenset({"device/{serial}/request"}),
        initial_commands=frozenset({"pushall", "get_version", "extrusion_cali_get"}),
    )
    assert mqtt_listener_disposition(assessment.profile).enabled is False
    assert mqtt_listener_disposition(assessment.profile).code == "mqtt_runtime_disabled"


@pytest.mark.parametrize(
    "change",
    [
        lambda document: document.__setitem__("extra", None),
        lambda document: document.__setitem__("transport", "mqtt"),
        lambda document: document.__setitem__("generated_serial", "not unicode-\N{SNOWMAN}"),
        lambda document: document.__setitem__("not_observed", []),
        lambda document: document.__setitem__("observed", []),
        lambda document: document["observed"].__setitem__("keepalive_seconds", True),
        lambda document: document["observed"].__setitem__("mqtt_protocol_level", 5),
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
