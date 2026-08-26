#!/usr/bin/env python3
"""Bounded MQTT/TLS observer for sanitized Grove control-request evidence."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import platform
import re
import socket
import ssl
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, BinaryIO, cast

MAX_CONTROL_PACKETS = 32
CONTROL_WINDOW_SECONDS = 30.0
POST_CONTROL_QUIESCENCE_SECONDS = 5.0
PROBE_CLOSE_TIMEOUT_SECONDS = 10.0
LISTENER_ACCEPT_TIMEOUT_SECONDS = 45.0
MAX_TLS_SESSIONS = 2
EXPECTED_CONTROLS = ("pause", "resume", "stop")
CONTROL_FIELDS = frozenset({"command", "sequence_id"})
EVIDENCE_PATH = Path("/evidence/mqtt-control-request-schema")
PROVENANCE_PATH = Path("/evidence/mqtt-control-request-recorder-provenance")
STATUS_PATH = Path("/evidence/mqtt-control-request-status")
READY_PATH = Path("/evidence/mqtt-control-request-ready")
CERTIFICATE_PATH = "/tmp/observation-cert.pem"  # noqa: S108 -- private container tmpfs only.
PRIVATE_KEY_PATH = "/tmp/observation-key.pem"  # noqa: S108 -- private container tmpfs only.
PYTHON_VERSION = re.compile(r"3\.13\.(?:0|[1-9][0-9]{0,2})\Z")
OPENSSL_VERSION = re.compile(r"OpenSSL (3\.[0-9]+\.[0-9]+)(?: [0-9]{1,2} [A-Za-z]{3} [0-9]{4})?\Z")


class ObservationFailure(Exception):
    """A bounded recorder failure with no peer-controlled detail."""


def _initial() -> ModuleType:
    source = Path(
        os.environ.get(
            "KLOVE_INITIAL_RECORDER",
            Path(__file__).with_name("grove-mqtt-initial-request-recorder.py"),
        )
    )
    spec = importlib.util.spec_from_file_location("klove_initial_request_observer", source)
    if spec is None or spec.loader is None:
        raise ObservationFailure("initial_observer_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INITIAL = _initial()


def _json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result or not key or len(key) > 64 or any(ord(item) < 32 for item in key):
            raise ObservationFailure("json_member_invalid")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    raise ObservationFailure("payload_json_constant")


def _schema() -> dict[str, object]:
    return {
        "type": "object",
        "members": [
            {
                "name": "print",
                "present": True,
                "schema": {
                    "type": "object",
                    "members": [
                        {"name": "command", "present": True, "schema": {"type": "string"}},
                        {"name": "sequence_id", "present": True, "schema": {"type": "string"}},
                    ],
                },
            }
        ],
    }


def sanitize_control_payload(payload: bytes) -> tuple[str, bool, bytes]:
    """Accept only the independently whitelisted control envelope; retain no values."""
    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_json_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ObservationFailure("payload_json_invalid") from error
    if type(document) is not dict or set(document) != {"print"}:
        raise ObservationFailure("control_envelope_invalid")
    print_block = document["print"]
    if type(print_block) is not dict or set(print_block) != CONTROL_FIELDS:
        raise ObservationFailure("control_members_invalid")
    command = print_block.get("command")
    sequence = print_block.get("sequence_id")
    if (
        type(command) is not str
        or command not in EXPECTED_CONTROLS
        or type(sequence) is not str
        or not sequence.isascii()
        or not sequence.isdecimal()
        or not 1 <= len(sequence) <= 64
    ):
        raise ObservationFailure("control_values_invalid")
    return command, True, hashlib.sha256(payload).digest()


def parse_control_publish(
    header: int, body: bytes, serial: bytes
) -> tuple[dict[str, object], bytes, bytes]:
    """Parse one strict QoS-1 request publish without retaining identifiers or payloads."""
    if header >> 4 != 3 or header & 0x06 != 0x02 or header & 0x01:
        raise ObservationFailure("publish_flags_invalid")
    topic, cursor = INITIAL._mqtt_string(body, 0)
    if topic != INITIAL.REQUEST_TOPIC_PREFIX + serial + INITIAL.REQUEST_TOPIC_SUFFIX:
        raise ObservationFailure("publish_topic_invalid")
    if cursor + 2 >= len(body):
        raise ObservationFailure("publish_truncated")
    packet_id = body[cursor : cursor + 2]
    if packet_id == b"\0\0":
        raise ObservationFailure("publish_packet_id_invalid")
    command, sequence_marker, digest = sanitize_control_payload(body[cursor + 2 :])
    return (
        {
            "operation": command,
            "topic": "device/{serial}/request",
            "qos": 1,
            "dup": bool(header & 0x08),
            "retain": False,
            "members": _schema(),
            "generated_sequence_marker": sequence_marker,
            "payload_bytes": len(body) - cursor - 2,
        },
        packet_id,
        digest,
    )


def _write_all(connection: BinaryIO, payload: bytes, deadline: float | None = None) -> None:
    INITIAL._write_all(connection, payload, deadline=deadline)


def _observe_initial_session_inner(connection: BinaryIO) -> bytes:
    """Require one exact initial request handshake and return its bound serial."""
    header, body = INITIAL.read_packet(connection)
    if header != 0x10:
        raise ObservationFailure("connect_missing")
    serial = INITIAL._validate_connect(body)
    _write_all(connection, b"\x20\x02\0\0")
    expected_subscriptions = (b"report", b"request")
    subscriptions = 0
    requests: list[dict[str, object]] = []
    subscription_packet_ids: list[bytes] = []
    first_packet_id: bytes | None = None
    for _ in range(8):
        header, body = INITIAL.read_packet(connection)
        if header >> 4 == 8:
            if subscriptions >= len(expected_subscriptions) or header != 0x82:
                raise ObservationFailure("subscribe_unexpected")
            response, packet_id = INITIAL._subscribe_packet(
                body, serial, expected_subscriptions[subscriptions]
            )
            if packet_id in subscription_packet_ids:
                raise ObservationFailure("subscribe_packet_id_reused")
            _write_all(connection, response)
            subscription_packet_ids.append(packet_id)
            subscriptions += 1
            continue
        if header >> 4 != 3 or subscriptions != len(expected_subscriptions):
            raise ObservationFailure("initial_session_invalid")
        request, packet_id = INITIAL.parse_publish(header, body, serial)
        request_index = len(requests)
        expected = INITIAL.EXPECTED_COMMANDS[request_index] if request_index < 3 else None
        if (
            request["command"] != expected
            or request["payload_bytes"] != INITIAL.EXPECTED_PAYLOAD_BYTES[request_index]
        ):
            raise ObservationFailure("initial_sequence_invalid")
        requests.append(request)
        first_packet_id = INITIAL._ack_initial_publish(
            connection, requests, packet_id, first_packet_id
        )
        if len(requests) == len(INITIAL.EXPECTED_COMMANDS):
            return cast(bytes, serial)
    raise ObservationFailure("initial_requests_incomplete")


def _observe_initial_session(connection: BinaryIO) -> bytes:
    try:
        return _observe_initial_session_inner(connection)
    except INITIAL.ObservationFailure as error:
        raise ObservationFailure("initial_session_invalid") from error


def _wait_for_probe_close_inner(
    connection: BinaryIO,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Accept only a complete DISCONNECT or an immediate clean EOF from the probe."""
    deadline = clock() + PROBE_CLOSE_TIMEOUT_SECONDS
    INITIAL._prepare_deadline(connection, deadline, clock)
    header = connection.read(1)
    INITIAL._check_deadline(deadline, clock)
    if not header:
        return
    if len(header) != 1:
        raise ObservationFailure("probe_close_invalid")
    remaining = INITIAL.decode_remaining_length(
        lambda: INITIAL._read_exact(connection, 1, deadline=deadline, clock=clock)
    )
    body = INITIAL._read_exact(connection, remaining, deadline=deadline, clock=clock)
    if header[0] != 0xE0 or body:
        raise ObservationFailure("probe_close_invalid")


