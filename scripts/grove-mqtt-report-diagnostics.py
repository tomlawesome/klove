#!/usr/bin/env python3
"""Map a failed MQTT report observer's private status to one fixed host code."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

MAX_STATUS_BYTES = 256
STATUS_INVALID = "MQTT_REPORT_CAPTURE_RECORDER_STATUS_INVALID"
_FAILURE_CODES = frozenset(
    {"internal_failure", "protocol_failure", "timeout", "tls_failure", "transport_failure"}
)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _private_status_text(path: Path) -> str:
    """Read one owner-private regular status file through a no-follow descriptor."""
    try:
        parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_IMODE(parent.st_mode) != 0o700
            or parent.st_uid != os.geteuid()
        ):
            raise ValueError
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.geteuid()
                or not 0 < metadata.st_size <= MAX_STATUS_BYTES
            ):
                raise ValueError
            encoded = handle.read(MAX_STATUS_BYTES + 1)
            if len(encoded) != metadata.st_size:
                raise ValueError
        return encoded.decode("ascii")
    except (OSError, UnicodeDecodeError, ValueError):
        raise ValueError from None


def validate_recorder_status(path: Path, exit_status: int) -> str:
    """Return a fixed result only for an exact bounded recorder failure status."""
    if type(exit_status) is not int or not 1 <= exit_status <= 255:
        return STATUS_INVALID
    try:
        document = json.loads(
            _private_status_text(path),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, json.JSONDecodeError, RecursionError):
        return STATUS_INVALID
    if (
        type(document) is not dict
        or set(document) != {"status", "code"}
        or document.get("status") != "failure"
        or type(document.get("code")) is not str
        or document["code"] not in _FAILURE_CODES
    ):
        return STATUS_INVALID
    return f"MQTT_REPORT_CAPTURE_RECORDER_{document['code'].upper()}"


def main(arguments: list[str]) -> int:
    if len(arguments) != 2:
        return 2
    try:
        exit_status = int(arguments[1])
    except ValueError:
        return 2
    print(validate_recorder_status(Path(arguments[0]), exit_status))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
