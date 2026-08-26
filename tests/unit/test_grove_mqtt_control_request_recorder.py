from __future__ import annotations

import importlib.util
import io
import json
import ssl
import sys
from collections.abc import Callable
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


def _diagnostics() -> Any:
    return _module(
        "grove_mqtt_control_request_diagnostics", "grove-mqtt-control-request-diagnostics.py"
    )


def _host_runner() -> Any:
    return _module("run_grove_mqtt_control_driver", "run-grove-mqtt-control-driver.py")


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


def _connect(serial: bytes = SERIAL) -> bytes:
    return _packet(
        0x10,
        _string(b"MQTT")
        + bytes((4, 0xC2))
        + (30).to_bytes(2, "big")
        + _string(b"bambuddy_" + serial + b"_1_1")
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


class _EofConnection(_Connection):
    def read(self, size: int) -> bytes:
        if not self.incoming:
            return b""
        return super().read(size)


class _RaggedEofConnection(_Connection):
    def read(self, size: int) -> bytes:
        if not self.incoming:
            raise ssl.SSLEOFError(8, "ragged EOF")
        return super().read(size)


class _Listener:
    def __init__(
        self,
        connections: list[_Connection],
        *,
        on_accept: Callable[[int], None] | None = None,
    ) -> None:
        self.connections = connections
        self.on_accept = on_accept
        self.accepts = 0
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def accept(self) -> tuple[_Connection, tuple[str, int]]:
        if self.accepts == len(self.connections):
            raise TimeoutError
        connection = self.connections[self.accepts]
        self.accepts += 1
        if self.on_accept is not None:
            self.on_accept(self.accepts)
        return connection, ("192.0.2.1", 8883)


class _WrappedConnection:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.closed = False

    def __enter__(self) -> _Connection:
        return self.connection

    def __exit__(self, *_arguments: object) -> None:
        self.closed = True


class _TlsContext:
    def __init__(self) -> None:
        self.sessions: list[_WrappedConnection] = []

    def wrap_socket(
        self,
        connection: _Connection,
        *,
        server_side: bool,
        suppress_ragged_eofs: bool,
    ) -> _WrappedConnection:
        assert server_side is True
        assert suppress_ragged_eofs is False
        wrapped = _WrappedConnection(connection)
        self.sessions.append(wrapped)
        return wrapped


def _probe_flow() -> list[bytes]:
    return [*_initial_flow(), _packet(0xE0, b"")]


def _control_flow() -> list[bytes]:
    return [
        *_initial_flow(),
        _publish({"print": {"command": "pause", "sequence_id": "0"}}, 6),
        _publish({"print": {"command": "resume", "sequence_id": "1"}}, 7),
        _publish({"print": {"command": "stop", "sequence_id": "2"}}, 8),
        _publish({"print": {"command": "pause", "sequence_id": "0"}}, 9),
    ]


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


def test_recorder_requires_exactly_two_tls_sessions_before_controls() -> None:
    recorder = _recorder()
    probe = _Connection(_probe_flow())
    persistent = _Connection(_control_flow())
    extra = _Connection(_control_flow())
    listener = _Listener([probe, persistent, extra])
    context = _TlsContext()

    candidate = recorder.observe_two_tls_sessions(listener, context)

    assert candidate["observed"]["post_control_quiescence_observed"] is True
    assert listener.accepts == recorder.MAX_TLS_SESSIONS
    assert [wrapped.connection for wrapped in context.sessions] == [probe, persistent]
    assert all(wrapped.closed for wrapped in context.sessions)
    assert not extra.writes
    assert probe.writes[-3:] == [b"@\x02\0\x01", b"@\x02\0\x02", b"@\x02\0\x03"]
    assert persistent.writes[-4:] == [
        b"@\x02\0\x06",
        b"@\x02\0\x07",
        b"@\x02\0\x08",
        b"@\x02\0\x09",
    ]


def test_recorder_accepts_only_clean_probe_disconnect_or_eof() -> None:
    recorder = _recorder()
    candidate = recorder.observe_two_sessions(
        _EofConnection(_initial_flow()), _Connection(_control_flow())
    )

    assert candidate["observed"]["post_control_quiescence_observed"] is True
    for probe_packets in (
        [*_initial_flow(), _packet(0xC0, b"")],
        [*_initial_flow(), _packet(0xE0, b"x")],
        _initial_flow(),
    ):
        with pytest.raises((recorder.ObservationFailure, TimeoutError)):
            recorder.observe_two_sessions(_Connection(probe_packets), _Connection(_control_flow()))


def test_recorder_rejects_ragged_tls_eof_in_either_session() -> None:
    recorder = _recorder()
    with pytest.raises(ssl.SSLEOFError):
        recorder.observe_two_tls_sessions(
            _Listener([_RaggedEofConnection(_initial_flow()), _Connection(_control_flow())]),
            _TlsContext(),
        )
    with pytest.raises(ssl.SSLEOFError):
        recorder.observe_two_tls_sessions(
            _Listener([_Connection(_probe_flow()), _RaggedEofConnection(_initial_flow())]),
            _TlsContext(),
        )


def test_recorder_rejects_probe_controls_missing_persistent_session_and_serial_mismatch() -> None:
    recorder = _recorder()
    probe_control = [
        *_initial_flow(),
        _publish({"print": {"command": "pause", "sequence_id": "0"}}, 6),
    ]
    no_persistent_handshake = [
        _publish({"print": {"command": "pause", "sequence_id": "0"}}, 6),
    ]
    mismatched_persistent = [
        _connect(b"OTHER0000000001"),
        *_initial_flow()[1:],
    ]

    persistent_after_probe_control = _Connection(_control_flow())
    with pytest.raises(recorder.ObservationFailure):
        recorder.observe_two_sessions(_Connection(probe_control), persistent_after_probe_control)
    assert not persistent_after_probe_control.writes
    with pytest.raises(recorder.ObservationFailure):
        recorder.observe_two_sessions(
            _Connection(_probe_flow()), _Connection(no_persistent_handshake)
        )
    with pytest.raises(recorder.ObservationFailure):
        recorder.observe_two_sessions(
            _Connection(_probe_flow()), _Connection(mismatched_persistent)
        )


def test_recorder_rejects_missing_second_session() -> None:
    recorder = _recorder()
    listener = _Listener([_Connection(_probe_flow())])

    with pytest.raises(TimeoutError):
        recorder.observe_two_tls_sessions(listener, _TlsContext())
    assert listener.accepts == 1


def test_recorder_uses_one_bounded_deadline_for_both_accepts() -> None:
    recorder = _recorder()
    now = [0.0]

    def advance(accepts: int) -> None:
        now[0] += 40.0 if accepts == 1 else 6.0

    listener = _Listener(
        [_Connection(_probe_flow()), _Connection(_control_flow())], on_accept=advance
    )

    def clock() -> float:
        return now[0]

    with pytest.raises(TimeoutError):
        recorder.observe_two_tls_sessions(listener, _TlsContext(), clock=clock)
    assert listener.timeouts == [45.0, 5.0]


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


def _private_status(path: Path, contents: str, mode: int = 0o600) -> Path:
    path.write_text(contents, encoding="ascii")
    path.chmod(mode)
    return path


def test_private_diagnostics_accept_only_exact_positive_statuses(tmp_path: Path) -> None:
    diagnostics = _diagnostics()
    driver = _private_status(tmp_path / "driver", "driver complete: controls\n")
    recorder = _private_status(
        tmp_path / "recorder",
        '{"status":"failure","code":"protocol_failure"}',
    )

    assert diagnostics.validate_driver_status(driver, 0) == diagnostics.DRIVER_SUCCESS
    assert (
        diagnostics.validate_recorder_status(recorder, 1)
        == "MQTT_CONTROL_CAPTURE_RECORDER_PROTOCOL_FAILURE"
    )


@pytest.mark.parametrize(
    ("contents", "mode"),
    [
        ("driver failed: control_pause_repeat\n", 0o600),
        ("hostile output\n", 0o600),
        ("driver complete: controls\n", 0o644),
        ("x" * 257, 0o600),
    ],
)
def test_private_driver_diagnostics_reject_or_map_only_allowlisted_output(
    tmp_path: Path,
    contents: str,
    mode: int,
) -> None:
    diagnostics = _diagnostics()
    path = _private_status(tmp_path / "driver", contents, mode)
    expected = (
        "MQTT_CONTROL_CAPTURE_DRIVER_PAUSE_REPEAT"
        if contents == "driver failed: control_pause_repeat\n"
        else diagnostics.DRIVER_INVALID
    )

    assert diagnostics.validate_driver_status(path, 1) == expected


def test_private_diagnostics_reject_missing_and_hostile_recorder_status(tmp_path: Path) -> None:
    diagnostics = _diagnostics()
    missing = tmp_path / "missing"
    duplicate = _private_status(
        tmp_path / "duplicate",
        '{"status":"failure","status":"failure","code":"timeout"}',
    )
    oversized = _private_status(tmp_path / "oversized", "x" * 257)

    assert diagnostics.validate_recorder_status(missing, 1) == diagnostics.RECORDER_MISSING
    assert diagnostics.validate_recorder_status(duplicate, 1) == diagnostics.RECORDER_INVALID
    assert diagnostics.validate_recorder_status(oversized, 1) == diagnostics.RECORDER_INVALID


def test_host_runner_atomically_retains_bounded_driver_status(tmp_path: Path) -> None:
    runner = _host_runner()
    tmp_path.chmod(0o700)
    output = tmp_path / "driver-status"

    status = runner.capture_driver(
        [sys.executable, "-c", "import sys; sys.stdout.write('driver complete: controls\\n')"],
        io.BytesIO(),
        output,
    )

    assert status == 0
    assert output.read_text(encoding="ascii") == "driver complete: controls\n"
    assert output.stat().st_mode & 0o777 == 0o600
    assert not output.with_suffix(".tmp").exists()


def test_host_runner_rejects_preexisting_and_symlink_outputs(tmp_path: Path) -> None:
    runner = _host_runner()
    tmp_path.chmod(0o700)
    output = _private_status(tmp_path / "driver-status", "preserve\n")
    command = [sys.executable, "-c", "import sys; sys.stdout.write('ignored')"]

    assert (
        runner.capture_driver(command, io.BytesIO(), output) == runner.OUTCOME_PRIVATE_PATH_INVALID
    )
    assert output.read_text(encoding="ascii") == "preserve\n"
    output.unlink()
    output.symlink_to(tmp_path / "outside")
    assert (
        runner.capture_driver(command, io.BytesIO(), output) == runner.OUTCOME_PRIVATE_PATH_INVALID
    )


def test_host_runner_rejects_oversize_and_timeout_without_publishing(tmp_path: Path) -> None:
    runner = _host_runner()
    tmp_path.chmod(0o700)
    oversized = tmp_path / "oversized"
    timeout = tmp_path / "timeout"

    assert (
        runner.capture_driver(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 257)"],
            io.BytesIO(),
            oversized,
        )
        == runner.OUTCOME_OVERSIZE
    )
    assert not oversized.exists()
    assert (
        runner.capture_driver(
            [sys.executable, "-c", "import time; time.sleep(1)"],
            io.BytesIO(),
            timeout,
            timeout_seconds=0.01,
        )
        == runner.OUTCOME_TIMEOUT
    )
    assert not timeout.exists()


