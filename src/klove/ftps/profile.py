"""Strict, non-listening interpretation of retained FTPS client observations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final, Literal

MAX_PROFILE_BYTES: Final = 64 * 1024
_UPSTREAM_REVISION: Final = "cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4"
_UPLOAD_SHA256: Final = "sha256:bc1d1e01a40fcee60437d5302ae87502e693576bdde958db1091b62e279553f1"
_NOT_OBSERVED: Final = [
    "FTP reply codes and error handling",
    "PASV address, port, and NAT behavior",
    "canonical production upload filename grammar",
    "multiple or concurrent transfer behavior",
    "wire retry and disconnect behavior",
]
_OBSERVED: Final = {
    "control_tls_versions": ["TLSv1.3"],
    "sessions": [
        {
            "username": "bblp",
            "password_is_access_code": True,
            "commands": [
                {"command": "USER", "argument": "bblp"},
                {"command": "PASS", "argument": "generated_access_code"},
                {"command": "PBSZ", "argument": "0"},
                {"command": "PROT", "argument": "P"},
                {"command": "DELE", "argument": "/observation.3mf"},
                {"command": "QUIT", "argument": None},
            ],
        },
        {
            "username": "bblp",
            "password_is_access_code": True,
            "commands": [
                {"command": "USER", "argument": "bblp"},
                {"command": "PASS", "argument": "generated_access_code"},
                {"command": "PBSZ", "argument": "0"},
                {"command": "PROT", "argument": "P"},
                {"command": "PASV", "argument": None},
                {"command": "STOR", "argument": "/observation.3mf"},
                {"command": "QUIT", "argument": None},
            ],
            "data_connection": {
                "tls_version": "TLSv1.3",
                "same_control_peer": True,
                "byte_count": 1684,
                "sha256": _UPLOAD_SHA256,
            },
        },
    ],
}
_EXPECTED_DOCUMENT: Final = {
    "profile_version": 1,
    "transport": "implicit-ftps",
    "upstream_revision": _UPSTREAM_REVISION,
    "generated_serial": "00M00A391800001",
    "observed": _OBSERVED,
    "not_observed": _NOT_OBSERVED,
}


@dataclass(frozen=True, slots=True)
class FtpsCommand:
    """One exact retained FTP command and argument from a client session."""

    command: str
    argument: str | None


@dataclass(frozen=True, slots=True)
class FtpsDataConnection:
    """The one retained protected passive data-transfer fact set."""

    tls_version: Literal["TLSv1.3"]
    same_control_peer: Literal[True]
    byte_count: Literal[1684]
    sha256: Literal["sha256:bc1d1e01a40fcee60437d5302ae87502e693576bdde958db1091b62e279553f1"]


@dataclass(frozen=True, slots=True)
class FtpsSession:
    """One ordered FTPS control session; no response or server semantics are implied."""

    commands: tuple[FtpsCommand, ...]
    data_connection: FtpsDataConnection | None = None


@dataclass(frozen=True, slots=True)
class FtpsObservationProfile:
    """The complete immutable fact set retained from the sanitized capture."""

    upstream_revision: str
    control_tls_version: Literal["TLSv1.3"]
    username: Literal["bblp"]
    password_is_access_code: Literal[True]
    sessions: tuple[FtpsSession, FtpsSession]


@dataclass(frozen=True, slots=True)
class FtpsProfileAssessment:
    """A non-enumerating assessment of untrusted profile bytes."""

    accepted: bool
    profile: FtpsObservationProfile | None = None
    code: Literal["profile_invalid", "profile_too_large"] | None = None


@dataclass(frozen=True, slots=True)
class FtpsListenerDisposition:
    """A fixed denial because this package deliberately composes no FTPS runtime."""

    enabled: Literal[False] = False
    code: Literal["ftps_runtime_disabled"] = "ftps_runtime_disabled"


def assess_ftps_observation_profile(raw: bytes) -> FtpsProfileAssessment:
    """Accept only the precise retained public-runtime capture; reject all other input."""
    if len(raw) > MAX_PROFILE_BYTES:
        return FtpsProfileAssessment(accepted=False, code="profile_too_large")
    try:
        document = json.loads(
            raw.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        return FtpsProfileAssessment(accepted=False, code="profile_invalid")
    if document != _EXPECTED_DOCUMENT:
        return FtpsProfileAssessment(accepted=False, code="profile_invalid")
    sessions = document["observed"]["sessions"]
    if (
        type(document["profile_version"]) is not int
        or type(sessions[0]["password_is_access_code"]) is not bool
        or type(sessions[1]["password_is_access_code"]) is not bool
        or type(sessions[1]["data_connection"]["same_control_peer"]) is not bool
        or type(sessions[1]["data_connection"]["byte_count"]) is not int
    ):
        return FtpsProfileAssessment(accepted=False, code="profile_invalid")
    return FtpsProfileAssessment(accepted=True, profile=_profile())


def ftps_listener_disposition(_profile: FtpsObservationProfile) -> FtpsListenerDisposition:
    """Keep FTPS uncomposed until ADR 0010 is accepted and all gates close."""
    return FtpsListenerDisposition()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON member")
        document[key] = value
    return document


def _profile() -> FtpsObservationProfile:
    deletion = FtpsSession(
        commands=(
            FtpsCommand("USER", "bblp"),
            FtpsCommand("PASS", "generated_access_code"),
            FtpsCommand("PBSZ", "0"),
            FtpsCommand("PROT", "P"),
            FtpsCommand("DELE", "/observation.3mf"),
            FtpsCommand("QUIT", None),
        )
    )
    upload = FtpsSession(
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
            sha256=_UPLOAD_SHA256,
        ),
    )
    return FtpsObservationProfile(
        upstream_revision=_UPSTREAM_REVISION,
        control_tls_version="TLSv1.3",
        username="bblp",
        password_is_access_code=True,
        sessions=(deletion, upload),
    )
