from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
SERIAL = b"01S00A391800001"


def _recorder() -> Any:
    spec = importlib.util.spec_from_file_location(
        "grove_mqtt_report_recorder", SCRIPTS / "grove-mqtt-report-recorder.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _string(value: bytes) -> bytes:
    return len(value).to_bytes(2, "big") + value


def _packet(header: int, body: bytes) -> bytes:
    remaining = len(body)
    encoded = bytearray()
    while True:
        digit = remaining % 128
        remaining //= 128
        if remaining:
            digit |= 128
        encoded.append(digit)
        if not remaining:
            return bytes((header,)) + bytes(encoded) + body


def _report(payload: object, *, header: int = 0x30, serial: bytes = SERIAL) -> bytes:
    body = _string(b"device/" + serial + b"/report") + json.dumps(payload).encode("utf-8")
    return _packet(header, body)


def _report_body(payload: object, *, serial: bytes = SERIAL) -> bytes:
    packet = _report(payload, serial=serial)
    cursor = 1
    while packet[cursor] & 128:
        cursor += 1
    return packet[cursor + 1 :]


class _Connection:
    def __init__(self, incoming: bytes = b"") -> None:
        self.incoming = bytearray(incoming)
        self.writes: list[bytes] = []
        self.closed = False

    def read(self, size: int) -> bytes:
        if not self.incoming:
            raise TimeoutError
        result = bytes(self.incoming[:size])
        del self.incoming[:size]
        return result

    def write(self, payload: bytes) -> int:
        self.writes.append(payload)
        return len(payload)

    def settimeout(self, _value: float) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def _acknowledgement() -> dict[str, object]:
    return {
        "print": {
            "command": "project_file",
            "sequence_id": "70001",
            "gcode_state": "PREPARE",
            "result": "SUCCESS",
            "gcode_file": "private-model.3mf",
        }
    }


def _status(state: str, sequence_id: str = "sensitive-sequence") -> dict[str, object]:
    return {
        "print": {
            "command": "push_status",
            "sequence_id": sequence_id,
            "gcode_state": state,
            "gcode_file": "private-model.3mf",
            "temperature": 200.0,
        }
    }


def test_report_sanitizer_retains_only_schema_and_canonical_tokens() -> None:
    recorder = _recorder()
    payload = _acknowledgement()

    profile, print_block = recorder._parse_report_publish(0x30, _report_body(payload), SERIAL)

    assert profile["topic"] == "device/{serial}/report"
    assert profile["qos"] == 0
    assert profile["dup"] is False
    assert profile["retain"] is False
    assert print_block["command"] == "project_file"
    serialized = json.dumps(profile, sort_keys=True)
    assert "private-model.3mf" not in serialized
    assert "70001" not in serialized


@pytest.mark.parametrize("header", [0x31, 0x32, 0x38])
def test_report_sanitizer_rejects_retain_qos_or_duplicate_publish(header: int) -> None:
    recorder = _recorder()
    with pytest.raises(recorder.ObservationFailure):
        recorder._parse_report_publish(header, _report_body(_acknowledgement()), SERIAL)


def test_report_sanitizer_rejects_noncanonical_topic_and_duplicate_json_member() -> None:
    recorder = _recorder()
    with pytest.raises(recorder.ObservationFailure):
        recorder._parse_report_publish(
            0x30, _report_body(_acknowledgement(), serial=b"BAD"), SERIAL
        )
    duplicate = b'{"print":{"command":"push_status","command":"project_file"}}'
    body = _string(b"device/" + SERIAL + b"/report") + duplicate
    with pytest.raises(recorder.ObservationFailure):
        recorder._parse_report_publish(0x30, body, SERIAL)


def test_two_session_observation_binds_project_result_and_non_idle_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _recorder()
    probe = _Connection()
    persistent = _Connection(
        b"".join(
            [
                _report(_acknowledgement()),
                _report(_status("PREPARE")),
                _report(_status("FINISH")),
            ]
        )
    )
    sessions = iter((probe, persistent))
    monkeypatch.setattr(recorder, "_open_session", lambda *_arguments: (next(sessions), set(), []))

    candidate = recorder.observe_two_sessions("grove", SERIAL)

    observed = candidate["observed"]
    assert observed["project_file_result"]["matches_generated_request_sequence"] is True
    assert [entry["state"] for entry in observed["non_idle_push_status"]] == ["PREPARE", "FINISH"]
    assert observed["persistent_session_qos0_no_replay_or_retain"] is True
    assert probe.closed and persistent.closed
    serialized = json.dumps(candidate, sort_keys=True)
    assert "private-model.3mf" not in serialized
    assert "sensitive-sequence" not in serialized


def test_two_session_observation_rejects_qos0_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _recorder()
    acknowledgement = _report(_acknowledgement())
    sessions = iter(
        (
            _Connection(),
            _Connection(acknowledgement + acknowledgement + _report(_status("PREPARE"))),
        )
    )
    monkeypatch.setattr(recorder, "_open_session", lambda *_arguments: (next(sessions), set(), []))

    with pytest.raises(recorder.ObservationFailure, match="report_replay"):
        recorder.observe_two_sessions("grove", SERIAL)


@pytest.mark.parametrize(
    ("reports", "code"),
    [
        ([_report(_status("PREPARE")), _report(_acknowledgement())], "stale_pre_ack_report"),
        ([_report(_acknowledgement()), _report(_status("FINISH"))], "post_ack_prepare_invalid"),
        (
            [
                _report(_acknowledgement()),
                _report(_status("PREPARE")),
                _report(_status("PREPARE", "different-sequence")),
            ],
            "post_prepare_finish_invalid",
        ),
        (
            [
                _report(_acknowledgement()),
                _report(_status("IDLE")),
                _report(_status("PREPARE")),
            ],
            "post_ack_prepare_invalid",
        ),
        (
            [
                _report(_acknowledgement()),
                _report(_status("PREPARE")),
                _report(_acknowledgement()),
            ],
            "report_replay",
        ),
    ],
)
def test_two_session_observation_rejects_noncausal_report_order(
    monkeypatch: pytest.MonkeyPatch, reports: list[bytes], code: str
) -> None:
    recorder = _recorder()
    sessions = iter((_Connection(), _Connection(b"".join(reports))))
    monkeypatch.setattr(recorder, "_open_session", lambda *_arguments: (next(sessions), set(), []))

    with pytest.raises(recorder.ObservationFailure, match=code):
        recorder.observe_two_sessions("grove", SERIAL)


def test_two_session_observation_rejects_stale_initial_non_idle_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _recorder()
    sessions = iter((_Connection(), _Connection()))
    stale = ({"topic": "device/{serial}/report"}, _status("PREPARE")["print"])
    monkeypatch.setattr(
        recorder,
        "_open_session",
        lambda *_arguments: (next(sessions), set(), [] if _arguments[2] == 1 else [stale]),
    )

    with pytest.raises(recorder.ObservationFailure, match="stale_pre_ack_report"):
        recorder.observe_two_sessions("grove", SERIAL)


@pytest.mark.parametrize(
    ("after_finish", "code"),
    [
        (_report(_status("IDLE")), "post_finish_report"),
        (_report(_status("FINISH")), "report_replay"),
    ],
)
def test_two_session_observation_requires_quiet_replay_free_window(
    monkeypatch: pytest.MonkeyPatch, after_finish: bytes, code: str
) -> None:
    recorder = _recorder()
    packets = b"".join(
        [
            _report(_acknowledgement()),
            _report(_status("PREPARE")),
            _report(_status("FINISH")),
            after_finish,
        ]
    )
    sessions = iter((_Connection(), _Connection(packets)))
    monkeypatch.setattr(recorder, "_open_session", lambda *_arguments: (next(sessions), set(), []))

    with pytest.raises(recorder.ObservationFailure, match=code):
        recorder.observe_two_sessions("grove", SERIAL)


def test_subscribe_has_small_combined_pre_suback_report_cap() -> None:
    recorder = _recorder()
    connection = _Connection(_report(_status("IDLE")) + _report(_status("IDLE")))
    with pytest.raises(recorder.ObservationFailure, match="pre_suback_report_limit"):
        recorder._subscribe(connection, SERIAL, b"report", 1, time.monotonic() + 10, set(), 1, [])


def test_candidate_validation_rejects_unproven_result_or_replay_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _recorder()
    sessions = iter(
        (
            _Connection(),
            _Connection(
                b"".join(
                    [
                        _report(_acknowledgement()),
                        _report(_status("PREPARE")),
                        _report(_status("FINISH")),
                    ]
                )
            ),
        )
    )
    monkeypatch.setattr(recorder, "_open_session", lambda *_arguments: (next(sessions), set(), []))
    candidate = recorder.observe_two_sessions("grove", SERIAL)
    candidate["observed"]["project_file_result"]["result"] = "FAILURE"
    with pytest.raises(recorder.ObservationFailure):
        recorder.validate_candidate(candidate)
    candidate["observed"]["project_file_result"]["result"] = "SUCCESS"
    candidate["observed"]["persistent_session_qos0_no_replay_or_retain"] = False
    with pytest.raises(recorder.ObservationFailure):
        recorder.validate_candidate(candidate)
    candidate["observed"]["persistent_session_qos0_no_replay_or_retain"] = True
    candidate["observed"]["non_idle_push_status"].reverse()
    with pytest.raises(recorder.ObservationFailure):
        recorder.validate_candidate(candidate)
