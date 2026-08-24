"""Pure, conservative projection of canonical state into one MQTT report."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Final

from klove.domain.models import PrinterPhase, PrinterSnapshot
from klove.northbound.mqtt.codec import MAX_REPORT_BYTES

MAX_SEQUENCE_ID_LENGTH: Final = 64
_REPORT_COMMAND: Final = "push_status"
_MAX_REPORT_NUMBER: Final = 1_000_000_000


def project_report(snapshot: PrinterSnapshot, *, sequence_id: str | None = None) -> bytes:
    """Return one bounded report without opening a listener or adding authority.

    Only the observed idle report state is emitted.  Other phases and every
    unsupported Bambu field are deliberately omitted until independently
    observed and reviewed.
    """
    report: dict[str, object] = {"command": _REPORT_COMMAND}
    if type(snapshot) is PrinterSnapshot and _usable_snapshot(snapshot):
        _project_state(snapshot, report)
        _project_measurements(snapshot.status, report)
    if _valid_sequence_id(sequence_id):
        report["sequence_id"] = sequence_id
    return _encode(report)


def _usable_snapshot(snapshot: PrinterSnapshot) -> bool:
    eventtime = snapshot.eventtime
    return (
        snapshot.connected is True
        and snapshot.reason == "observed"
        and eventtime is not None
        and not isinstance(eventtime, bool)
        and math.isfinite(eventtime)
        and eventtime >= 0
    )


def _project_state(snapshot: PrinterSnapshot, report: dict[str, object]) -> None:
    if snapshot.phase is not PrinterPhase.IDLE:
        return
    print_stats = _object(snapshot.status, "print_stats")
    if print_stats is None or print_stats.get("state") != "standby":
        return
    filename = print_stats.get("filename")
    if filename is not None and filename != "":
        return
    report["gcode_state"] = "IDLE"
    if filename == "":
        report["gcode_file"] = ""


def _project_measurements(status: Mapping[str, object], report: dict[str, object]) -> None:
    for source, target in (
        (("extruder", "temperature"), "nozzle_temper"),
        (("extruder", "target"), "nozzle_target_temper"),
        (("heater_bed", "temperature"), "bed_temper"),
        (("heater_bed", "target"), "bed_target_temper"),
    ):
        value = _number(_field(status, source[0], source[1]))
        if value is not None:
            report[target] = value

    progress = _number(_field(status, "virtual_sdcard", "progress"))
    if progress is not None and 0 <= progress <= 1:
        report["mc_percent"] = int(progress * 100)

    print_stats = _object(status, "print_stats")
    info = None if print_stats is None else _object(print_stats, "info")
    if info is None:
        return
    for field_name, target in (
        ("current_layer", "layer_num"),
        ("total_layer", "total_layer_num"),
    ):
        value = _integer(info.get(field_name))
        if value is not None:
            report[target] = value


def _field(status: Mapping[str, object], object_name: str, field_name: str) -> object:
    values = _object(status, object_name)
    return None if values is None else values.get(field_name)


def _object(value: object, key: str) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    selected = value.get(key)
    return dict(selected) if isinstance(selected, Mapping) else None


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or abs(value) > _MAX_REPORT_NUMBER:
        return None
    return value


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= _MAX_REPORT_NUMBER else None


def _valid_sequence_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.isascii()
        and value.isdecimal()
        and 1 <= len(value) <= MAX_SEQUENCE_ID_LENGTH
    )


def _encode(report: dict[str, object]) -> bytes:
    payload = json.dumps(
        {"print": report},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    if len(payload) > MAX_REPORT_BYTES:
        raise ValueError("report_too_large")
    return payload
