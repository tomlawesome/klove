#!/usr/bin/env python3
"""Bounded client-side observer for generated Grove non-idle MQTT reports.

This is observation-only.  It drives Grove's documented virtual-printer
project-file path with generated data and retains structure, not payload values.
"""

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
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import BinaryIO, cast

MAX_REPORT_PACKETS = 48
MAX_PRE_SUBACK_REPORTS = 8
MAX_POST_FINISH_PACKETS = 16
MAX_JSON_DEPTH = 12
MAX_JSON_MEMBERS = 128
CONNECT_TIMEOUT_SECONDS = 15.0
REPORT_WINDOW_SECONDS = 15.0
POST_FINISH_QUIESCENCE_SECONDS = 5.0
EVIDENCE_PATH = Path("/evidence/mqtt-report-schema")
PROVENANCE_PATH = Path("/evidence/mqtt-report-recorder-provenance")
STATUS_PATH = Path("/evidence/mqtt-report-status")
READY_PATH = Path("/evidence/mqtt-report-ready")
PYTHON_VERSION = re.compile(r"3\.13\.(?:0|[1-9][0-9]{0,2})\Z")
OPENSSL_VERSION = re.compile(r"OpenSSL (3\.[0-9]+\.[0-9]+)(?: [0-9]{1,2} [A-Za-z]{3} [0-9]{4})?\Z")
FAILURE_CODES = frozenset({"internal_failure", "timeout", "tls_failure", "transport_failure"})
PROTOCOL_CODES = frozenset(
    {
        "candidate_header_invalid",
        "candidate_lifecycle_invalid",
        "candidate_reports_invalid",
        "candidate_request_invalid",
        "candidate_result_invalid",
        "candidate_shape_invalid",
        "connack_invalid",
        "evidence_directory_not_owner_private",
        "evidence_path_already_exists",
        "initial_observer_unavailable",
        "json_member_invalid",
        "mqtt_string_invalid",
        "packet_invalid",
        "payload_depth_invalid",
        "payload_json_constant",
        "payload_members_invalid",
        "payload_type_invalid",
        "post_ack_prepare_invalid",
        "post_finish_packet_invalid",
        "post_finish_packet_limit",
        "post_finish_report",
        "post_prepare_finish_invalid",
        "pre_suback_report_limit",
        "project_result_invalid",
        "recorder_tool_version_invalid",
        "report_command_invalid",
        "report_envelope_invalid",
        "report_json_invalid",
        "report_publish_flags_invalid",
        "report_replay",
        "report_sequence_incomplete",
        "report_topic_invalid",
        "serial_invalid",
        "session_invalid",
        "stale_pre_ack_report",
        "subscribe_invalid",
        "subscribe_response_invalid",
    }
)


class ObservationFailure(Exception):
    """A fixed-code failure which deliberately excludes peer-controlled text."""

    def __init__(self, code: str) -> None:
        if code not in PROTOCOL_CODES:
            raise ValueError("observation_failure_code_invalid")
        self.code = code
        super().__init__(code)


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


def _write_all(connection: BinaryIO, payload: bytes, deadline: float | None = None) -> None:
    INITIAL._write_all(connection, payload, deadline=deadline)


def _packet(header: int, body: bytes) -> bytes:
    return bytes((header,)) + cast(bytes, INITIAL._mqtt_remaining_length(len(body))) + body


def _string(value: bytes) -> bytes:
    if not value or len(value) > 512:
        raise ObservationFailure("mqtt_string_invalid")
    return len(value).to_bytes(2, "big") + value


def _validate_serial(serial: bytes) -> None:
    try:
        INITIAL._validate_serial(serial)
    except INITIAL.ObservationFailure as error:
        raise ObservationFailure("serial_invalid") from error


