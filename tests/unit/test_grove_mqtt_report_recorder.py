from __future__ import annotations

import ast
import importlib.util
import json
import os
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


def _diagnostics() -> Any:
    spec = importlib.util.spec_from_file_location(
        "grove_mqtt_report_diagnostics", SCRIPTS / "grove-mqtt-report-diagnostics.py"
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
    assert observed[recorder.BOUNDED_CHAIN_EVIDENCE] is True
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


@pytest.mark.parametrize("after_finish", [_report(_status("IDLE")), _report(_status("FINISH"))])
def test_two_session_observation_closes_after_first_strict_finish(
    monkeypatch: pytest.MonkeyPatch, after_finish: bytes
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
    persistent = _Connection(packets)
    sessions = iter((_Connection(), persistent))
    monkeypatch.setattr(recorder, "_open_session", lambda *_arguments: (next(sessions), set(), []))

    candidate = recorder.observe_two_sessions("grove", SERIAL)

    assert candidate["observed"][recorder.BOUNDED_CHAIN_EVIDENCE] is True
    assert persistent.incoming == bytearray(after_finish)
    assert persistent.closed


def test_subscribe_has_small_combined_pre_suback_report_cap() -> None:
    recorder = _recorder()
    connection = _Connection(_report(_status("IDLE")) + _report(_status("IDLE")))
    with pytest.raises(recorder.ObservationFailure, match="pre_suback_report_limit"):
        recorder._subscribe(connection, SERIAL, b"report", 1, time.monotonic() + 10, set(), 1, [])


def test_subscribe_consumes_required_post_suback_idle_status() -> None:
    recorder = _recorder()
    suback = _packet(0x90, b"\0\x01\0")
    connection = _Connection(suback + _report(_status("IDLE")))
    observed: list[tuple[dict[str, object], dict[str, object]]] = []

    reports = recorder._subscribe(
        connection, SERIAL, b"report", 1, time.monotonic() + 10, set(), 2, observed
    )

    assert reports == 1
    assert observed[0][1]["command"] == "push_status"
    assert observed[0][1]["gcode_state"] == "IDLE"
    assert not connection.incoming


def test_subscribe_accounts_for_pre_and_post_suback_reports_together() -> None:
    recorder = _recorder()
    suback = _packet(0x90, b"\0\x01\0")
    connection = _Connection(
        _report(_status("IDLE", "pre-suback")) + suback + _report(_status("IDLE", "post-suback"))
    )
    observed: list[tuple[dict[str, object], dict[str, object]]] = []

    reports = recorder._subscribe(
        connection, SERIAL, b"report", 1, time.monotonic() + 10, set(), 2, observed
    )

    assert reports == 2
    assert [report[1]["gcode_state"] for report in observed] == ["IDLE", "IDLE"]
    assert not connection.incoming


@pytest.mark.parametrize(
    "post_suback",
    [
        _report(_status("PREPARE")),
        _report(_acknowledgement()),
        _packet(0xC0, b""),
    ],
)
def test_subscribe_rejects_missing_or_nonidle_post_suback_status(post_suback: bytes) -> None:
    recorder = _recorder()
    connection = _Connection(_packet(0x90, b"\0\x01\0") + post_suback)
    with pytest.raises(recorder.ObservationFailure, match="post_suback_report_invalid"):
        recorder._subscribe(connection, SERIAL, b"report", 1, time.monotonic() + 10, set(), 2, [])


def test_subscribe_reserves_report_budget_for_required_post_suback_status() -> None:
    recorder = _recorder()
    connection = _Connection(_packet(0x90, b"\0\x01\0"))
    with pytest.raises(recorder.ObservationFailure, match="post_suback_report_limit"):
        recorder._subscribe(connection, SERIAL, b"report", 1, time.monotonic() + 10, set(), 0, [])


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
    candidate["observed"][recorder.BOUNDED_CHAIN_EVIDENCE] = False
    with pytest.raises(recorder.ObservationFailure):
        recorder.validate_candidate(candidate)
    candidate["observed"][recorder.BOUNDED_CHAIN_EVIDENCE] = True
    candidate["observed"]["non_idle_push_status"].reverse()
    with pytest.raises(recorder.ObservationFailure):
        recorder.validate_candidate(candidate)


def test_observation_failures_are_closed_static_codes_without_value_reflection() -> None:
    recorder = _recorder()
    tree = ast.parse((SCRIPTS / "grove-mqtt-report-recorder.py").read_text(encoding="utf-8"))
    raise_codes = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ObservationFailure"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    assert raise_codes == recorder.PROTOCOL_CODES
    for code in recorder.PROTOCOL_CODES:
        failure = recorder.ObservationFailure(code)
        assert failure.code == code
        assert recorder._failure_status(failure) == {"status": "failure", "code": code}
    with pytest.raises(ValueError) as captured:
        recorder.ObservationFailure("peer-controlled-value")
    assert "peer-controlled-value" not in str(captured.value)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("internal_failure", "MQTT_REPORT_CAPTURE_RECORDER_INTERNAL_FAILURE"),
        ("timeout", "MQTT_REPORT_CAPTURE_RECORDER_TIMEOUT"),
        ("tls_failure", "MQTT_REPORT_CAPTURE_RECORDER_TLS_FAILURE"),
        ("transport_failure", "MQTT_REPORT_CAPTURE_RECORDER_TRANSPORT_FAILURE"),
    ],
)
def test_report_diagnostics_allow_only_fixed_owner_private_statuses(
    tmp_path: Path, code: str, expected: str
) -> None:
    diagnostics = _diagnostics()
    tmp_path.chmod(0o700)
    path = tmp_path / "status"
    path.write_text(json.dumps({"status": "failure", "code": code}), encoding="ascii")
    path.chmod(0o600)

    assert diagnostics.validate_recorder_status(path, 1) == expected