def test_host_runner_preserves_driver_exit_status(tmp_path: Path) -> None:
    runner = _host_runner()
    tmp_path.chmod(0o700)
    output = tmp_path / "driver-status"

    status = runner.capture_driver(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('driver failed: control_pause\\n'); sys.exit(7)",
        ],
        io.BytesIO(),
        output,
    )

    assert status == 7
    assert output.read_text(encoding="ascii") == "driver failed: control_pause\n"


def test_host_runner_accepts_only_the_aligned_bounded_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _host_runner()
    drive = tmp_path / "drive.py"
    drive.write_text("print('driver')\n", encoding="ascii")
    observed: list[float] = []

    def capture(
        _command: list[str],
        _source: Any,
        _output: Path,
        *,
        timeout_seconds: float,
    ) -> int:
        observed.append(timeout_seconds)
        return 0

    monkeypatch.setattr(runner, "capture_driver", capture)
    arguments = [str(tmp_path / "status"), "container", "192.0.2.1", str(drive)]

    assert runner.main([*arguments, "45"]) == 0
    assert observed == [runner.DRIVER_TIMEOUT_SECONDS]
    assert runner.main([*arguments, "44"]) == 2
    assert runner.main([*arguments, "not-a-timeout"]) == 2


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
    timeouts: list[float] = []

    def request(
        path: str,
        *,
        method: str = "POST",
        data: bytes | None = None,
        timeout_seconds: float = drive.REQUEST_TIMEOUT_SECONDS,
    ) -> tuple[int, bytes]:
        calls.append((path, method, data))
        timeouts.append(timeout_seconds)
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
    assert timeouts[0] == drive.REQUEST_TIMEOUT_SECONDS
    assert timeouts[-4:] == [drive.REQUEST_TIMEOUT_SECONDS] * 4