def _connect_packet(serial: bytes, session: int) -> bytes:
    if not 1 <= session <= 2:
        raise ObservationFailure("session_invalid")
    _validate_serial(serial)
    client_id = b"bambuddy_" + serial + b"_999_" + str(session).encode("ascii")
    body = (
        _string(b"MQTT")
        + bytes((4, 0xC2))
        + (30).to_bytes(2, "big")
        + _string(client_id)
        + _string(b"bblp")
        + _string(b"TEST0000")
    )
    return _packet(0x10, body)


def _subscribe_packet(serial: bytes, suffix: bytes, packet_id: int) -> bytes:
    if suffix not in {b"report", b"request"} or not 1 <= packet_id <= 65535:
        raise ObservationFailure("subscribe_invalid")
    return _packet(
        0x82,
        packet_id.to_bytes(2, "big") + _string(b"device/" + serial + b"/" + suffix) + b"\0",
    )


def _project_packet(serial: bytes) -> tuple[bytes, bytes]:
    sequence = "70001"
    payload = json.dumps(
        {
            "print": {
                "command": "project_file",
                "sequence_id": sequence,
                "subtask_name": "observation",
                "file": "observation.3mf",
            }
        },
        separators=(",", ":"),
    ).encode("ascii")
    return (
        _packet(0x30, _string(b"device/" + serial + b"/request") + payload),
        sequence.encode("ascii"),
    )


def _schema(value: object, depth: int = 0) -> dict[str, object]:  # noqa: PLR0911
    if depth > MAX_JSON_DEPTH:
        raise ObservationFailure("payload_depth_invalid")
    if type(value) is dict:
        mapping = cast(dict[str, object], value)
        if len(mapping) > MAX_JSON_MEMBERS:
            raise ObservationFailure("payload_members_invalid")
        return {
            "type": "object",
            "members": [
                {"name": key, "present": True, "schema": _schema(item, depth + 1)}
                for key, item in mapping.items()
            ],
        }
    if type(value) is list:
        if len(value) > MAX_JSON_MEMBERS:
            raise ObservationFailure("payload_members_invalid")
        return {"type": "array", "items": [_schema(item, depth + 1) for item in value]}
    if type(value) is str:
        return {"type": "string"}
    if type(value) is bool:
        return {"type": "boolean"}
    if type(value) is int:
        return {"type": "integer"}
    if type(value) is float:
        return {"type": "number"}
    if value is None:
        return {"type": "null"}
    raise ObservationFailure("payload_type_invalid")