def test_report_diagnostics_maps_only_closed_protocol_codes(tmp_path: Path) -> None:
    diagnostics = _diagnostics()
    recorder = _recorder()
    assert diagnostics._PROTOCOL_CODES == recorder.PROTOCOL_CODES
    tmp_path.chmod(0o700)
    path = tmp_path / "status"
    for code in recorder.PROTOCOL_CODES:
        path.write_text(json.dumps({"status": "failure", "code": code}), encoding="ascii")
        path.chmod(0o600)
        assert (
            diagnostics.validate_recorder_status(path, 1)
            == f"MQTT_REPORT_CAPTURE_PROTOCOL_{code.upper()}"
        )
    path.write_text('{"status":"failure","code":"peer-controlled-value"}', encoding="ascii")
    path.chmod(0o600)
    assert diagnostics.validate_recorder_status(path, 1) == diagnostics.STATUS_INVALID


@pytest.mark.parametrize(
    "contents",
    [
        '{"status":"failure","code":"report_replay","code":"timeout"}',
        '{"status":"failure","code":NaN}',
        '{"status":"success","code":"timeout"}',
        '{"status":"failure","code":"peer controlled"}',
        "x" * 257,
    ],
)
def test_report_diagnostics_reject_missing_hostile_or_nonprivate_statuses(
    tmp_path: Path, contents: str
) -> None:
    diagnostics = _diagnostics()
    tmp_path.chmod(0o700)
    path = tmp_path / "status"
    path.write_text(contents, encoding="ascii")
    path.chmod(0o600)
    assert diagnostics.validate_recorder_status(path, 1) == diagnostics.STATUS_INVALID

    path.unlink()
    assert diagnostics.validate_recorder_status(path, 1) == diagnostics.STATUS_INVALID
    path.write_text('{"status":"failure","code":"timeout"}', encoding="ascii")
    path.chmod(0o644)
    assert diagnostics.validate_recorder_status(path, 1) == diagnostics.STATUS_INVALID


def test_report_diagnostics_rejects_symlink_and_nonfailure_exit(tmp_path: Path) -> None:
    diagnostics = _diagnostics()
    tmp_path.chmod(0o700)
    target = tmp_path / "target"
    target.write_text('{"status":"failure","code":"timeout"}', encoding="ascii")
    target.chmod(0o600)
    link = tmp_path / "status"
    link.symlink_to(target)

    assert diagnostics.validate_recorder_status(link, 1) == diagnostics.STATUS_INVALID
    assert diagnostics.validate_recorder_status(target, 0) == diagnostics.STATUS_INVALID


def test_report_diagnostics_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    diagnostics = _diagnostics()
    tmp_path.chmod(0o700)
    fifo = tmp_path / "status"
    os.mkfifo(fifo, 0o600)

    started = time.monotonic()
    assert diagnostics.validate_recorder_status(fifo, 1) == diagnostics.STATUS_INVALID
    assert time.monotonic() - started < 1