def _wait_for_probe_close(
    connection: BinaryIO,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    try:
        _wait_for_probe_close_inner(connection, clock=clock)
    except INITIAL.ObservationFailure as error:
        raise ObservationFailure("probe_close_invalid") from error


def _observe_controls(connection: BinaryIO, serial: bytes) -> dict[str, object]:  # noqa: PLR0912, PLR0915
    """Observe controls only after this session's independently complete handshake."""
    overall_deadline = time.monotonic() + CONTROL_WINDOW_SECONDS
    quiescence_deadline: float | None = None
    requests: list[dict[str, object]] = []
    seen: dict[str, tuple[bytes, bool]] = {}
    session_packet_ids: dict[bytes, tuple[str, bytes, bool]] = {}
    for _ in range(MAX_CONTROL_PACKETS):
        deadline = quiescence_deadline or overall_deadline
        try:
            header, body = INITIAL.read_packet(connection, deadline=deadline)
        except TimeoutError:
            if quiescence_deadline is not None:
                return {
                    "profile_version": 1,
                    "transport": "mqtt-over-tls",
                    "observed": {
                        "control_requests": requests,
                        "post_control_quiescence_observed": True,
                    },
                }
            raise
        packet_type = header >> 4
        if packet_type == 3:
            request, packet_id, digest = parse_control_publish(header, body, serial)
            operation = request["operation"]
            if not isinstance(operation, str):
                raise ObservationFailure("control_operation_invalid")
            packet_relation = session_packet_ids.get(packet_id)
            prior = seen.get(operation)
            if packet_relation is not None:
                prior_operation, prior_digest, prior_dup = packet_relation
                if (
                    request["dup"] is not True
                    or operation != prior_operation
                    or digest != prior_digest
                ):
                    raise ObservationFailure("session_packet_id_reuse")
                request["request_occurrence"] = "mqtt_dup_retransmission"
                request["prior_publish_dup"] = prior_dup
                request["matches_prior_publish"] = True
            elif request["dup"]:
                raise ObservationFailure("dup_without_prior_publish")
            elif prior is None:
                request["request_occurrence"] = "first_observed_publish"
                seen[operation] = (digest, bool(request["dup"]))
            else:
                prior_digest, prior_dup = prior
                request["request_occurrence"] = (
                    "separate_publish_same_payload"
                    if digest == prior_digest
                    else "separate_publish_different_payload"
                )
                request["prior_publish_dup"] = prior_dup
                request["matches_prior_publish"] = False
                seen[operation] = (digest, bool(request["dup"]))
            if packet_relation is None:
                session_packet_ids[packet_id] = (operation, digest, bool(request["dup"]))
            _write_all(connection, b"\x40\x02" + packet_id, deadline)
            request["puback_after_publish"] = True
            requests.append(request)
            if set(seen) == set(EXPECTED_CONTROLS) and quiescence_deadline is None:
                quiescence_deadline = time.monotonic() + POST_CONTROL_QUIESCENCE_SECONDS
            continue
        if header == 0xC0 and not body:
            _write_all(connection, b"\xd0\0", deadline)
            continue
        raise ObservationFailure("control_packet_invalid")
    raise ObservationFailure("control_requests_incomplete")


def observe_connection(connection: Any) -> dict[str, object]:
    """Observe one persistent session for unit-level protocol checks."""
    return _observe_controls(connection, _observe_initial_session(connection))


def observe_two_sessions(probe: BinaryIO, persistent: BinaryIO) -> dict[str, object]:
    """Require one clean probe session before a fresh persistent control session."""
    probe_serial = _observe_initial_session(probe)
    _wait_for_probe_close(probe)
    persistent_serial = _observe_initial_session(persistent)
    if persistent_serial != probe_serial:
        raise ObservationFailure("session_serial_mismatch")
    return _observe_controls(persistent, persistent_serial)


def _accept_before_deadline(
    listener: Any,
    deadline: float,
    clock: Callable[[], float],
) -> tuple[Any, Any]:
    remaining = deadline - clock()
    if remaining <= 0:
        raise TimeoutError
    listener.settimeout(remaining)
    accepted = cast(tuple[Any, Any], listener.accept())
    if clock() >= deadline:
        raise TimeoutError
    return accepted


def observe_two_tls_sessions(
    listener: Any,
    context: Any,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Accept exactly two bounded TLS sessions, closing the probe before persistent use."""
    deadline = clock() + LISTENER_ACCEPT_TIMEOUT_SECONDS
    probe_plain, _probe_peer = _accept_before_deadline(listener, deadline, clock)
    probe_plain.settimeout(PROBE_CLOSE_TIMEOUT_SECONDS)
    with context.wrap_socket(
        probe_plain,
        server_side=True,
        suppress_ragged_eofs=False,
    ) as probe:
        probe.settimeout(PROBE_CLOSE_TIMEOUT_SECONDS)
        probe_serial = _observe_initial_session(probe)
        _wait_for_probe_close(probe)
    persistent_plain, _persistent_peer = _accept_before_deadline(listener, deadline, clock)
    persistent_plain.settimeout(PROBE_CLOSE_TIMEOUT_SECONDS)
    with context.wrap_socket(
        persistent_plain,
        server_side=True,
        suppress_ragged_eofs=False,
    ) as persistent:
        persistent.settimeout(PROBE_CLOSE_TIMEOUT_SECONDS)
        persistent_serial = _observe_initial_session(persistent)
        if persistent_serial != probe_serial:
            raise ObservationFailure("session_serial_mismatch")
        return _observe_controls(persistent, persistent_serial)


def _require_exact_mapping(value: object, keys: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise ObservationFailure("candidate_shape_invalid")
    return cast(dict[str, object], value)


def parse_candidate_json(serialized: str) -> object:
    """Reject duplicate keys and non-finite values before candidate validation."""
    try:
        return json.loads(
            serialized,
            object_pairs_hook=_json_pairs,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ObservationFailure) as error:
        raise ObservationFailure("candidate_json_invalid") from error


def _matches_static_value(actual: object, expected: object) -> bool:
    if type(actual) is not type(expected):
        return False
    if type(expected) is dict:
        actual_mapping = cast(dict[object, object], actual)
        expected_mapping = cast(dict[object, object], expected)
        return set(actual_mapping) == set(expected_mapping) and all(
            _matches_static_value(actual_mapping[key], expected_mapping[key])
            for key in expected_mapping
        )
    if type(expected) is list:
        actual_list = cast(list[object], actual)
        expected_list = cast(list[object], expected)
        return len(actual_list) == len(expected_list) and all(
            _matches_static_value(left, right)
            for left, right in zip(actual_list, expected_list, strict=True)
        )
    return actual == expected


def validate_candidate(candidate: object) -> None:  # noqa: PLR0912 -- exact nested contract checks.
    """Fail closed unless every stdout candidate fact has the exact static schema."""
    document = _require_exact_mapping(
        candidate, frozenset({"profile_version", "transport", "observed"})
    )
    if (
        type(document["profile_version"]) is not int
        or document["profile_version"] != 1
        or document["transport"] != "mqtt-over-tls"
    ):
        raise ObservationFailure("candidate_header_invalid")
    observed = _require_exact_mapping(
        document["observed"],
        frozenset({"control_requests", "post_control_quiescence_observed"}),
    )
    requests = observed["control_requests"]
    if (
        type(requests) is not list
        or not 3 <= len(requests) <= MAX_CONTROL_PACKETS
        or observed["post_control_quiescence_observed"] is not True
    ):
        raise ObservationFailure("candidate_observation_invalid")
    first_operations: list[str] = []
    seen: set[str] = set()
    base_keys = frozenset(
        {
            "operation",
            "topic",
            "qos",
            "dup",
            "retain",
            "members",
            "generated_sequence_marker",
            "payload_bytes",
            "request_occurrence",
            "puback_after_publish",
        }
    )
    for request_value in requests:
        if type(request_value) is not dict:
            raise ObservationFailure("candidate_request_invalid")
        occurrence = request_value.get("request_occurrence")
        keys = (
            base_keys
            if occurrence == "first_observed_publish"
            else base_keys | {"prior_publish_dup", "matches_prior_publish"}
        )
        request = _require_exact_mapping(request_value, keys)
        operation = request["operation"]
        if (
            type(operation) is not str
            or operation not in EXPECTED_CONTROLS
            or request["topic"] != "device/{serial}/request"
            or type(request["qos"]) is not int
            or request["qos"] != 1
            or type(request["dup"]) is not bool
            or request["retain"] is not False
            or not _matches_static_value(request["members"], _schema())
            or request["generated_sequence_marker"] is not True
            or type(request["payload_bytes"]) is not int
            or not 1 <= request["payload_bytes"] <= 4096
            or request["puback_after_publish"] is not True
        ):
            raise ObservationFailure("candidate_request_invalid")
        if occurrence == "first_observed_publish":
            if operation in seen:
                raise ObservationFailure("candidate_order_invalid")
            seen.add(operation)
            first_operations.append(operation)
            continue
        if (
            type(request["prior_publish_dup"]) is not bool
            or type(request["matches_prior_publish"]) is not bool
        ):
            raise ObservationFailure("candidate_replay_invalid")
        if occurrence == "mqtt_dup_retransmission":
            if request["dup"] is not True or request["matches_prior_publish"] is not True:
                raise ObservationFailure("candidate_replay_invalid")
        elif occurrence in {"separate_publish_same_payload", "separate_publish_different_payload"}:
            if request["dup"] is not False or request["matches_prior_publish"] is not False:
                raise ObservationFailure("candidate_replay_invalid")
        else:
            raise ObservationFailure("candidate_replay_invalid")
    if (
        first_operations != list(EXPECTED_CONTROLS)
        or len(requests) != 4
        or requests[3]["operation"] != "pause"
        or requests[3]["request_occurrence"]
        not in {"separate_publish_same_payload", "separate_publish_different_payload"}
    ):
        raise ObservationFailure("candidate_order_invalid")


def _private_json(path: Path, document: Mapping[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        raise ObservationFailure("evidence_directory_not_owner_private")
    temporary = path.with_suffix(".tmp")
    if path.exists() or temporary.exists():
        raise ObservationFailure("evidence_path_already_exists")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(json.dumps(document, indent=2) + "\n")
    os.replace(temporary, path)


def _tool_versions() -> dict[str, str]:
    python = platform.python_version()
    openssl = OPENSSL_VERSION.fullmatch(ssl.OPENSSL_VERSION)
    if PYTHON_VERSION.fullmatch(python) is None or openssl is None:
        raise ObservationFailure("recorder_tool_version_invalid")
    return {"python": python, "openssl": f"OpenSSL {openssl.group(1)}"}


def _failure_code(error: Exception) -> str:
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ssl.SSLError):
        return "tls_failure"
    if isinstance(error, ObservationFailure):
        return "protocol_failure"
    if isinstance(error, OSError):
        return "transport_failure"
    return "internal_failure"


def main() -> int:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(CERTIFICATE_PATH, PRIVATE_KEY_PATH)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", 8883))  # noqa: S104 -- internal-only Docker network.
        listener.listen(MAX_TLS_SESSIONS)
        listener.settimeout(LISTENER_ACCEPT_TIMEOUT_SECONDS)
        _private_json(READY_PATH, {"status": "ready"})
        candidate = observe_two_tls_sessions(listener, context)
    _private_json(EVIDENCE_PATH, candidate)
    _private_json(PROVENANCE_PATH, _tool_versions())
    print("MQTT control request observation completed", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        with contextlib.suppress(Exception):
            _private_json(STATUS_PATH, {"status": "failure", "code": _failure_code(error)})
        print(f"observation failed: {_failure_code(error)}", flush=True)
        raise SystemExit(1) from None
