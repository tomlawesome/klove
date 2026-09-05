#!/usr/bin/env python3
"""Validate bounded private control-capture diagnostics without exposing capture data."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

MAX_STATUS_BYTES = 256
DRIVER_SUCCESS = "MQTT_CONTROL_CAPTURE_DRIVER_COMPLETE"
DRIVER_INVALID = "MQTT_CONTROL_CAPTURE_DRIVER_STATUS_INVALID"
RECORDER_INVALID = "MQTT_CONTROL_CAPTURE_RECORDER_STATUS_INVALID"
RECORDER_MISSING = "MQTT_CONTROL_CAPTURE_RECORDER_STATUS_MISSING"
_DRIVER_FAILURES = {
    "driver failed: printer_create\n": "MQTT_CONTROL_CAPTURE_DRIVER_PRINTER_CREATE",
    "driver failed: printer_create_response\n": (
        "MQTT_CONTROL_CAPTURE_DRIVER_PRINTER_CREATE_RESPONSE"
    ),
    "driver failed: connected_printer_barrier\n": "MQTT_CONTROL_CAPTURE_DRIVER_CONNECTED_BARRIER",
    "driver failed: control_pause\n": "MQTT_CONTROL_CAPTURE_DRIVER_PAUSE",
    "driver failed: control_resume\n": "MQTT_CONTROL_CAPTURE_DRIVER_RESUME",
    "driver failed: control_stop\n": "MQTT_CONTROL_CAPTURE_DRIVER_STOP",
    "driver failed: control_pause_repeat\n": "MQTT_CONTROL_CAPTURE_DRIVER_PAUSE_REPEAT",
}
_RECORDER_FAILURES = {
    "internal_failure": "MQTT_CONTROL_CAPTURE_RECORDER_INTERNAL_FAILURE",
    "protocol_failure": "MQTT_CONTROL_CAPTURE_RECORDER_PROTOCOL_FAILURE",
    "timeout": "MQTT_CONTROL_CAPTURE_RECORDER_TIMEOUT",
    "tls_failure": "MQTT_CONTROL_CAPTURE_RECORDER_TLS_FAILURE",
    "transport_failure": "MQTT_CONTROL_CAPTURE_RECORDER_TRANSPORT_FAILURE",
}


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _private_text(path: Path) -> str:
    try:
        metadata = path.stat()
        if (
            not path.is_file()
            or path.is_symlink()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or not 0 < metadata.st_size <= MAX_STATUS_BYTES
        ):
            raise ValueError
        return path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError, ValueError):
        raise ValueError from None


def validate_driver_status(path: Path, exit_status: int) -> str:
    """Return one fixed host code for an exact, bounded driver result."""
    if type(exit_status) is not int or exit_status < 0 or exit_status > 255:
        return DRIVER_INVALID
    try:
        output = _private_text(path)
    except ValueError:
        return DRIVER_INVALID
    if output == "driver complete: controls\n":
        return DRIVER_SUCCESS if exit_status == 0 else DRIVER_INVALID
    if output in _DRIVER_FAILURES:
        return _DRIVER_FAILURES[output] if exit_status != 0 else DRIVER_INVALID
    return DRIVER_INVALID


def validate_recorder_status(path: Path, exit_status: int) -> str:
    """Return one fixed host code for a failed recorder's private status file."""
    if type(exit_status) is not int or exit_status == 0 or exit_status > 255:
        return RECORDER_INVALID
    try:
        document = json.loads(
            _private_text(path),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, json.JSONDecodeError, RecursionError):
        return RECORDER_MISSING if not path.exists() and not path.is_symlink() else RECORDER_INVALID
    if type(document) is not dict or set(document) != {"status", "code"}:
        return RECORDER_INVALID
    if document.get("status") != "failure" or type(document.get("code")) is not str:
        return RECORDER_INVALID
    return _RECORDER_FAILURES.get(document["code"], RECORDER_INVALID)


def main(arguments: list[str]) -> int:
    if len(arguments) != 3 or arguments[0] not in {"driver", "recorder"}:
        return 2
    try:
        exit_status = int(arguments[2])
    except ValueError:
        return 2
    result = (
        validate_driver_status(Path(arguments[1]), exit_status)
        if arguments[0] == "driver"
        else validate_recorder_status(Path(arguments[1]), exit_status)
    )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