def test_public_drive_allows_two_fresh_connections_just_before_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drive = _module("grove_mqtt_control_request_drive", "grove-mqtt-control-request-drive.py")
    now = 0.0
    poll_times: list[float] = []
    status_timeouts: list[float] = []
    calls: list[str] = []

    def request(
        path: str,
        *,
        method: str = "POST",
        data: bytes | None = None,
        timeout_seconds: float = drive.REQUEST_TIMEOUT_SECONDS,
    ) -> tuple[int, bytes]:
        calls.append(path)
        if path == "/api/v1/printers/":
            return 200, b'{"id":1}'
        if path == "/api/v1/printers/1/status":
            poll_times.append(now)
            status_timeouts.append(timeout_seconds)
            if len(poll_times) in {77, 79, 80}:
                return 200, b'{"id":1,"connected":true}'
            return 200, b'{"id":1,"connected":false}'
        return 200, b"{}"

    def clock() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    monkeypatch.setattr(drive, "_request", request)
    monkeypatch.setattr(drive.time, "monotonic", clock)
    monkeypatch.setattr(drive.time, "sleep", sleep)

    assert drive.main(["192.0.2.1"]) == 0
    assert poll_times[-2:] == [19.5, 19.75]
    assert status_timeouts[-2:] == [0.5, 0.25]
    assert len(poll_times) == 80
    assert [path for path in calls if "/print/" in path] == [
        "/api/v1/printers/1/print/pause",
        "/api/v1/printers/1/print/resume",
        "/api/v1/printers/1/print/stop",
        "/api/v1/printers/1/print/pause",
    ]


