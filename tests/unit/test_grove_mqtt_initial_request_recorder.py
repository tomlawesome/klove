# ruff: noqa: E501 -- embedded mock shell records exact command lines.

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _recorder() -> Any:
    spec = importlib.util.spec_from_file_location(
        "grove_mqtt_initial_request_recorder",
        SCRIPTS / "grove-mqtt-initial-request-recorder.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mqtt_string(value: bytes) -> bytes:
    return len(value).to_bytes(2, "big") + value


def _publish(
    command: str,
    *,
    topic: bytes = b"device/MQTTOBS0000001/request",
    extra_member: bool = False,
) -> tuple[int, bytes]:
    request_payloads = {
        "pushall": {"pushing": {"command": command}},
        "get_version": {"info": {"sequence_id": "1", "command": command}},
        "extrusion_cali_get": {
            "print": {
                "command": command,
                "filament_id": "",
                "nozzle_diameter": "0.4",
                "sequence_id": "1",
            }
        },
    }
    print_payload = request_payloads.get(command, {"print": {"command": command}})
    if extra_member:
        next(iter(print_payload.values()))["secret"] = "must-not-retain"  # noqa: S105 -- hostile test input.
    payload = json.dumps(print_payload).encode()
    return 0x32, _mqtt_string(topic) + b"\x00\x01" + payload


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


def _publish_packet(command: str, packet_id: bytes) -> bytes:
    header, body = _publish(command)
    packet_id_offset = 2 + int.from_bytes(body[:2], "big")
    return _packet(
        header,
        body[:packet_id_offset] + packet_id + body[packet_id_offset + 2 :],
    )


def _connect(
    *,
    serial: bytes = b"MQTTOBS0000001",
    flags: int = 0xC2,
    keepalive: int = 30,
    client_id: bytes | None = None,
) -> bytes:
    if client_id is None:
        client_id = b"bambuddy_" + serial + b"_1_1"
    body = (
        _mqtt_string(b"MQTT")
        + bytes((4, flags))
        + keepalive.to_bytes(2, "big")
        + _mqtt_string(client_id)
        + _mqtt_string(b"bblp")
        + _mqtt_string(b"TEST0000")
    )
    return _packet(0x10, body)


def _subscribe(
    serial: bytes = b"MQTTOBS0000001",
    *,
    packet_id: bytes = b"\x00\x04",
    suffixes: tuple[bytes, ...] = (b"report",),
) -> bytes:
    body = bytearray(packet_id)
    for suffix in suffixes:
        body.extend(_mqtt_string(b"device/" + serial + b"/" + suffix))
        body.append(0)
    return _packet(0x82, bytes(body))


def _captured_initial_flow() -> bytes:
    packets = [
        _connect(),
        _subscribe(packet_id=b"\x00\x04"),
        _subscribe(packet_id=b"\x00\x05", suffixes=(b"request",)),
    ]
    for command, packet_id in zip(
        ("pushall", "get_version", "extrusion_cali_get"),
        (b"\x00\x01", b"\x00\x02", b"\x00\x03"),
        strict=True,
    ):
        packets.append(_publish_packet(command, packet_id))
    return b"".join(packets)


class _Connection:
    def __init__(  # noqa: PLR0913 -- configurable bounded fake transport.
        self,
        incoming: bytes,
        *,
        write_limit: int | None = None,
        timeout_when_empty: bool = False,
        clock: _FakeClock | None = None,
        read_advance: float = 0,
        write_advance: float = 0,
        read_limit: int | None = None,
    ) -> None:
        self.incoming = bytearray(incoming)
        self.write_limit = write_limit
        self.timeout_when_empty = timeout_when_empty
        self.clock = clock
        self.read_advance = read_advance
        self.write_advance = write_advance
        self.read_limit = read_limit
        self.applied_timeout: float | None = None
        self.timeouts: list[float] = []
        self.writes: list[bytes] = []

    def read(self, size: int) -> bytes:
        if not self.incoming and self.timeout_when_empty:
            raise TimeoutError
        self._advance(self.read_advance)
        if self.read_limit is not None:
            size = min(size, self.read_limit)
        chunk = bytes(self.incoming[:size])
        del self.incoming[:size]
        return chunk

    def write(self, value: bytes) -> int:
        self._advance(self.write_advance)
        progress = len(value) if self.write_limit is None else min(self.write_limit, len(value))
        if progress > 0:
            self.writes.append(value[:progress])
        return progress

    def settimeout(self, value: float) -> None:
        self.applied_timeout = value
        self.timeouts.append(value)

    def _advance(self, amount: float) -> None:
        if self.clock is None or amount == 0:
            return
        if self.applied_timeout is not None and amount >= self.applied_timeout:
            self.clock.advance(self.applied_timeout)
            raise TimeoutError
        self.clock.advance(amount)


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.mark.parametrize(
    "encoded",
    [
        [b"\xff", b"\xff", b"\xff", b"\xff"],
        [b"\x80", b"\x80", b"\x80", b"\x80"],
        [b"\x80"],
        [b"\x80", b"\x00"],
        [b"\x81", b"\x00"],
    ],
)
def test_remaining_length_bounds_fail_before_packet_body_allocation(encoded: list[bytes]) -> None:
    recorder = _recorder()
    iterator = iter(encoded)
    with pytest.raises(recorder.ObservationFailure):
        recorder.decode_remaining_length(lambda: next(iterator, b""))


def test_publish_sanitizer_retains_schema_not_secret_values_or_packet_id() -> None:
    recorder = _recorder()
    header, body = _publish("pushall")

    request, packet_id = recorder.parse_publish(header, body, b"MQTTOBS0000001")

    serialized = json.dumps(request, sort_keys=True)
    assert packet_id == b"\x00\x01"
    assert request["topic"] == "device/{serial}/request"
    assert request["command"] == "pushall"
    assert request["generated_sequence_marker"] is False
    assert "must-not-retain" not in serialized
    assert "0001" not in serialized
    assert "MQTTOBS0000001" not in serialized
    assert request["members"] == {
        "type": "object",
        "members": [
            {
                "name": "pushing",
                "present": True,
                "schema": {
                    "type": "object",
                    "members": [{"name": "command", "present": True, "schema": {"type": "string"}}],
                },
            }
        ],
    }


def test_publish_rejects_unsupported_command_topic_and_malformed_json() -> None:
    recorder = _recorder()
    header, body = _publish("pause")
    with pytest.raises(recorder.ObservationFailure):
        recorder.parse_publish(header, body, b"MQTTOBS0000001")
    header, body = _publish("pushall", topic=b"device/MQTTOBS0000001/report")
    with pytest.raises(recorder.ObservationFailure):
        recorder.parse_publish(header, body, b"MQTTOBS0000001")
    with pytest.raises(recorder.ObservationFailure):
        recorder.sanitize_payload(b'{"print":{"command":"pushall"')
    header, body = _publish("pushall", extra_member=True)
    with pytest.raises(recorder.ObservationFailure):
        recorder.parse_publish(header, body, b"MQTTOBS0000001")


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
def test_payload_rejects_nonstandard_json_constants(constant: bytes) -> None:
    recorder = _recorder()
    with pytest.raises(recorder.ObservationFailure):
        recorder.sanitize_payload(b'{"print":{"command":"pushall","value":' + constant + b"}}")


def test_session_binds_connect_subscribe_and_publish_serial_and_proves_puback_ordering() -> None:
    recorder = _recorder()
    connection = _Connection(_captured_initial_flow() + _packet(0xE0, b""))

    candidate = recorder.observe_connection(connection)

    assert candidate["observed"]["first_puback_after_next_initial_publish"] is True
    assert (
        candidate["observed"]["initial_requests"][0]["puback_order"] == "after_next_initial_publish"
    )
    assert [
        request["generated_sequence_marker"]
        for request in candidate["observed"]["initial_requests"]
    ] == [False, True, True]
    assert [
        request["members"]["members"][0]["name"]
        for request in candidate["observed"]["initial_requests"]
    ] == ["pushing", "info", "print"]
    assert connection.writes == [
        b"\x20\x02\x00\x00",
        b"\x90\x03\x00\x04\x00",
        b"\x90\x03\x00\x05\x00",
        b"\x40\x02\x00\x01",
        b"\x40\x02\x00\x02",
        b"\x40\x02\x00\x03",
    ]
    assert len(connection.timeouts) == 2
    assert all(0 < timeout <= 10 for timeout in connection.timeouts)


def test_post_capture_accepts_ping_then_requires_clean_disconnect() -> None:
    recorder = _recorder()
    connection = _Connection(_captured_initial_flow() + _packet(0xC0, b"") + _packet(0xE0, b""))

    candidate = recorder.observe_connection(connection)

    assert candidate["observed"]["first_puback_after_next_initial_publish"] is True
    assert connection.writes[-1] == b"\xd0\x00"
    assert len(connection.timeouts) == 5


def test_post_capture_rejects_eof_timeout_and_extra_packet() -> None:
    recorder = _recorder()
    with pytest.raises(recorder.ObservationFailure):
        recorder.observe_connection(_Connection(_captured_initial_flow()))
    with pytest.raises(TimeoutError):
        recorder.observe_connection(_Connection(_captured_initial_flow(), timeout_when_empty=True))
    with pytest.raises(recorder.ObservationFailure):
        recorder.observe_connection(
            _Connection(_captured_initial_flow() + _publish_packet("pushall", b"\x00\x06"))
        )


def test_post_capture_deadline_cannot_be_extended_by_trickled_body_reads() -> None:
    recorder = _recorder()
    clock = _FakeClock()
    connection = _Connection(
        _packet(0xC0, b"xxxx"),
        clock=clock,
        read_advance=3,
        read_limit=1,
    )

    with pytest.raises(TimeoutError):
        recorder._wait_for_clean_disconnect(connection, clock=clock)

    assert clock.value == pytest.approx(10)
    assert connection.incoming
    assert connection.timeouts == sorted(connection.timeouts, reverse=True)


def test_post_capture_deadline_cannot_be_extended_by_partial_writes() -> None:
    recorder = _recorder()
    clock = _FakeClock()
    connection = _Connection(
        _packet(0xC0, b"") + _packet(0xE0, b""),
        clock=clock,
        write_advance=6,
        write_limit=1,
    )

    with pytest.raises(TimeoutError):
        recorder._wait_for_clean_disconnect(connection, clock=clock)

    assert clock.value == pytest.approx(10)
    assert connection.writes == [b"\xd0"]
    assert connection.timeouts[-1] == pytest.approx(4)


def test_ping_before_post_capture_phase_is_rejected() -> None:
    recorder = _recorder()
    with pytest.raises(recorder.ObservationFailure):
        recorder.observe_connection(
            _Connection(
                _connect()
                + _subscribe()
                + _subscribe(packet_id=b"\x00\x05", suffixes=(b"request",))
                + _packet(0xC0, b"")
            )
        )


def test_session_rejects_second_publish_reusing_unacked_first_packet_id() -> None:
    recorder = _recorder()
    connection = _Connection(
        _connect()
        + _subscribe(packet_id=b"\x00\x04")
        + _subscribe(packet_id=b"\x00\x05", suffixes=(b"request",))
        + _publish_packet("pushall", b"\x00\x01")
        + _publish_packet("get_version", b"\x00\x01")
    )

    with pytest.raises(recorder.ObservationFailure):
        recorder.observe_connection(connection)

    assert connection.writes == [
        b"\x20\x02\x00\x00",
        b"\x90\x03\x00\x04\x00",
        b"\x90\x03\x00\x05\x00",
    ]


def test_write_all_completes_partial_progress_and_rejects_zero_progress() -> None:
    recorder = _recorder()
    partial = _Connection(b"", write_limit=2)

    recorder._write_all(partial, b"abcdef")

    assert partial.writes == [b"ab", b"cd", b"ef"]
    with pytest.raises(recorder.ObservationFailure):
        recorder._write_all(_Connection(b"", write_limit=0), b"x")


def test_failure_status_is_fixed_owner_private_and_contains_no_exception_text(
    tmp_path: Path,
) -> None:
    recorder = _recorder()
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    recorder.STATUS_PATH = evidence / "mqtt-initial-request-status"

    recorder._write_failure_status(recorder._failure_code(recorder.ObservationFailure("secret")))

    assert json.loads(recorder.STATUS_PATH.read_text(encoding="ascii")) == {
        "status": "failure",
        "code": "protocol_failure",
    }
    assert stat.S_IMODE(recorder.STATUS_PATH.stat().st_mode) == 0o600
    assert "secret" not in recorder.STATUS_PATH.read_text(encoding="ascii")
    with pytest.raises(recorder.ObservationFailure):
        recorder._write_failure_status("unbounded_value")


def test_recorder_provenance_reports_exact_interpreter_and_openssl_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _recorder()
    monkeypatch.setattr(recorder.platform, "python_version", lambda: "3.13.15")
    monkeypatch.setattr(recorder.ssl, "OPENSSL_VERSION", "OpenSSL 3.5.6")

    assert recorder._recorder_tool_versions() == {
        "python": "3.13.15",
        "openssl": "OpenSSL 3.5.6",
    }


@pytest.mark.parametrize(
    ("python", "openssl"),
    [
        ("3.13.15 peer-value", "OpenSSL 3.5.6"),
        ("3.13.15", "OpenSSL 3.5.6 peer-value"),
        ("3.12.15", "OpenSSL 3.5.6"),
    ],
)
def test_recorder_provenance_rejects_unrecognized_tool_versions(
    monkeypatch: pytest.MonkeyPatch, python: str, openssl: str
) -> None:
    recorder = _recorder()
    monkeypatch.setattr(recorder.platform, "python_version", lambda: python)
    monkeypatch.setattr(recorder.ssl, "OPENSSL_VERSION", openssl)

    with pytest.raises(recorder.ObservationFailure):
        recorder._recorder_tool_versions()


def test_session_rejects_publish_before_subscribe_and_serial_mismatch() -> None:
    recorder = _recorder()
    header, body = _publish("pushall")
    with pytest.raises(recorder.ObservationFailure):
        recorder.observe_connection(_Connection(_connect() + _packet(header, body)))
    with pytest.raises(recorder.ObservationFailure):
        recorder.observe_connection(_Connection(_connect() + _subscribe() + _packet(header, body)))
    with pytest.raises(recorder.ObservationFailure):
        recorder.observe_connection(_Connection(_connect() + _subscribe(b"OTHER0000000001")))


def test_connect_rejects_non_observed_flags_and_keepalive() -> None:
    recorder = _recorder()
    with pytest.raises(recorder.ObservationFailure):
        recorder.observe_connection(_Connection(_connect(flags=0x82)))
    with pytest.raises(recorder.ObservationFailure):
        recorder.observe_connection(_Connection(_connect(keepalive=29)))
    with pytest.raises(recorder.ObservationFailure):
        recorder.observe_connection(_Connection(_connect(client_id=b"bambuddy_MQTTOBS0000001__1")))
    with pytest.raises(recorder.ObservationFailure):
        recorder.observe_connection(_Connection(_connect(client_id=b"bambuddy_MQTTOBS0000001_1_")))
    with pytest.raises(recorder.ObservationFailure):
        recorder.observe_connection(_Connection(_connect(serial=b"OTHER0000000001")))


@pytest.mark.parametrize(
    "packet",
    [
        _subscribe(packet_id=b"\x00\x00"),
        _subscribe(suffixes=(b"request",)),
        _subscribe(suffixes=(b"report", b"report")),
        _subscribe(suffixes=(b"report", b"request", b"report")),
    ],
)
def test_subscribe_rejects_zero_id_reordering_duplicates_and_extras(packet: bytes) -> None:
    recorder = _recorder()
    with pytest.raises(recorder.ObservationFailure):
        recorder.observe_connection(_Connection(_connect() + packet))


@pytest.mark.parametrize(
    "subscriptions",
    [
        _subscribe() + _subscribe(packet_id=b"\x00\x05"),
        _subscribe() + _subscribe(packet_id=b"\x00\x04", suffixes=(b"request",)),
        _subscribe()
        + _subscribe(packet_id=b"\x00\x05", suffixes=(b"request",))
        + _subscribe(packet_id=b"\x00\x06"),
    ],
)
def test_subscribe_rejects_duplicate_topic_id_and_third_packet(subscriptions: bytes) -> None:
    recorder = _recorder()
    with pytest.raises(recorder.ObservationFailure):
        recorder.observe_connection(_Connection(_connect() + subscriptions))


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _copy_capture_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for name in (
        "capture-grove-mqtt-initial-requests.sh",
        "grove-mqtt-initial-request-drive.py",
        "grove-mqtt-initial-request-recorder.py",
        "grove-observation-down.sh",
        "grove-observation-lib.sh",
        "grove-observation-up.sh",
    ):
        shutil.copy2(SCRIPTS / name, scripts / name)
    return root


def _mock_environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    tools = tmp_path / "tools"
    tools.mkdir()
    log = tmp_path / "docker.log"
    _write_executable(tools / "timeout", '#!/usr/bin/env sh\nshift\nexec "$@"\n')
    _write_executable(tools / "sleep", "#!/usr/bin/env sh\nexit 0\n")
    _write_executable(
        tools / "date",
        "#!/usr/bin/env sh\nprintf '%s\\n' '2026-08-26T09:10:11Z'\n",
    )
    _write_executable(
        tools / "git",
        """#!/usr/bin/env sh
if [ "$1" = -C ]; then shift 2; fi
case "$1" in
  rev-parse) printf '%s\\n' df8a7ffa9e7809f797f1f247510127af9201eb8d ;;
  status) if [ "${MOCK_CAPTURE_DIRTY:-}" = 1 ]; then printf '%s\\n' ' M scripts/grove-mqtt-initial-request-recorder.py'; fi ;;
  *) exit 1 ;;
esac
""",
    )
    _write_executable(
        tools / "docker",
        """#!/usr/bin/env sh
printf '%s\\n' "$*" >> "$MOCK_DOCKER_LOG"
if [ "$1" = context ]; then printf '%s\\n' mock-context; exit 0; fi
if [ "$1" = info ]; then
  case "$*" in *'{{.ID}}'*) printf '%s\\n' mock-daemon ;; *) printf '%s\\n' '["name=rootless"]' ;; esac
  exit 0
fi
if [ "$1" = version ]; then printf '%s\\n' '29.7.2|29.7.2'; exit 0; fi
if [ "$1" = image ] && [ "$2" = inspect ]; then
  printf '%s\\n' "${MOCK_IMAGE_BINDING:-sha256:53a06a0021b138510ca6941d08ab473976fa06a0dcc22905d800cf25085929f5|127.0.0.1:5302/grove-observer@sha256:53a06a0021b138510ca6941d08ab473976fa06a0dcc22905d800cf25085929f5|cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4}"
  exit 0
fi
if [ "$1" = network ] && [ "$2" = create ]; then printf '%s\\n' mock-network; exit 0; fi
if [ "$1" = network ] && [ "$2" = inspect ]; then
  case "$*" in *'.Labels'*) printf '%s\\n' mock-run ;; *) printf '%s\\n' mock-network ;; esac
  exit 0
fi
if [ "$1" = network ] && [ "$2" = rm ]; then exit 0; fi
if [ "$1" = run ]; then
  case "$*" in
    *'mqtt-initial-request-recorder'*)
      case "${MOCK_LOADER_ARCH:-x86_64}" in
        x86_64) case "$*" in *'/lib64/ld-linux-x86-64.so.2'*) : ;; *) exit 43 ;; esac ;;
        aarch64) case "$*" in *'/lib/ld-linux-aarch64.so.1'*) : ;; *) exit 43 ;; esac ;;
        *) printf '%s\n' 'TEST0000 unsupported architecture stderr' >&2; exit 43 ;;
      esac
      if [ "${MOCK_PRE_READY_FAILURE:-}" = run ]; then
        printf '%s\n' 'TEST0000 docker run stderr' >&2
        exit 41
      fi
      for argument; do case "$argument" in *':/evidence:rw') evidence=${argument%:/evidence:rw} ;; esac; done
      printf '%s\n' "$evidence" > "$MOCK_EVIDENCE_PATH_FILE"
      case "${MOCK_READY_MODE:-immediate}" in
        hostile) printf '%s\n' '{"status":"ready","extra":"TEST0000"}' > "$evidence/mqtt-initial-request-ready"; chmod 600 "$evidence/mqtt-initial-request-ready" ;;
        delayed | bootstrap-exit) : ;;
        *) printf '%s\n' '{"status":"ready"}' > "$evidence/mqtt-initial-request-ready"; chmod 600 "$evidence/mqtt-initial-request-ready" ;;
      esac
      if [ "${MOCK_READY_MODE:-}" = bootstrap-exit ]; then
        :
      elif [ "${MOCK_DRIVER_FAILURE:-}" = 1 ] || [ "${MOCK_RECORDER_FAILURE:-}" = 1 ]; then
        status='{"status":"failure","code":"timeout"}'
        case "${MOCK_STATUS_MODE:-valid}" in
          hostile) status='{"status":"failure","code":"timeout","extra":"TEST0000"}' ;;
          list-code) status='{"status":"failure","code":[]}' ;;
          non-ascii) status= ;;
        esac
        if [ "${MOCK_STATUS_MODE:-valid}" = non-ascii ]; then
          printf '\\377' > "$evidence/mqtt-initial-request-status"
        else
          printf '%s\\n' "$status" > "$evidence/mqtt-initial-request-status"
        fi
        chmod 600 "$evidence/mqtt-initial-request-status"
      else
        candidate='{"profile_version":1,"transport":"mqtt-over-tls","observed":{"initial_requests":[{"topic":"device/{serial}/request","qos":1,"dup":false,"retain":false,"command":"pushall","members":{"type":"object","members":[{"name":"pushing","present":true,"schema":{"type":"object","members":[{"name":"command","present":true,"schema":{"type":"string"}}]}}]},"generated_sequence_marker":false,"payload_bytes":35,"puback_order":"after_next_initial_publish"},{"topic":"device/{serial}/request","qos":1,"dup":false,"retain":false,"command":"get_version","members":{"type":"object","members":[{"name":"info","present":true,"schema":{"type":"object","members":[{"name":"sequence_id","present":true,"schema":{"type":"string"}},{"name":"command","present":true,"schema":{"type":"string"}}]}}]},"generated_sequence_marker":true,"payload_bytes":56,"puback_order":"after_publish"},{"topic":"device/{serial}/request","qos":1,"dup":false,"retain":false,"command":"extrusion_cali_get","members":{"type":"object","members":[{"name":"print","present":true,"schema":{"type":"object","members":[{"name":"command","present":true,"schema":{"type":"string"}},{"name":"filament_id","present":true,"schema":{"type":"string"}},{"name":"nozzle_diameter","present":true,"schema":{"type":"string"}},{"name":"sequence_id","present":true,"schema":{"type":"string"}}]}}]},"generated_sequence_marker":true,"payload_bytes":109,"puback_order":"after_publish"}],"first_puback_after_next_initial_publish":true}}'
        case "${MOCK_CANDIDATE_MODE:-valid}" in
          secret) candidate='{"leak":"TEST0000"}' ;;
          extra) candidate=${candidate%?}',"extra":true}' ;;
          unknown-member) candidate=$(printf '%s' "$candidate" | sed 's/"pushing"/"peer_key"/') ;;
        esac
        printf '%s\\n' "$candidate" > "$evidence/mqtt-initial-request-schema"
        chmod 600 "$evidence/mqtt-initial-request-schema"
        provenance='{"python":"3.13.15","openssl":"OpenSSL 3.5.6"}'
        case "${MOCK_PROVENANCE_MODE:-valid}" in
          hostile) provenance='{"python":"TEST0000","openssl":"OpenSSL 3.5.6"}' ;;
          extra) provenance='{"python":"3.13.15","openssl":"OpenSSL 3.5.6","extra":true}' ;;
          version) provenance='{"python":"3.13.15 peer-value","openssl":"OpenSSL 3.5.6"}' ;;
        esac
        printf '%s\\n' "$provenance" > "$evidence/mqtt-initial-request-recorder-provenance"
        chmod 600 "$evidence/mqtt-initial-request-recorder-provenance"
      fi
      printf '%s\\n' mock-recorder
      ;;
    *) printf '%s\\n' mock-grove ;;
  esac
  exit 0
fi
if [ "$1" = wait ]; then
  if [ "${MOCK_READY_MODE:-}" = bootstrap-exit ] || [ "${MOCK_DRIVER_FAILURE:-}" = 1 ] || [ "${MOCK_RECORDER_FAILURE:-}" = 1 ]; then
    printf '%s\\n' 1
  else
    printf '%s\\n' 0
  fi
  exit 0
fi
if [ "$1" = logs ] || [ "$1" = rm ]; then exit 0; fi
if [ "$1" = exec ]; then
  case "$*" in
    *'python - 172.30.0.3'*)
      if [ "${MOCK_DRIVER_FAILURE:-}" = 1 ]; then exit 40; fi
      printf '%s\\n' 'public API accepted generated MQTT observation printer'
      ;;
    *'/api/v1/virtual-printers'*) printf '%s\\n' '{"name":"Klove Observation","status":{"running":true}}' ;;
  esac
  exit 0
fi
if [ "$1" = inspect ]; then
  resource=""; for argument; do resource=$argument; done
  case "$*" in
    *'.State.Running'*) failure_stage=readiness-inspect ;;
    *'.NetworkSettings.Networks'*) failure_stage=ip-inspect ;;
    *'.Id'*) failure_stage=id-inspect ;;
    *) failure_stage= ;;
  esac
  case "$resource" in
    klove-mqtt-initial-request-*)
      if [ -n "$failure_stage" ] \
        && [ "${MOCK_PRE_READY_FAILURE:-}" = "$failure_stage" ]; then
        printf '%s\n' 'TEST0000 docker inspect stderr' >&2
        exit 42
      fi
      if [ "${MOCK_DOCKER_EMPTY_RECORDER_ID:-}" = 1 ]; then
        case "$*" in *'.Id'*) exit 0 ;; esac
      fi
      ;;
  esac
  case "$*" in
    *'.State.Running'*)
      if [ "${MOCK_READY_MODE:-}" = bootstrap-exit ]; then
        printf '%s\\n' false
      else
        if [ "${MOCK_READY_MODE:-}" = delayed ]; then
          count=0
          if [ -f "$MOCK_READY_COUNT_FILE" ]; then read -r count < "$MOCK_READY_COUNT_FILE"; fi
          count=$((count + 1))
          printf '%s\\n' "$count" > "$MOCK_READY_COUNT_FILE"
          if [ "$count" -ge "${MOCK_READY_DELAY:-3}" ]; then
            read -r evidence < "$MOCK_EVIDENCE_PATH_FILE"
            printf '%s\\n' '{"status":"ready"}' > "$evidence/mqtt-initial-request-ready"
            chmod 600 "$evidence/mqtt-initial-request-ready"
          fi
        fi
        printf '%s\\n' true
      fi
      ;;
    *'grove-observation.role'*) printf '%s\\n' mqtt-initial-request-recorder ;;
    *'grove-observation.run-id'*) printf '%s\\n' mock-run ;;
    *'.Config.Image'*) printf '%s\\n' grove-observer:cdf6b829 ;;
    *'.Image'*) printf '%s\\n' sha256:53a06a0021b138510ca6941d08ab473976fa06a0dcc22905d800cf25085929f5 ;;
    *'.NetworkSettings.Networks'*) case "$resource" in *mqtt-initial-request*) printf '%s\\n' 172.30.0.3 ;; *) printf '%s\\n' 172.30.0.2 ;; esac ;;
    *'.Id'*) case "$resource" in *mqtt-initial-request*) printf '%s\\n' mock-recorder ;; *) printf '%s\\n' mock-grove ;; esac ;;
    *) printf '%s\\n' mock-grove ;;
  esac
  exit 0
fi
exit 0
""",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{tools}:{environment['PATH']}",
            "MOCK_DOCKER_LOG": str(log),
            "MOCK_EVIDENCE_PATH_FILE": str(tmp_path / "evidence-path"),
            "MOCK_READY_COUNT_FILE": str(tmp_path / "ready-count"),
        }
    )
    return environment, log


