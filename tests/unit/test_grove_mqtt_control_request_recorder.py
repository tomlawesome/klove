from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
SERIAL = b"MQTTOBS0000001"


def _module(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recorder() -> Any:
    return _module("grove_mqtt_control_request_recorder", "grove-mqtt-control-request-recorder.py")


def _string(value: bytes) -> bytes:
    return len(value).to_bytes(2, "big") + value


def _packet(header: int, body: bytes) -> bytes:
    encoded = bytearray()
    remaining = len(body)
    while True:
        digit = remaining % 128
        remaining //= 128
        if remaining:
            digit |= 128
        encoded.append(digit)
        if not remaining:
            return bytes((header,)) + bytes(encoded) + body


def _connect() -> bytes:
    return _packet(
        0x10,
        _string(b"MQTT")
        + bytes((4, 0xC2))
        + (30).to_bytes(2, "big")
        + _string(b"bambuddy_" + SERIAL + b"_1_1")
        + _string(b"bblp")
        + _string(b"TEST0000"),
    )


def _subscribe(packet_id: int, suffix: bytes) -> bytes:
    return _packet(
        0x82,
        packet_id.to_bytes(2, "big") + _string(b"device/" + SERIAL + b"/" + suffix) + b"\0",
    )


def _publish(payload: object, packet_id: int, *, duplicate: bool = False) -> bytes:
    encoded = json.dumps(payload).encode("ascii")
    return _packet(
        0x3A if duplicate else 0x32,
        _string(b"device/" + SERIAL + b"/request") + packet_id.to_bytes(2, "big") + encoded,
    )


def _initial_flow() -> list[bytes]:
    return [
        _connect(),
        _subscribe(4, b"report"),
        _subscribe(5, b"request"),
        _publish({"pushing": {"command": "pushall"}}, 1),
        _publish({"info": {"sequence_id": "1", "command": "get_version"}}, 2),
        _publish(
            {
                "print": {
                    "command": "extrusion_cali_get",
                    "filament_id": "",
                    "nozzle_diameter": "0.4",
                    "sequence_id": "1",
                }
            },
            3,
        ),
    ]


class _Connection:
    def __init__(self, packets: list[bytes]) -> None:
        self.incoming = bytearray(b"".join(packets))
        self.writes: list[bytes] = []

    def read(self, size: int) -> bytes:
        if not self.incoming:
            raise TimeoutError
        output = bytes(self.incoming[:size])
        del self.incoming[:size]
        return output

    def write(self, value: bytes) -> int:
        self.writes.append(value)
        return len(value)

    def settimeout(self, _value: float) -> None:
        return None


def test_recorder_retains_only_static_control_schema_and_order() -> None:
    recorder = _recorder()
    packets = [
        *_initial_flow(),
        _publish({"print": {"command": "pause", "sequence_id": "0"}}, 6),
        _publish({"print": {"command": "resume", "sequence_id": "1"}}, 7),
        _publish({"print": {"command": "stop", "sequence_id": "2"}}, 8),
        _publish({"print": {"command": "pause", "sequence_id": "0"}}, 9),
    ]
    connection = _Connection(packets)

    candidate = recorder.observe_connection(connection)

    requests = candidate["observed"]["control_requests"]
    assert [request["operation"] for request in requests] == ["pause", "resume", "stop", "pause"]
    assert all(request["topic"] == "device/{serial}/request" for request in requests)
    assert all(request["qos"] == 1 and request["retain"] is False for request in requests)
    assert all(request["generated_sequence_marker"] is True for request in requests)
    assert [request["request_occurrence"] for request in requests] == [
        "first_observed_publish",
        "first_observed_publish",
        "first_observed_publish",
        "separate_publish_same_payload",
    ]
    assert all(request["puback_after_publish"] is True for request in requests)
    assert candidate["observed"]["post_control_quiescence_observed"] is True
    serialized = json.dumps(candidate, sort_keys=True)
    for forbidden in ("MQTTOBS0000001", "TEST0000", "packet_id", '"0"'):
        assert forbidden not in serialized
    assert connection.writes[-4:] == [
        b"@\x02\0\x06",
        b"@\x02\0\x07",
        b"@\x02\0\x08",
        b"@\x02\0\x09",
    ]


def test_recorder_marks_same_control_payload_replay_without_retaining_identity() -> None:
    recorder = _recorder()
    pause = {"print": {"command": "pause", "sequence_id": "0"}}
    candidate = recorder.observe_connection(
        _Connection(
            [
                *_initial_flow(),
                _publish(pause, 6),
                _publish({"print": {"command": "resume", "sequence_id": "1"}}, 7),
                _publish({"print": {"command": "stop", "sequence_id": "2"}}, 8),
                _publish(pause, 6, duplicate=True),
            ]
        )
    )

    replay = candidate["observed"]["control_requests"][3]
    assert replay["request_occurrence"] == "mqtt_dup_retransmission"
    assert replay["prior_publish_dup"] is False
    assert replay["matches_prior_publish"] is True


def test_recorder_distinguishes_a_separate_publish_from_mqtt_dup() -> None:
    recorder = _recorder()
    pause = {"print": {"command": "pause", "sequence_id": "0"}}
    candidate = recorder.observe_connection(
        _Connection(
            [
                *_initial_flow(),
                _publish(pause, 6),
                _publish({"print": {"command": "resume", "sequence_id": "1"}}, 7),
                _publish({"print": {"command": "stop", "sequence_id": "2"}}, 8),
                _publish(pause, 9),
            ]
        )
    )

    repeated = candidate["observed"]["control_requests"][3]
    assert repeated["request_occurrence"] == "separate_publish_same_payload"
    assert repeated["prior_publish_dup"] is False
    assert repeated["matches_prior_publish"] is False


def test_candidate_validation_rejects_extras_wrong_order_and_nonfinite_or_duplicate_json() -> None:
    recorder = _recorder()
    candidate = recorder.observe_connection(
        _Connection(
            [
                *_initial_flow(),
                _publish({"print": {"command": "pause", "sequence_id": "0"}}, 6),
                _publish({"print": {"command": "resume", "sequence_id": "1"}}, 7),
                _publish({"print": {"command": "stop", "sequence_id": "2"}}, 8),
                _publish({"print": {"command": "pause", "sequence_id": "0"}}, 9),
            ]
        )
    )
    recorder.validate_candidate(candidate)

    candidate["unexpected"] = True
    with pytest.raises(recorder.ObservationFailure):
        recorder.validate_candidate(candidate)
    candidate.pop("unexpected")
    candidate["observed"]["control_requests"][0]["members"]["members"][0]["present"] = 1
    with pytest.raises(recorder.ObservationFailure):
        recorder.validate_candidate(candidate)
    with pytest.raises(recorder.ObservationFailure):
        recorder.parse_candidate_json('{"profile_version":1,"profile_version":1}')
    with pytest.raises(recorder.ObservationFailure):
        recorder.parse_candidate_json('{"profile_version":NaN}')


def test_candidate_validation_requires_the_direct_repeated_pause_evidence() -> None:
    recorder = _recorder()
    candidate = recorder.observe_connection(
        _Connection(
            [
                *_initial_flow(),
                _publish({"print": {"command": "pause", "sequence_id": "0"}}, 6),
                _publish({"print": {"command": "resume", "sequence_id": "1"}}, 7),
                _publish({"print": {"command": "stop", "sequence_id": "2"}}, 8),
            ]
        )
    )

    with pytest.raises(recorder.ObservationFailure):
        recorder.validate_candidate(candidate)


@pytest.mark.parametrize(
    ("packet_id", "duplicate"),
    [(7, True), (6, False)],
)
def test_recorder_rejects_inconsistent_replay_packet_id_relations(
    packet_id: int,
    duplicate: bool,
) -> None:
    recorder = _recorder()
    pause = {"print": {"command": "pause", "sequence_id": "0"}}

    with pytest.raises(recorder.ObservationFailure):
        recorder.observe_connection(
            _Connection(
                [
                    *_initial_flow(),
                    _publish(pause, 6),
                    _publish({"print": {"command": "resume", "sequence_id": "1"}}, 7),
                    _publish({"print": {"command": "stop", "sequence_id": "2"}}, 8),
                    _publish(pause, packet_id, duplicate=duplicate),
                ]
            )
        )


@pytest.mark.parametrize(
    ("operation", "duplicate"), [("resume", False), ("stop", False), ("resume", True)]
)
def test_recorder_rejects_cross_operation_packet_id_reuse(
    operation: str,
    duplicate: bool,
) -> None:
    recorder = _recorder()

    with pytest.raises(recorder.ObservationFailure):
        recorder.observe_connection(
            _Connection(
                [
                    *_initial_flow(),
                    _publish({"print": {"command": "pause", "sequence_id": "0"}}, 6),
                    _publish(
                        {"print": {"command": operation, "sequence_id": "1"}},
                        6,
                        duplicate=duplicate,
                    ),
                ]
            )
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"print": {"command": "pause"}},
        {"print": {"command": "gcode_line", "sequence_id": "1"}},
        {"print": {"command": "pause", "sequence_id": "one"}},
        {"print": {"command": "pause", "sequence_id": "1", "extra": "x"}},
        {"other": {"command": "pause", "sequence_id": "1"}},
    ],
)
def test_control_payload_rejects_non_whitelisted_schemas(payload: dict[str, object]) -> None:
    recorder = _recorder()

    with pytest.raises(recorder.ObservationFailure):
        recorder.sanitize_control_payload(json.dumps(payload).encode("ascii"))