def test_public_drive_times_out_without_control_until_two_connected_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drive = _module("grove_mqtt_control_request_drive", "grove-mqtt-control-request-drive.py")
    calls: list[str] = []
    poll_times: list[float] = []
    now = 0.0

    def request(
        path: str,
        *,
        method: str = "POST",
        data: bytes | None = None,
        timeout_seconds: float = drive.REQUEST_TIMEOUT_SECONDS,
    ) -> tuple[int, bytes]:
        calls.append(path)
        if path == "/api/v1/printers/":
            return 200, b'{"id":1}'
        poll_times.append(now)
        assert method == "GET"
        assert data is None
        return 200, b'{"id":1,"connected":false}'

    def clock() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    monkeypatch.setattr(drive, "_request", request)
    monkeypatch.setattr(drive.time, "monotonic", clock)
    monkeypatch.setattr(drive.time, "sleep", sleep)

    assert drive.main(["192.0.2.1"]) == 1
    assert len(poll_times) == 80
    assert now == 20.0
    assert all("/print/" not in path for path in calls)


def test_public_drive_rejects_a_connected_response_arriving_at_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drive = _module("grove_mqtt_control_request_drive", "grove-mqtt-control-request-drive.py")
    now = 0.0
    poll_times: list[float] = []
    status_timeouts: list[float] = []
    calls: list[str] = []

    def request(
        path: str,
        *,
        method: str = "POST",
        data: bytes | None = None,
        timeout_seconds: float = drive.REQUEST_TIMEOUT_SECONDS,
    ) -> tuple[int, bytes]:
        nonlocal now
        calls.append(path)
        if path == "/api/v1/printers/":
            return 200, b'{"id":1}'
        if path == "/api/v1/printers/1/status":
            poll_times.append(now)
            status_timeouts.append(timeout_seconds)
            if len(poll_times) == 79:
                return 200, b'{"id":1,"connected":true}'
            if len(poll_times) == 80:
                now = 20.0
                return 200, b'{"id":1,"connected":true}'
            return 200, b'{"id":1,"connected":false}'
        return 200, b"{}"

    def clock() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    monkeypatch.setattr(drive, "_request", request)
    monkeypatch.setattr(drive.time, "monotonic", clock)
    monkeypatch.setattr(drive.time, "sleep", sleep)

    assert drive.main(["192.0.2.1"]) == 1
    assert poll_times[-2:] == [19.5, 19.75]
    assert status_timeouts[-2:] == [0.5, 0.25]
    assert all("/print/" not in path for path in calls)
