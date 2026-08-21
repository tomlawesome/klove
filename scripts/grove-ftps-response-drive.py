#!/usr/bin/env python3
"""Drive one harmless generated archive through Grove's public runtime API."""

from __future__ import annotations

import io
import json
import sys
import time
import urllib.error
import urllib.request
import zipfile

BASE_URL = "http://127.0.0.1:8000"
BOUNDARY = "KLOVE-OBSERVATION-BOUNDARY"


def _request(
    path: str,
    *,
    operation: str,
    data: bytes | None = None,
    content_type: str | None = None,
    method: str | None = None,
) -> object:
    headers = {} if content_type is None else {"Content-Type": content_type}
    request = urllib.request.Request(  # noqa: S310 -- fixed loopback HTTP public API.
        BASE_URL + path,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 -- request URL is fixed to loopback HTTP.
            request, timeout=20
        ) as response:
            body = response.read(1024 * 1024)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"{operation}_http_{error.code}") from None
    if not body:
        return None
    return json.loads(body)


def _generated_archive() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _write_generated(
            archive,
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
            'relationships+xml"/>'
            '<Default Extension="model" ContentType="application/vnd.ms-package.'
            '3dmanufacturing-3dmodel+xml"/>'
            "</Types>",
        )
        _write_generated(
            archive,
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel-1" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
            "</Relationships>",
        )
        _write_generated(
            archive,
            "3D/3dmodel.model",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<model unit="millimeter" xml:lang="en-US" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            '<resources><object id="1" type="model"><mesh><vertices>'
            '<vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/>'
            '<vertex x="0" y="1" z="0"/></vertices><triangles>'
            '<triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources>'
            '<build><item objectid="1"/></build></model>',
        )
        _write_generated(
            archive,
            "Metadata/plate_1.gcode",
            "; generated observation archive\nG4 P1\n",
        )
    return output.getvalue()


def _write_generated(archive: zipfile.ZipFile, name: str, contents: str) -> None:
    """Write one generated member with byte-stable non-host metadata."""
    member = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
    member.compress_type = zipfile.ZIP_DEFLATED
    member.create_system = 3
    member.external_attr = 0o100600 << 16
    archive.writestr(member, contents.encode("utf-8"))


def _multipart(filename: str, contents: bytes) -> bytes:
    prefix = (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("ascii")
    return prefix + contents + f"\r\n--{BOUNDARY}--\r\n".encode("ascii")


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        return 2
    recorder_address = arguments[0]
    printer = _request(
        "/api/v1/printers/",
        operation="printer_create",
        data=json.dumps(
            {
                "name": "FTPS Response Observation",
                "serial_number": "FTPSOBS0000001",
                "ip_address": recorder_address,
                "model": "BL-P001",
                "auto_archive": False,
                "access_code": "TEST0000",
            }
        ).encode("ascii"),
        content_type="application/json",
    )
    if not isinstance(printer, dict) or type(printer.get("id")) is not int:
        raise RuntimeError("printer_create_response_invalid")
    archive_contents = _generated_archive()
    uploaded = _request(
        "/api/v1/archives/upload",
        operation="archive_upload",
        data=_multipart("observation.3mf", archive_contents),
        content_type=f"multipart/form-data; boundary={BOUNDARY}",
    )
    archive_contents = b""
    if not isinstance(uploaded, dict) or type(uploaded.get("id")) is not int:
        raise RuntimeError("archive_upload_response_invalid")
    queued = _request(
        "/api/v1/queue/",
        operation="queue_create",
        data=json.dumps(
            {
                "printer_id": printer["id"],
                "archive_id": uploaded["id"],
                "manual_start": False,
                "plate_id": 1,
                "use_ams": False,
                "bed_levelling": False,
                "vibration_cali": False,
                "nozzle_offset_cali": False,
            }
        ).encode("ascii"),
        content_type="application/json",
    )
    if not isinstance(queued, dict) or type(queued.get("id")) is not int:
        raise RuntimeError("queue_create_response_invalid")
    for _attempt in range(60):
        time.sleep(1)
        queue = _request("/api/v1/queue/", operation="queue_list")
        if isinstance(queue, list) and any(
            isinstance(item, dict)
            and item.get("printer_id") == printer["id"]
            and item.get("archive_id") == uploaded["id"]
            for item in queue
        ):
            print("public API accepted generated printer, archive, and queue item")
            return 0
    raise RuntimeError("queue_observation_timeout")


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"drive failed: {error}")
        raise SystemExit(1) from None
