#!/usr/bin/env python3
"""Drive only Grove's public control endpoints in an isolated observation run."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE_URL = "http://127.0.0.1:8000"


MAX_READY_POLLS = 20
READY_POLL_SECONDS = 0.25


def _request(path: str, *, method: str = "POST", data: bytes | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(  # noqa: S310 -- fixed loopback Grove public API.
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            return response.status, response.read(64 * 1024)
    except urllib.error.HTTPError as error:
        return error.code, error.read(64 * 1024)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _connected_status(body: bytes, printer_id: int) -> bool:
    """Accept only directly observable public connection evidence for this printer."""
    try:
        document = json.loads(
            body,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return False
    return (
        type(document) is dict
        and document.get("id") == printer_id
        and type(document.get("id")) is int
        and document.get("connected") is True
    )


def _await_connected(printer_id: int) -> bool:
    """Require two fresh matching public status observations before control."""
    confirmations = 0
    for _ in range(MAX_READY_POLLS):
        status, body = _request(f"/api/v1/printers/{printer_id}/status", method="GET")
        if status == 200 and _connected_status(body, printer_id):
            confirmations += 1
            if confirmations == 2:
                return True
        else:
            confirmations = 0
        time.sleep(READY_POLL_SECONDS)
    return False


def main(arguments: list[str]) -> int:  # noqa: PLR0911 -- fixed non-reflecting failure exits.
    if len(arguments) != 1:
        return 2
    status, body = _request(
        "/api/v1/printers/",
        data=json.dumps(
            {
                "name": "MQTT Control Observation",
                "serial_number": "MQTTOBS0000001",
                "ip_address": arguments[0],
                "model": "BL-P001",
                "auto_archive": False,
                "access_code": "TEST0000",
            }
        ).encode("ascii"),
    )
    if status != 200:
        print("drive failed: printer_create")
        return 1
    try:
        document = json.loads(body)
    except (TypeError, ValueError):
        print("drive failed: printer_create_response")
        return 1
    printer_id = document.get("id") if isinstance(document, dict) else None
    if type(printer_id) is not int or printer_id < 1:
        print("drive failed: printer_create_response")
        return 1
    if not _await_connected(printer_id):
        print("drive failed: connected_printer_barrier")
        return 1
    for operation in ("pause", "resume", "stop", "pause"):
        operation_status, _operation_body = _request(
            f"/api/v1/printers/{printer_id}/print/{operation}"
        )
        if operation_status != 200:
            print("drive failed: control_endpoint")
            return 1
    print("public control endpoints invoked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