def _parse_report_publish(
    header: int, body: bytes, serial: bytes
) -> tuple[dict[str, object], dict[str, object]]:
    """Accept a strictly QoS-0 report and return its schema-only representation."""
    if header != 0x30:
        raise ObservationFailure("report_publish_flags_invalid")
    try:
        topic, cursor = INITIAL._mqtt_string(body, 0)
    except INITIAL.ObservationFailure as error:
        raise ObservationFailure("report_topic_invalid") from error
    if topic != b"device/" + serial + b"/report" or cursor >= len(body):
        raise ObservationFailure("report_topic_invalid")
    try:
        document = json.loads(
            body[cursor:].decode("utf-8"),
            object_pairs_hook=_json_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ObservationFailure("report_json_invalid") from error
    if (
        type(document) is not dict
        or set(document) != {"print"}
        or type(document["print"]) is not dict
    ):
        raise ObservationFailure("report_envelope_invalid")
    print_block = cast(dict[str, object], document["print"])
    command = print_block.get("command")
    if type(command) is not str:
        raise ObservationFailure("report_command_invalid")
    return (
        {
            "topic": "device/{serial}/report",
            "qos": 0,
            "dup": False,
            "retain": False,
            "members": _schema(document),
        },
        print_block,
    )


def _read_packet(connection: BinaryIO, deadline: float) -> tuple[int, bytes]:
    try:
        return cast(tuple[int, bytes], INITIAL.read_packet(connection, deadline=deadline))
    except INITIAL.ObservationFailure as error:
        raise ObservationFailure("packet_invalid") from error


def _remember_report(
    header: int, body: bytes, serial: bytes, seen_digests: set[bytes]
) -> tuple[dict[str, object], dict[str, object]]:
    profile, print_block = _parse_report_publish(header, body, serial)
    digest = hashlib.sha256(body).digest()
    if digest in seen_digests:
        raise ObservationFailure("report_replay")
    seen_digests.add(digest)
    return profile, print_block


def _subscribe(  # noqa: PLR0913, PLR0917 -- exact MQTT subscribe binding.
    connection: BinaryIO,
    serial: bytes,
    suffix: bytes,
    packet_id: int,
    deadline: float,
    seen_digests: set[bytes],
    report_budget: int,
    observed_reports: list[tuple[dict[str, object], dict[str, object]]],
) -> int:
    _write_all(connection, _subscribe_packet(serial, suffix, packet_id), deadline)
    reports = 0
    while True:
        header, body = _read_packet(connection, deadline)
        if header == 0x90 and body == packet_id.to_bytes(2, "big") + b"\0":
            return reports
        if header == 0x30:
            reports += 1
            if reports > report_budget:
                raise ObservationFailure("pre_suback_report_limit")
            observed_reports.append(_remember_report(header, body, serial, seen_digests))
            continue
        raise ObservationFailure("subscribe_response_invalid")


def _open_session(
    host: str, serial: bytes, session: int
) -> tuple[ssl.SSLSocket, set[bytes], list[tuple[dict[str, object], dict[str, object]]]]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    plain = socket.create_connection((host, 8883), timeout=CONNECT_TIMEOUT_SECONDS)
    try:
        connection = context.wrap_socket(plain, server_hostname=None)
        deadline = time.monotonic() + CONNECT_TIMEOUT_SECONDS
        typed_connection = cast(BinaryIO, connection)
        seen_digests: set[bytes] = set()
        observed_reports: list[tuple[dict[str, object], dict[str, object]]] = []
        _write_all(typed_connection, _connect_packet(serial, session), deadline)
        header, body = _read_packet(typed_connection, deadline)
        if header != 0x20 or body != b"\0\0":
            raise ObservationFailure("connack_invalid")
        first_count = _subscribe(
            typed_connection,
            serial,
            b"report",
            1,
            deadline,
            seen_digests,
            MAX_PRE_SUBACK_REPORTS,
            observed_reports,
        )
        _subscribe(
            typed_connection,
            serial,
            b"request",
            2,
            deadline,
            seen_digests,
            MAX_PRE_SUBACK_REPORTS - first_count,
            observed_reports,
        )
        return connection, seen_digests, observed_reports
    except Exception:
        plain.close()
        raise


def _close_cleanly(connection: ssl.SSLSocket) -> None:
    try:
        _write_all(
            cast(BinaryIO, connection), b"\xe0\0", time.monotonic() + CONNECT_TIMEOUT_SECONDS
        )
    finally:
        connection.close()


def _reject_stale_pre_ack(report: dict[str, object]) -> None:
    command = report.get("command")
    state = report.get("gcode_state")
    if command == "project_file" or (command == "push_status" and state in {"PREPARE", "FINISH"}):
        raise ObservationFailure("stale_pre_ack_report")


def _report(profile: dict[str, object], *, command: str, state: str) -> dict[str, object]:
    return {**profile, "command": command, "state": state}


def _quiet_after_finish(connection: BinaryIO, serial: bytes, seen_digests: set[bytes]) -> None:
    deadline = time.monotonic() + POST_FINISH_QUIESCENCE_SECONDS
    for _ in range(MAX_POST_FINISH_PACKETS):
        try:
            header, body = _read_packet(connection, deadline)
        except TimeoutError:
            return
        if header == 0xC0 and not body:
            _write_all(connection, b"\xd0\0", deadline)
            continue
        if header != 0x30:
            raise ObservationFailure("post_finish_packet_invalid")
        _remember_report(header, body, serial, seen_digests)
        raise ObservationFailure("post_finish_report")
    raise ObservationFailure("post_finish_packet_limit")


def observe_two_sessions(host: str, serial: bytes) -> dict[str, object]:
    """Prove an exact acknowledgement/status chain on a fresh persistent session."""
    probe, _probe_digests, _probe_reports = _open_session(host, serial, 1)
    _close_cleanly(probe)
    persistent, seen_digests, pre_ack_reports = _open_session(host, serial, 2)
    try:
        for _profile, report in pre_ack_reports:
            _reject_stale_pre_ack(report)
        command_packet, sequence = _project_packet(serial)
        typed_persistent = cast(BinaryIO, persistent)
        _write_all(typed_persistent, command_packet, time.monotonic() + CONNECT_TIMEOUT_SECONDS)
        deadline = time.monotonic() + REPORT_WINDOW_SECONDS
        acknowledgement: dict[str, object] | None = None
        prepare: dict[str, object] | None = None
        finish: dict[str, object] | None = None
        for _ in range(MAX_REPORT_PACKETS):
            header, body = _read_packet(typed_persistent, deadline)
            if header == 0xC0 and not body:
                _write_all(typed_persistent, b"\xd0\0", deadline)
                continue
            profile, print_block = _remember_report(header, body, serial, seen_digests)
            command = print_block.get("command")
            state = print_block.get("gcode_state")
            if acknowledgement is None:
                if command == "push_status" and state in {"PREPARE", "FINISH"}:
                    raise ObservationFailure("stale_pre_ack_report")
                if (
                    command != "project_file"
                    or print_block.get("result") != "SUCCESS"
                    or print_block.get("sequence_id") != sequence.decode("ascii")
                    or state != "PREPARE"
                ):
                    raise ObservationFailure("project_result_invalid")
                acknowledgement = {
                    **_report(profile, command="project_file", state="PREPARE"),
                    "result": "SUCCESS",
                    "matches_generated_request_sequence": True,
                }
                continue
            if prepare is None:
                if command != "push_status" or state != "PREPARE":
                    raise ObservationFailure("post_ack_prepare_invalid")
                prepare = _report(profile, command="push_status", state="PREPARE")
                continue
            if finish is None:
                if command != "push_status" or state != "FINISH":
                    raise ObservationFailure("post_prepare_finish_invalid")
                finish = _report(profile, command="push_status", state="FINISH")
                _quiet_after_finish(typed_persistent, serial, seen_digests)
                return {
                    "profile_version": 1,
                    "transport": "mqtt-over-tls",
                    "observed": {
                        "two_session_lifecycle": {
                            "probe_clean_disconnect": True,
                            "persistent_fresh_connection": True,
                        },
                        "project_file_request": {
                            "topic": "device/{serial}/request",
                            "qos": 0,
                            "dup": False,
                            "retain": False,
                            "generated_sequence_marker": True,
                        },
                        "project_file_result": acknowledgement,
                        "non_idle_push_status": [prepare, finish],
                        "persistent_session_qos0_no_replay_or_retain": True,
                    },
                }
        raise ObservationFailure("report_sequence_incomplete")
    finally:
        _close_cleanly(persistent)


def _exact_mapping(value: object, keys: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise ObservationFailure("candidate_shape_invalid")
    return cast(dict[str, object], value)


def _valid_schema(value: object, depth: int = 0) -> bool:
    if depth > MAX_JSON_DEPTH or type(value) is not dict:
        return False
    schema = cast(dict[str, object], value)
    kind = schema.get("type")
    if kind in {"string", "boolean", "integer", "number", "null"}:
        return set(schema) == {"type"}
    if kind == "array":
        items = schema.get("items")
        return (
            set(schema) == {"type", "items"}
            and type(items) is list
            and len(items) <= MAX_JSON_MEMBERS
            and all(_valid_schema(item, depth + 1) for item in items)
        )
    if kind == "object":
        members = schema.get("members")
        if set(schema) != {"type", "members"} or type(members) is not list:
            return False
        return len(members) <= MAX_JSON_MEMBERS and all(
            type(member) is dict
            and set(member) == {"name", "present", "schema"}
            and type(member["name"]) is str
            and bool(member["name"])
            and member["present"] is True
            and _valid_schema(member["schema"], depth + 1)
            for member in members
        )
    return False


def _valid_report(value: object, *, command: str, state: str) -> bool:
    report = _exact_mapping(
        value,
        frozenset({"topic", "qos", "dup", "retain", "members", "command", "state"}),
    )
    return (
        report["topic"] == "device/{serial}/report"
        and report["qos"] == 0
        and report["dup"] is False
        and report["retain"] is False
        and report["command"] == command
        and report["state"] == state
        and _valid_schema(report["members"])
    )


def validate_candidate(candidate: object) -> None:
    """Require the exact static candidate before an owner can review it."""
    document = _exact_mapping(candidate, frozenset({"profile_version", "transport", "observed"}))
    if document["profile_version"] != 1 or document["transport"] != "mqtt-over-tls":
        raise ObservationFailure("candidate_header_invalid")
    observed = _exact_mapping(
        document["observed"],
        frozenset(
            {
                "two_session_lifecycle",
                "project_file_request",
                "project_file_result",
                "non_idle_push_status",
                "persistent_session_qos0_no_replay_or_retain",
            }
        ),
    )
    if observed["two_session_lifecycle"] != {
        "probe_clean_disconnect": True,
        "persistent_fresh_connection": True,
    }:
        raise ObservationFailure("candidate_lifecycle_invalid")
    if observed["project_file_request"] != {
        "topic": "device/{serial}/request",
        "qos": 0,
        "dup": False,
        "retain": False,
        "generated_sequence_marker": True,
    }:
        raise ObservationFailure("candidate_request_invalid")
    result = _exact_mapping(
        observed["project_file_result"],
        frozenset(
            {
                "topic",
                "qos",
                "dup",
                "retain",
                "members",
                "command",
                "result",
                "matches_generated_request_sequence",
                "state",
            }
        ),
    )
    result_report = {
        key: result[key] for key in ("topic", "qos", "dup", "retain", "members", "command", "state")
    }
    if (
        not _valid_report(
            result_report,
            command="project_file",
            state="PREPARE",
        )
        or result["result"] != "SUCCESS"
        or result["matches_generated_request_sequence"] is not True
    ):
        raise ObservationFailure("candidate_result_invalid")
    statuses = observed["non_idle_push_status"]
    if (
        type(statuses) is not list
        or len(statuses) != 2
        or not _valid_report(statuses[0], command="push_status", state="PREPARE")
        or not _valid_report(statuses[1], command="push_status", state="FINISH")
        or observed["persistent_session_qos0_no_replay_or_retain"] is not True
    ):
        raise ObservationFailure("candidate_reports_invalid")


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
        handle.flush()
        os.fsync(handle.fileno())
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
    if isinstance(error, OSError):
        return "transport_failure"
    return "internal_failure"


def _failure_status(error: Exception) -> dict[str, str]:
    if isinstance(error, ObservationFailure):
        return {"status": "failure", "code": error.code}
    code = _failure_code(error)
    return {"status": "failure", "code": code if code in FAILURE_CODES else "internal_failure"}


def main(arguments: list[str]) -> int:
    if len(arguments) != 2:
        return 2
    host, serial_text = arguments
    serial = serial_text.encode("ascii")
    _validate_serial(serial)
    _private_json(READY_PATH, {"status": "ready"})
    candidate = observe_two_sessions(host, serial)
    _private_json(EVIDENCE_PATH, candidate)
    _private_json(PROVENANCE_PATH, _tool_versions())
    print("MQTT report observation completed", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as error:
        status = _failure_status(error)
        with contextlib.suppress(Exception):
            _private_json(STATUS_PATH, status)
        print(f"observation failed: {status['code']}", flush=True)
        raise SystemExit(1) from None
