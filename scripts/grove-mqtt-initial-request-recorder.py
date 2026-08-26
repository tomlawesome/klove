#!/usr/bin/env python3
"""Bounded MQTT/TLS recorder for one sanitized initial-request observation."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import ssl
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

MAX_PACKET_BYTES = 16 * 1024
MAX_TOPIC_BYTES = 512
MAX_JSON_DEPTH = 12
MAX_JSON_MEMBERS = 64
EXPECTED_COMMANDS = ("pushall", "get_version", "extrusion_cali_get")
EXPECTED_PAYLOAD_BYTES = (35, 56, 109)
REQUEST_TOPIC_PREFIX = b"device/"
REQUEST_TOPIC_SUFFIX = b"/request"
SERIAL_ALPHABET = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
OBSERVATION_SERIAL = b"MQTTOBS0000001"
CONNECT_FLAGS = 0xC2
CONNECT_KEEPALIVE_SECONDS = 30
CONNECT_USERNAME = b"bblp"
OBSERVATION_ACCESS_CODE = b"TEST0000"
EVIDENCE_PATH = Path("/evidence/mqtt-initial-request-schema")
STATUS_PATH = Path("/evidence/mqtt-initial-request-status")
READY_PATH = Path("/evidence/mqtt-initial-request-ready")
FAILURE_CODES = frozenset(
    {"internal_failure", "protocol_failure", "timeout", "tls_failure", "transport_failure"}
)
POST_CAPTURE_TIMEOUT_SECONDS = 10.0
MAX_POST_CAPTURE_PACKETS = 32
CERTIFICATE_PATH = "/tmp/observation-cert.pem"  # noqa: S108 -- private container tmpfs only.
PRIVATE_KEY_PATH = "/tmp/observation-key.pem"  # noqa: S108 -- private container tmpfs only.


class ObservationFailure(Exception):
    """A bounded recorder failure that never includes peer-controlled text."""


def _prepare_deadline(
    connection: BinaryIO,
    deadline: float | None,
    clock: Callable[[], float],
) -> None:
    if deadline is None:
        return
    remaining = deadline - clock()
    if remaining <= 0:
        raise TimeoutError
    timeout_setter = getattr(connection, "settimeout", None)
    if callable(timeout_setter):
        timeout_setter(remaining)


def _check_deadline(deadline: float | None, clock: Callable[[], float]) -> None:
    if deadline is not None and clock() >= deadline:
        raise TimeoutError


def _read_exact(
    connection: BinaryIO,
    size: int,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> bytes:
    output = bytearray()
    while len(output) < size:
        _prepare_deadline(connection, deadline, clock)
        chunk = connection.read(size - len(output))
        _check_deadline(deadline, clock)
        if not chunk:
            raise ObservationFailure("packet_truncated")
        output.extend(chunk)
    return bytes(output)


def decode_remaining_length(read_byte: Callable[[], bytes]) -> int:
    """Decode an MQTT remaining length before allocating the packet body."""
    multiplier = 1
    remaining = 0
    for index in range(4):
        encoded = read_byte()
        if len(encoded) != 1:
            raise ObservationFailure("packet_truncated")
        value = encoded[0]
        remaining += (value & 127) * multiplier
        if remaining > MAX_PACKET_BYTES:
            raise ObservationFailure("packet_oversize")
        if value & 128 == 0:
            minimum_bytes = 1
            canonical_remaining = remaining
            while canonical_remaining >= 128:
                canonical_remaining //= 128
                minimum_bytes += 1
            if index + 1 != minimum_bytes:
                raise ObservationFailure("remaining_length_nonminimal")
            return remaining
        multiplier *= 128
    raise ObservationFailure("remaining_length_invalid")


def read_packet(
    connection: BinaryIO,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[int, bytes]:
    header = _read_exact(connection, 1, deadline=deadline, clock=clock)[0]
    remaining = decode_remaining_length(
        lambda: _read_exact(connection, 1, deadline=deadline, clock=clock)
    )
    return header, _read_exact(connection, remaining, deadline=deadline, clock=clock)


def _write_all(
    connection: BinaryIO,
    payload: bytes,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    cursor = 0
    while cursor < len(payload):
        _prepare_deadline(connection, deadline, clock)
        progress = connection.write(payload[cursor:])
        _check_deadline(deadline, clock)
        if type(progress) is not int or progress <= 0 or progress > len(payload) - cursor:
            raise ObservationFailure("write_progress_invalid")
        cursor += progress


def _mqtt_remaining_length(size: int) -> bytes:
    encoded = bytearray()
    while True:
        digit = size % 128
        size //= 128
        if size:
            digit |= 128
        encoded.append(digit)
        if not size:
            return bytes(encoded)


def _mqtt_string(body: bytes, cursor: int) -> tuple[bytes, int]:
    if cursor + 2 > len(body):
        raise ObservationFailure("mqtt_string_truncated")
    size = int.from_bytes(body[cursor : cursor + 2], "big")
    cursor += 2
    if size > MAX_TOPIC_BYTES or cursor + size > len(body):
        raise ObservationFailure("mqtt_string_invalid")
    return body[cursor : cursor + size], cursor + size


def _validate_serial(serial: bytes) -> None:
    if not serial or any(character not in SERIAL_ALPHABET for character in serial):
        raise ObservationFailure("serial_invalid")


def _validate_connect(body: bytes) -> bytes:
    protocol, cursor = _mqtt_string(body, 0)
    if protocol != b"MQTT" or cursor + 4 > len(body) or body[cursor] != 4:
        raise ObservationFailure("connect_protocol_invalid")
    flags = body[cursor + 1]
    keepalive = int.from_bytes(body[cursor + 2 : cursor + 4], "big")
    if flags != CONNECT_FLAGS or keepalive != CONNECT_KEEPALIVE_SECONDS:
        raise ObservationFailure("connect_flags_invalid")
    cursor += 4
    client_id, cursor = _mqtt_string(body, cursor)
    if not client_id.startswith(b"bambuddy_"):
        raise ObservationFailure("client_id_invalid")
    client_parts = client_id[9:].split(b"_")
    if len(client_parts) != 3:
        raise ObservationFailure("client_id_invalid")
    serial, printer_id, session_counter = client_parts
    _validate_serial(serial)
    if serial != OBSERVATION_SERIAL:
        raise ObservationFailure("connect_serial_invalid")
    if (
        not printer_id
        or not session_counter
        or not all(48 <= item <= 57 for item in printer_id)
        or not all(48 <= item <= 57 for item in session_counter)
    ):
        raise ObservationFailure("client_id_invalid")
    username, cursor = _mqtt_string(body, cursor)
    password, cursor = _mqtt_string(body, cursor)
    if username != CONNECT_USERNAME or password != OBSERVATION_ACCESS_CODE:
        raise ObservationFailure("connect_credentials_invalid")
    if cursor != len(body):
        raise ObservationFailure("connect_trailing_bytes")
    return serial


def _json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ObservationFailure("json_duplicate_member")
        if not key or len(key) > 64 or any(ord(character) < 32 for character in key):
            raise ObservationFailure("json_member_invalid")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    raise ObservationFailure("payload_json_constant")


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) is str:
        return "string"
    if type(value) in (int, float):
        return "number"
    if type(value) is list:
        return "array"
    if type(value) is dict:
        return "object"
    raise ObservationFailure("json_value_invalid")


def sanitize_payload(payload: bytes) -> tuple[dict[str, object], str, bool]:
    """Return schema-only facts; values are never retained in candidate evidence."""
    try:
        decoded = payload.decode("utf-8")
        document = json.loads(
            decoded,
            object_pairs_hook=_json_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ObservationFailure("payload_json_invalid") from error
    if type(document) is not dict:
        raise ObservationFailure("payload_root_invalid")

    command_values: list[str] = []
    generated_sequence = False

    def schema(value: object, depth: int) -> dict[str, object]:
        nonlocal generated_sequence
        if depth > MAX_JSON_DEPTH:
            raise ObservationFailure("payload_too_deep")
        kind = _json_type(value)
        if type(value) is dict:
            if len(value) > MAX_JSON_MEMBERS:
                raise ObservationFailure("payload_too_wide")
            members: list[dict[str, object]] = []
            for name, member in value.items():
                if name == "command":
                    if type(member) is not str or member not in EXPECTED_COMMANDS:
                        raise ObservationFailure("command_invalid")
                    command_values.append(member)
                if name == "sequence_id" and type(member) is str and member.isdecimal():
                    generated_sequence = True
                members.append({"name": name, "present": True, "schema": schema(member, depth + 1)})
            return {"type": kind, "members": members}
        if type(value) is list:
            if len(value) > MAX_JSON_MEMBERS:
                raise ObservationFailure("payload_too_wide")
            return {"type": kind, "items": [schema(item, depth + 1) for item in value]}
        return {"type": kind}

    result = schema(document, 0)
    if len(command_values) != 1:
        raise ObservationFailure("command_missing_or_ambiguous")
    return result, command_values[0], generated_sequence


def parse_publish(header: int, body: bytes, serial: bytes) -> tuple[dict[str, object], bytes]:
    """Parse one exact QoS-1 request publish without retaining its packet ID."""
    if header >> 4 != 3 or header & 0x0F != 0x02:
        raise ObservationFailure("publish_flags_invalid")
    topic, cursor = _mqtt_string(body, 0)
    if topic != REQUEST_TOPIC_PREFIX + serial + REQUEST_TOPIC_SUFFIX:
        raise ObservationFailure("publish_topic_invalid")
    if cursor + 2 >= len(body):
        raise ObservationFailure("publish_truncated")
    packet_id = body[cursor : cursor + 2]
    if packet_id == b"\0\0":
        raise ObservationFailure("publish_packet_id_invalid")
    payload = body[cursor + 2 :]
    schema, command, generated_sequence = sanitize_payload(payload)
    return (
        {
            "topic": "device/{serial}/request",
            "qos": 1,
            "dup": False,
            "retain": False,
            "command": command,
            "members": schema,
            "generated_sequence_marker": generated_sequence,
            "payload_bytes": len(payload),
            "puback_order": "after_publish",
        },
        packet_id,
    )


def _subscribe_packet(body: bytes, serial: bytes, suffix: bytes) -> tuple[bytes, bytes]:
    if len(body) < 5:
        raise ObservationFailure("subscribe_invalid")
    packet_id = body[:2]
    if packet_id == b"\0\0":
        raise ObservationFailure("subscribe_packet_id_invalid")
    cursor = 2
    topic, cursor = _mqtt_string(body, cursor)
    if cursor == len(body) or body[cursor] != 0:
        raise ObservationFailure("subscribe_invalid")
    if topic != REQUEST_TOPIC_PREFIX + serial + b"/" + suffix:
        raise ObservationFailure("subscribe_topic_invalid")
    cursor += 1
    if cursor != len(body):
        raise ObservationFailure("subscribe_extra_filter")
    return b"\x90\x03" + packet_id + b"\0", packet_id


def _ack_initial_publish(
    connection: BinaryIO,
    requests: list[dict[str, object]],
    packet_id: bytes,
    first_packet_id: bytes | None,
) -> bytes:
    if len(requests) == 1:
        return packet_id
    if len(requests) == 2:
        if first_packet_id is None or packet_id == first_packet_id:
            raise ObservationFailure("inflight_packet_id_reused")
        requests[0]["puback_order"] = "after_next_initial_publish"
        _write_all(connection, b"\x40\x02" + first_packet_id)
    _write_all(connection, b"\x40\x02" + packet_id)
    if first_packet_id is None:
        raise ObservationFailure("first_packet_id_missing")
    return first_packet_id


def _wait_for_clean_disconnect(
    connection: BinaryIO,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    deadline = clock() + POST_CAPTURE_TIMEOUT_SECONDS
    for _ in range(MAX_POST_CAPTURE_PACKETS):
        header, body = read_packet(connection, deadline=deadline, clock=clock)
        if header == 0xE0 and not body:
            return
        if header == 0xC0 and not body:
            _write_all(connection, b"\xd0\0", deadline=deadline, clock=clock)
            continue
        raise ObservationFailure("post_capture_packet_invalid")
    raise ObservationFailure("post_capture_packet_limit")


def observe_connection(connection: BinaryIO) -> dict[str, object]:
    """Consume one bounded MQTT session and return only the sanitized candidate."""
    header, body = read_packet(connection)
    if header != 0x10:
        raise ObservationFailure("connect_missing")
    serial = _validate_connect(body)
    _write_all(connection, b"\x20\x02\0\0")
    requests: list[dict[str, object]] = []
    subscription_packet_ids: list[bytes] = []
    expected_subscription_suffixes = (b"report", b"request")
    first_packet_id: bytes | None = None
    packet_limit = 8
    for _ in range(packet_limit):
        header, body = read_packet(connection)
        packet_type = header >> 4
        if packet_type == 8:
            if len(subscription_packet_ids) >= 2 or header != 0x82:
                raise ObservationFailure("subscribe_unexpected")
            response, packet_id = _subscribe_packet(
                body,
                serial,
                expected_subscription_suffixes[len(subscription_packet_ids)],
            )
            if packet_id in subscription_packet_ids:
                raise ObservationFailure("subscribe_packet_id_reused")
            _write_all(connection, response)
            subscription_packet_ids.append(packet_id)
            continue
        if packet_type == 3:
            if len(subscription_packet_ids) != 2:
                raise ObservationFailure("publish_before_subscribe")
            request, packet_id = parse_publish(header, body, serial)
            expected = EXPECTED_COMMANDS[len(requests)] if len(requests) < 3 else None
            if request["command"] != expected:
                raise ObservationFailure("publish_sequence_invalid")
            requests.append(request)
            first_packet_id = _ack_initial_publish(connection, requests, packet_id, first_packet_id)
            if len(requests) == len(EXPECTED_COMMANDS):
                candidate = {
                    "profile_version": 1,
                    "transport": "mqtt-over-tls",
                    "observed": {
                        "initial_requests": requests,
                        "first_puback_after_next_initial_publish": True,
                    },
                }
                _wait_for_clean_disconnect(connection)
                return candidate
            continue
        raise ObservationFailure("packet_unexpected")
    raise ObservationFailure("initial_requests_incomplete")


def _tls_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(CERTIFICATE_PATH, PRIVATE_KEY_PATH)
    return context


def _write_private_json(output: Path, document: dict[str, object]) -> None:
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output.parent.stat().st_mode & 0o077:
        raise ObservationFailure("evidence_directory_not_owner_private")
    temporary = output.with_suffix(".tmp")
    if output.exists() or temporary.exists():
        raise ObservationFailure("evidence_path_already_exists")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(json.dumps(document, indent=2) + "\n")
    os.replace(temporary, output)


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


def _write_failure_status(code: str) -> None:
    if code not in FAILURE_CODES:
        raise ObservationFailure("failure_code_invalid")
    _write_private_json(STATUS_PATH, {"status": "failure", "code": code})


def _write_ready_marker() -> None:
    _write_private_json(READY_PATH, {"status": "ready"})


def main() -> int:
    output = Path(os.environ.get("KLOVE_MQTT_EVIDENCE", EVIDENCE_PATH))
    context = _tls_context()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", 8883))  # noqa: S104 -- internal-only Docker network.
        listener.listen(1)
        listener.settimeout(45)
        _write_ready_marker()
        plain, _peer = listener.accept()
        plain.settimeout(10)
        with context.wrap_socket(plain, server_side=True) as protected:
            protected.settimeout(10)
            candidate = observe_connection(protected)
    _write_private_json(output, candidate)
    print("MQTT initial request observation completed", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        code = _failure_code(error)
        with contextlib.suppress(Exception):
            _write_failure_status(code)
        print(f"observation failed: {code}", flush=True)
        raise SystemExit(1) from None
