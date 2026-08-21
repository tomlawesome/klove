from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from klove.ftps import (
    FtpsCommand,
    FtpsDataConnection,
    FtpsObservationProfile,
    FtpsSession,
    assess_ftps_observation_profile,
    ftps_listener_disposition,
)
from klove.ftps.profile import MAX_PROFILE_BYTES

PROFILE = Path(__file__).parents[1] / "fixtures" / "grove-observations" / "ftps-client-profile"


def profile_document() -> dict[str, object]:
    loaded = json.loads(PROFILE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_exact_retained_profile_is_accepted_but_cannot_enable_a_listener() -> None:
    assessment = assess_ftps_observation_profile(PROFILE.read_bytes())

    assert assessment.accepted is True
    assert assessment.code is None
    assert assessment.profile == FtpsObservationProfile(
        upstream_revision="cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4",
        control_tls_version="TLSv1.3",
        username="bblp",
        password_is_access_code=True,
        sessions=(
            FtpsSession(
                commands=(
                    FtpsCommand("USER", "bblp"),
                    FtpsCommand("PASS", "generated_access_code"),
                    FtpsCommand("PBSZ", "0"),
                    FtpsCommand("PROT", "P"),
                    FtpsCommand("DELE", "/observation.3mf"),
                    FtpsCommand("QUIT", None),
                )
            ),
            FtpsSession(
                commands=(
                    FtpsCommand("USER", "bblp"),
                    FtpsCommand("PASS", "generated_access_code"),
                    FtpsCommand("PBSZ", "0"),
                    FtpsCommand("PROT", "P"),
                    FtpsCommand("PASV", None),
                    FtpsCommand("STOR", "/observation.3mf"),
                    FtpsCommand("QUIT", None),
                ),
                data_connection=FtpsDataConnection(
                    tls_version="TLSv1.3",
                    same_control_peer=True,
                    byte_count=1684,
                    sha256="sha256:bc1d1e01a40fcee60437d5302ae87502e693576bdde958db1091b62e279553f1",
                ),
            ),
        ),
    )
    assert ftps_listener_disposition(assessment.profile).enabled is False
    assert ftps_listener_disposition(assessment.profile).code == "ftps_runtime_disabled"


@pytest.mark.parametrize(
    "change",
    [
        lambda document: document.__setitem__("extra", None),
        lambda document: document.__setitem__("profile_version", True),
        lambda document: document.__setitem__("transport", "explicit-ftps"),
        lambda document: document.__setitem__("not_observed", []),
        lambda document: document["observed"].__setitem__("extra", None),
        lambda document: document["observed"].__setitem__("control_tls_versions", ["TLSv1.2"]),
        lambda document: document["observed"]["sessions"][0].__setitem__("username", "other"),
        lambda document: document["observed"]["sessions"][0].__setitem__(
            "password_is_access_code", 1
        ),
        lambda document: document["observed"]["sessions"][0]["commands"][0].__setitem__(
            "argument", 0
        ),
        lambda document: document["observed"]["sessions"][1]["commands"].pop(),
        lambda document: document["observed"]["sessions"][1]["data_connection"].__setitem__(
            "same_control_peer", 1
        ),
        lambda document: document["observed"]["sessions"][1]["data_connection"].__setitem__(
            "byte_count", 1684.0
        ),
        lambda document: document["observed"]["sessions"][1]["data_connection"].__setitem__(
            "sha256", "sha256:" + "0" * 64
        ),
    ],
)
def test_changed_or_unretained_profile_facts_are_rejected(change: object) -> None:
    document = profile_document()
    assert callable(change)
    change(document)

    assessment = assess_ftps_observation_profile(json.dumps(document).encode())

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
    assessment = assess_ftps_observation_profile(raw)

    assert assessment.accepted is False
    assert assessment.profile is None
    assert assessment.code in {"profile_invalid", "profile_too_large"}


@given(st.binary(max_size=MAX_PROFILE_BYTES))
def test_arbitrary_bounded_bytes_never_raise_or_open_ftps(raw: bytes) -> None:
    assessment = assess_ftps_observation_profile(raw)

    assert assessment.accepted is (assessment.profile is not None)
    if assessment.profile is not None:
        assert ftps_listener_disposition(assessment.profile).enabled is False
    else:
        assert assessment.code == "profile_invalid"


def test_deeply_nested_bounded_json_fails_closed() -> None:
    raw = ("[" * 10_000 + "]" * 10_000).encode()

    assessment = assess_ftps_observation_profile(raw)

    assert assessment.accepted is False
    assert assessment.profile is None
    assert assessment.code == "profile_invalid"