def test_capture_lifecycle_is_isolated_and_cleanup_is_identity_bound(tmp_path: Path) -> None:
    root = _copy_capture_tree(tmp_path)
    environment, log = _mock_environment(tmp_path)

    result = subprocess.run(  # noqa: S603 -- copied repository script and safe literal run ID.
        [str(root / "scripts/capture-grove-mqtt-initial-requests.sh"), "mock-run"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert '"initial_requests"' in result.stdout
    assert '"generated_sequence_marker":false' in result.stdout
    assert '"captured_at_utc":"2026-08-26T09:10:11Z"' in result.stdout
    assert '"command":"scripts/capture-grove-mqtt-initial-requests.sh mock-run"' in result.stdout
    assert '"harness_revision":"df8a7ffa9e7809f797f1f247510127af9201eb8d"' in result.stdout
    assert (
        '"digest":"sha256:53a06a0021b138510ca6941d08ab473976fa06a0dcc22905d800cf25085929f5"'
        in result.stdout
    )
    assert '"source_revision":"cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4"' in result.stdout
    assert '"docker_client":"29.7.2"' in result.stdout
    assert '"docker_server":"29.7.2"' in result.stdout
    assert '"openssl":"OpenSSL 3.5.6"' in result.stdout
    assert '"python":"3.13.15"' in result.stdout
    assert not (root / ".klove-integration/grove-observation/mock-run").exists()
    commands = log.read_text(encoding="utf-8")
    assert "network create --internal" in commands
    assert "--label io.klove.grove-observation.role=mqtt-initial-request-recorder" in commands
    recorder_commands = [line for line in commands.splitlines() if "mqtt-initial-request" in line]
    assert all("--cap-add NET_BIND_SERVICE" not in line for line in recorder_commands)
    assert "--read-only --tmpfs /tmp:rw,noexec,nosuid,size=4m" in commands
    assert "--memory 128m --cpus 0.5 --pids-limit 64" in commands
    assert "exec python /opt/recorder.py" not in commands
    assert "/lib64/ld-linux-x86-64.so.2" in commands
    assert "/lib/ld-linux-aarch64.so.1" in commands
    assert 'exec "$loader" /usr/local/bin/python3.13 /opt/recorder.py' in commands
    assert "wait klove-mqtt-initial-request-mock-run" in commands
    assert "rm --force klove-mqtt-initial-request-mock-run" in commands
    assert "rm --force klove-grove-observation-mock-run" in commands
    assert "network rm klove-grove-observation-mock-run" in commands


@pytest.mark.parametrize("mode", ["hostile", "extra", "version"])
def test_capture_rejects_invalid_recorder_provenance_without_leaking_it(
    tmp_path: Path, mode: str
) -> None:
    root = _copy_capture_tree(tmp_path)
    environment, _log = _mock_environment(tmp_path)
    environment["MOCK_PROVENANCE_MODE"] = mode

    result = subprocess.run(  # noqa: S603 -- copied repository script and safe literal run ID.
        [str(root / "scripts/capture-grove-mqtt-initial-requests.sh"), "mock-run"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert result.stderr == "MQTT recorder provenance is invalid\n"
    assert "TEST0000" not in result.stdout + result.stderr
    assert "OpenSSL 3.5.6" not in result.stdout + result.stderr
    assert "peer-value" not in result.stdout + result.stderr
    assert not (root / ".klove-integration/grove-observation/mock-run").exists()


def test_capture_rejects_unknown_candidate_member_without_echoing_it(tmp_path: Path) -> None:
    root = _copy_capture_tree(tmp_path)
    environment, _log = _mock_environment(tmp_path)
    environment["MOCK_CANDIDATE_MODE"] = "unknown-member"

    result = subprocess.run(  # noqa: S603 -- copied repository script and safe literal run ID.
        [str(root / "scripts/capture-grove-mqtt-initial-requests.sh"), "mock-run"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert result.stderr == "MQTT recorder evidence shape is invalid\n"
    assert "peer_key" not in result.stdout + result.stderr
    assert not (root / ".klove-integration/grove-observation/mock-run").exists()


@pytest.mark.parametrize(
    ("environment_key", "environment_value", "expected_error"),
    [
        ("MOCK_CAPTURE_DIRTY", "1", "MQTT capture harness is not committed\n"),
        (
            "MOCK_IMAGE_BINDING",
            "sha256:53a06a0021b138510ca6941d08ab473976fa06a0dcc22905d800cf25085929f5|127.0.0.1:5302/grove-observer@sha256:53a06a0021b138510ca6941d08ab473976fa06a0dcc22905d800cf25085929f5|peer-value",
            "MQTT capture image binding is invalid\n",
        ),
    ],
)
def test_capture_refuses_unbound_harness_or_image_before_starting_grove(
    tmp_path: Path, environment_key: str, environment_value: str, expected_error: str
) -> None:
    root = _copy_capture_tree(tmp_path)
    environment, log = _mock_environment(tmp_path)
    environment[environment_key] = environment_value

    result = subprocess.run(  # noqa: S603 -- copied repository script and safe literal run ID.
        [str(root / "scripts/capture-grove-mqtt-initial-requests.sh"), "mock-run"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert result.stderr == expected_error
    assert "peer-value" not in result.stdout + result.stderr
    assert not log.exists() or "network create" not in log.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("architecture", "loader"),
    [
        ("x86_64", "/lib64/ld-linux-x86-64.so.2"),
        ("aarch64", "/lib/ld-linux-aarch64.so.1"),
    ],
)
def test_capture_launches_recorder_through_exact_supported_dynamic_loader(
    tmp_path: Path, architecture: str, loader: str
) -> None:
    root = _copy_capture_tree(tmp_path)
    environment, log = _mock_environment(tmp_path)
    environment["MOCK_LOADER_ARCH"] = architecture

    result = subprocess.run(  # noqa: S603 -- copied repository script and safe literal run ID.
        [str(root / "scripts/capture-grove-mqtt-initial-requests.sh"), "mock-run"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    commands = log.read_text(encoding="utf-8")
    assert loader in commands
    assert "exec python /opt/recorder.py" not in commands
    assert 'exec "$loader" /usr/local/bin/python3.13 /opt/recorder.py' in commands


def test_capture_rejects_unknown_recorder_architecture_without_leaking_stderr(
    tmp_path: Path,
) -> None:
    root = _copy_capture_tree(tmp_path)
    environment, log = _mock_environment(tmp_path)
    environment["MOCK_LOADER_ARCH"] = "s390x"

    result = subprocess.run(  # noqa: S603 -- copied repository script and safe literal run ID.
        [str(root / "scripts/capture-grove-mqtt-initial-requests.sh"), "mock-run"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    combined = result.stdout + result.stderr
    state_dir = root / ".klove-integration/grove-observation/mock-run"
    assert result.returncode == 1
    assert result.stderr.count("MQTT recorder bootstrap failed") == 1
    assert "TEST0000" not in combined
    assert "Traceback" not in combined
    assert len(combined) < 1024
    commands = log.read_text(encoding="utf-8")
    assert "python - 172.30.0.3" not in commands
    assert "exec python /opt/recorder.py" not in commands
    assert (state_dir / "mqtt-initial-request-recorder-origin").is_file()


def test_capture_waits_for_delayed_private_ready_marker_before_driver(tmp_path: Path) -> None:
    root = _copy_capture_tree(tmp_path)
    environment, log = _mock_environment(tmp_path)
    environment.update({"MOCK_READY_MODE": "delayed", "MOCK_READY_DELAY": "3"})

    result = subprocess.run(  # noqa: S603 -- copied repository script and safe literal run ID.
        [str(root / "scripts/capture-grove-mqtt-initial-requests.sh"), "mock-run"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    commands = log.read_text(encoding="utf-8").splitlines()
    readiness = [index for index, line in enumerate(commands) if ".State.Running" in line]
    driver = next(index for index, line in enumerate(commands) if "python - 172.30.0.3" in line)
    assert len(readiness) == 3
    assert readiness[-1] < driver


def test_capture_rejects_hostile_ready_marker_without_running_driver(tmp_path: Path) -> None:
    root = _copy_capture_tree(tmp_path)
    environment, log = _mock_environment(tmp_path)
    environment["MOCK_READY_MODE"] = "hostile"

    result = subprocess.run(  # noqa: S603 -- copied repository script and safe literal run ID.
        [str(root / "scripts/capture-grove-mqtt-initial-requests.sh"), "mock-run"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "MQTT recorder ready marker is invalid" in result.stderr
    assert "TEST0000" not in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    assert "python - 172.30.0.3" not in log.read_text(encoding="utf-8")
    assert not (root / ".klove-integration/grove-observation/mock-run").exists()


def test_capture_bounds_exit_before_ready_without_status_or_driver(tmp_path: Path) -> None:
    root = _copy_capture_tree(tmp_path)
    environment, log = _mock_environment(tmp_path)
    environment["MOCK_READY_MODE"] = "bootstrap-exit"

    result = subprocess.run(  # noqa: S603 -- copied repository script and safe literal run ID.
        [str(root / "scripts/capture-grove-mqtt-initial-requests.sh"), "mock-run"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "MQTT recorder bootstrap failed" in result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    commands = log.read_text(encoding="utf-8")
    assert "wait klove-mqtt-initial-request-mock-run" in commands
    assert "python - 172.30.0.3" not in commands
    assert "docker logs" not in commands
    assert not (root / ".klove-integration/grove-observation/mock-run").exists()


@pytest.mark.parametrize(
    ("stage", "retains_recovery_state"),
    [
        ("run", True),
        ("id-inspect", True),
        ("ip-inspect", False),
        ("readiness-inspect", False),
    ],
)
def test_capture_redacts_docker_failures_before_recorder_ready(
    tmp_path: Path, stage: str, retains_recovery_state: bool
) -> None:
    root = _copy_capture_tree(tmp_path)
    environment, log = _mock_environment(tmp_path)
    environment.update({"MOCK_PRE_READY_FAILURE": stage, "MOCK_READY_MODE": "delayed"})

    result = subprocess.run(  # noqa: S603 -- copied repository script and safe literal run ID.
        [str(root / "scripts/capture-grove-mqtt-initial-requests.sh"), "mock-run"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    combined = result.stdout + result.stderr
    state_dir = root / ".klove-integration/grove-observation/mock-run"
    assert result.returncode == 1
    assert result.stderr.count("MQTT recorder bootstrap failed") == 1
    assert "TEST0000" not in combined
    assert "Traceback" not in combined
    assert len(combined) < 1024
    commands = log.read_text(encoding="utf-8")
    assert "python - 172.30.0.3" not in commands
    assert "docker logs" not in commands
    if retains_recovery_state:
        assert (state_dir / "mqtt-initial-request-recorder-origin").is_file()
    else:
        assert not state_dir.exists()


@pytest.mark.parametrize("mode", ["secret", "extra"])
def test_capture_rejects_secret_or_extra_candidate_output(tmp_path: Path, mode: str) -> None:
    root = _copy_capture_tree(tmp_path)
    environment, _log = _mock_environment(tmp_path)
    environment["MOCK_CANDIDATE_MODE"] = mode

    result = subprocess.run(  # noqa: S603 -- copied repository script and safe literal run ID.
        [str(root / "scripts/capture-grove-mqtt-initial-requests.sh"), "mock-run"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert '"leak"' not in result.stdout
    assert '"extra"' not in result.stdout
    assert not (root / ".klove-integration/grove-observation/mock-run").exists()


@pytest.mark.parametrize("failure_variable", ["MOCK_DRIVER_FAILURE", "MOCK_RECORDER_FAILURE"])
def test_capture_surfaces_only_bounded_recorder_failure_code(
    tmp_path: Path, failure_variable: str
) -> None:
    root = _copy_capture_tree(tmp_path)
    environment, log = _mock_environment(tmp_path)
    environment[failure_variable] = "1"

    result = subprocess.run(  # noqa: S603 -- copied repository script and safe literal run ID.
        [str(root / "scripts/capture-grove-mqtt-initial-requests.sh"), "mock-run"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "MQTT recorder failed: timeout" in result.stderr
    assert "TEST0000" not in result.stdout + result.stderr
    assert "docker logs" not in log.read_text(encoding="utf-8")
    assert not (root / ".klove-integration/grove-observation/mock-run").exists()


@pytest.mark.parametrize("mode", ["hostile", "list-code", "non-ascii"])
def test_capture_rejects_hostile_recorder_failure_status(tmp_path: Path, mode: str) -> None:
    root = _copy_capture_tree(tmp_path)
    environment, log = _mock_environment(tmp_path)
    environment.update({"MOCK_DRIVER_FAILURE": "1", "MOCK_STATUS_MODE": mode})

    result = subprocess.run(  # noqa: S603 -- copied repository script and safe literal run ID.
        [str(root / "scripts/capture-grove-mqtt-initial-requests.sh"), "mock-run"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "MQTT recorder failure status is invalid" in result.stderr
    assert "TEST0000" not in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    assert len(result.stdout) + len(result.stderr) < 1024
    assert "docker logs" not in log.read_text(encoding="utf-8")
    assert not (root / ".klove-integration/grove-observation/mock-run").exists()


def test_recorder_identity_persistence_failure_retains_recovery_state(tmp_path: Path) -> None:
    root = _copy_capture_tree(tmp_path)
    environment, log = _mock_environment(tmp_path)
    environment["MOCK_DOCKER_EMPTY_RECORDER_ID"] = "1"

    result = subprocess.run(  # noqa: S603 -- copied repository script and safe literal run ID.
        [str(root / "scripts/capture-grove-mqtt-initial-requests.sh"), "mock-run"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    state_dir = root / ".klove-integration/grove-observation/mock-run"
    assert result.returncode == 1
    assert "retaining recovery state" in result.stderr
    assert (state_dir / "mqtt-initial-request-recorder-origin").is_file()
    assert "rm --force klove-mqtt-initial-request-mock-run" not in log.read_text(encoding="utf-8")
