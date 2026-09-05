#!/usr/bin/env python3
"""Drive only Grove's public control endpoints in an isolated observation run."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable

BASE_URL = "http://127.0.0.1:8000"


CONNECTION_WINDOW_SECONDS = 20.0
READY_POLL_SECONDS = 0.25
REQUEST_TIMEOUT_SECONDS = 20.0


def _request(
    path: str,
    *,
    method: str = "POST",
    data: bytes | None = None,
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
) -> tuple[int, bytes]:
    request = urllib.request.Request(  # noqa: S310 -- fixed loopback Grove public API.
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
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


def _await_connected(
    printer_id: int,
    *,
    clock: Callable[[], float] | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> bool:
    """Require two fresh matching public status observations before control."""
    now = time.monotonic if clock is None else clock
    sleep = time.sleep if sleeper is None else sleeper
    confirmations = 0
    deadline = now() + CONNECTION_WINDOW_SECONDS
    while now() < deadline:
        remaining = min(REQUEST_TIMEOUT_SECONDS, deadline - now())
        if remaining <= 0:
            return False
        status, body = _request(
            f"/api/v1/printers/{printer_id}/status",
            method="GET",
            timeout_seconds=remaining,
        )
        if now() >= deadline:
            return False
        if status == 200 and _connected_status(body, printer_id):
            confirmations += 1
            if confirmations == 2:
                return True
        else:
            confirmations = 0
        remaining = deadline - now()
        if remaining > 0:
            sleep(min(READY_POLL_SECONDS, remaining))
    return False


def _failed(stage: str) -> int:
    print(f"driver failed: {stage}")
    return 1


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
        return _failed("printer_create")
    try:
        document = json.loads(body)
    except (TypeError, ValueError):
        return _failed("printer_create_response")
    printer_id = document.get("id") if isinstance(document, dict) else None
    if type(printer_id) is not int or printer_id < 1:
        return _failed("printer_create_response")
    if not _await_connected(printer_id):
        return _failed("connected_printer_barrier")
    for index, operation in enumerate(("pause", "resume", "stop", "pause"), start=1):
        operation_status, _operation_body = _request(
            f"/api/v1/printers/{printer_id}/print/{operation}"
        )
        if operation_status != 200:
            return _failed("control_pause_repeat" if index == 4 else f"control_{operation}")
    print("driver complete: controls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
