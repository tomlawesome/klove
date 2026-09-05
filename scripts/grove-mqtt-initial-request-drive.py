#!/usr/bin/env python3
"""Create one disposable Grove printer through its public API for observation."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE_URL = "http://127.0.0.1:8000"


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        return 2
    request = urllib.request.Request(  # noqa: S310 -- fixed loopback Grove public API.
        BASE_URL + "/api/v1/printers/",
        data=json.dumps(
            {
                "name": "MQTT Initial Request Observation",
                "serial_number": "MQTTOBS0000001",
                "ip_address": arguments[0],
                "model": "BL-P001",
                "auto_archive": False,
                "access_code": "TEST0000",
            }
        ).encode("ascii"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            body = response.read(64 * 1024)
    except urllib.error.HTTPError as error:
        print(f"drive failed: printer_create_http_{error.code}")
        return 1
    try:
        document = json.loads(body)
    except (TypeError, ValueError):
        print("drive failed: printer_create_response_invalid")
        return 1
    if not isinstance(document, dict) or type(document.get("id")) is not int:
        print("drive failed: printer_create_response_invalid")
        return 1
    print("public API accepted generated MQTT observation printer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