def test_control_publish_rejects_wrong_topic_and_qos() -> None:
    recorder = _recorder()
    payload = json.dumps({"print": {"command": "pause", "sequence_id": "1"}}).encode("ascii")
    body = _string(b"device/" + SERIAL + b"/report") + b"\0\x01" + payload
    with pytest.raises(recorder.ObservationFailure):
        recorder.parse_control_publish(0x32, body, SERIAL)
    body = _string(b"device/" + SERIAL + b"/request") + b"\0\x01" + payload
    with pytest.raises(recorder.ObservationFailure):
        recorder.parse_control_publish(0x30, body, SERIAL)


def test_public_drive_uses_only_exact_control_endpoint_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drive = _module("grove_mqtt_control_request_drive", "grove-mqtt-control-request-drive.py")
    calls: list[tuple[str, str, bytes | None]] = []

    def request(path: str, *, method: str = "POST", data: bytes | None = None) -> tuple[int, bytes]:
        calls.append((path, method, data))
        if path == "/api/v1/printers/":
            return 200, b'{"id":1}'
        if path == "/api/v1/printers/1/status":
            return 200, b'{"id":1,"connected":true}'
        return 200, b"{}"

    monkeypatch.setattr(drive, "_request", request)
    monkeypatch.setattr(drive.time, "sleep", lambda _seconds: None)

    assert drive.main(["192.0.2.1"]) == 0
    assert [path for path, _method, _data in calls] == [
        "/api/v1/printers/",
        "/api/v1/printers/1/status",
        "/api/v1/printers/1/status",
        "/api/v1/printers/1/print/pause",
        "/api/v1/printers/1/print/resume",
        "/api/v1/printers/1/print/stop",
        "/api/v1/printers/1/print/pause",
    ]
    assert [method for _path, method, _data in calls] == [
        "POST",
        "GET",
        "GET",
        "POST",
        "POST",
        "POST",
        "POST",
    ]
    assert all(data is None for _path, _method, data in calls[1:])


def test_public_drive_denies_control_until_two_connected_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drive = _module("grove_mqtt_control_request_drive", "grove-mqtt-control-request-drive.py")
    calls: list[str] = []

    def request(path: str, *, method: str = "POST", data: bytes | None = None) -> tuple[int, bytes]:
        calls.append(path)
        if path == "/api/v1/printers/":
            return 200, b'{"id":1}'
        assert method == "GET"
        assert data is None
        return 200, b'{"id":1,"connected":false}'

    monkeypatch.setattr(drive, "_request", request)
    monkeypatch.setattr(drive.time, "sleep", lambda _seconds: None)

    assert drive.main(["192.0.2.1"]) == 1
    assert all("/print/" not in path for path in calls)
