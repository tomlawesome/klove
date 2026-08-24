from __future__ import annotations

import json
import math
from typing import Any, cast

import pytest

from klove.domain.discovery import discover_capabilities
from klove.domain.models import PrinterPhase, PrinterSnapshot, initial_snapshot
from klove.northbound.mqtt import reports
from klove.northbound.mqtt.reports import project_report

PRINTER = "11111111-1111-4111-8111-111111111111"
CAPABILITIES = discover_capabilities({"pause_resume", "print_stats", "virtual_sdcard"})


def snapshot(**updates: object) -> PrinterSnapshot:
    values: dict[str, object] = {
        "printer_id": PRINTER,
        "revision": 2,
        "control_revision": 1,
        "connected": True,
        "phase": PrinterPhase.IDLE,
        "reason": "observed",
        "eventtime": 12.5,
        "capabilities": CAPABILITIES,
        "job": None,
        "status": {
            "print_stats": {
                "state": "standby",
                "filename": "",
                "info": {"current_layer": 0, "total_layer": 0},
            },
            "virtual_sdcard": {"progress": 0.0, "is_active": False},
            "extruder": {"temperature": 21.5, "target": 0.0},
            "heater_bed": {"temperature": 20, "target": 0},
        },
    }
    values.update(updates)
    return PrinterSnapshot.model_construct(**cast(Any, values))


def document(payload: bytes) -> dict[str, object]:
    decoded = json.loads(payload)
    assert isinstance(decoded, dict)
    return decoded


def test_idle_report_projects_only_directly_supported_state_and_values() -> None:
    current = snapshot(
        status={
            "print_stats": {
                "state": "standby",
                "filename": "",
                "info": {"current_layer": 3, "total_layer": 12},
            },
            "virtual_sdcard": {"progress": 0.375},
            "extruder": {"temperature": 201.5, "target": 210},
            "heater_bed": {"temperature": 60, "target": 65.0},
        }
    )

    assert document(project_report(current, sequence_id="42")) == {
        "print": {
            "bed_target_temper": 65.0,
            "bed_temper": 60,
            "command": "push_status",
            "gcode_file": "",
            "gcode_state": "IDLE",
            "layer_num": 3,
            "mc_percent": 37,
            "nozzle_target_temper": 210,
            "nozzle_temper": 201.5,
            "sequence_id": "42",
            "total_layer_num": 12,
        }
    }


@pytest.mark.parametrize(
    "current",
    [
        initial_snapshot(PRINTER),
        snapshot(connected=False, phase=PrinterPhase.OFFLINE, reason="moonraker_disconnected"),
        snapshot(phase=PrinterPhase.NOT_READY, reason="klippy_shutdown", eventtime=None),
        snapshot(reason="stale"),
        snapshot(eventtime=None),
        snapshot(eventtime=math.nan),
        snapshot(eventtime=-1.0),
    ],
)
def test_unavailable_or_stale_snapshots_never_claim_idle(current: PrinterSnapshot) -> None:
    assert document(project_report(current)) == {"print": {"command": "push_status"}}


@pytest.mark.parametrize(
    "current",
    [
        snapshot(phase=PrinterPhase.PRINTING),
        snapshot(status={"print_stats": {"state": "printing", "filename": "job.gcode"}}),
        snapshot(status={"print_stats": {"state": "standby", "filename": "job.gcode"}}),
        snapshot(status={"print_stats": {"state": "unknown", "filename": ""}}),
        snapshot(status={"print_stats": []}),
        snapshot(status=[]),
    ],
)
def test_unobserved_or_contradictory_state_is_omitted(current: PrinterSnapshot) -> None:
    report = document(project_report(current))

    print_block = report["print"]
    assert isinstance(print_block, dict)
    assert print_block["command"] == "push_status"
    assert "gcode_state" not in print_block
    assert "gcode_file" not in print_block


def test_idle_state_without_filename_omits_unobserved_filename() -> None:
    report = document(project_report(snapshot(status={"print_stats": {"state": "standby"}})))

    assert report["print"] == {"command": "push_status", "gcode_state": "IDLE"}


def test_valid_telemetry_is_bounded_and_hostile_or_unsupported_values_are_omitted() -> None:
    current = snapshot(
        status={
            "print_stats": {
                "state": "standby",
                "filename": "",
                "info": {
                    "current_layer": True,
                    "total_layer": -1,
                },
            },
            "virtual_sdcard": {
                "progress": 2,
            },
            "extruder": {
                "temperature": True,
                "target": math.inf,
            },
            "heater_bed": {
                "temperature": "20",
                "target": 1_000_000_001,
            },
        }
    )

    assert document(project_report(current)) == {
        "print": {
            "command": "push_status",
            "gcode_file": "",
            "gcode_state": "IDLE",
        }
    }


@pytest.mark.parametrize(
    "sequence_id",
    [None, "", "abc", chr(0xFF11) + chr(0xFF12), "9" * 65, 1],
)
def test_invalid_sequence_ids_are_not_reflected(sequence_id: object) -> None:
    payload = project_report(snapshot(), sequence_id=cast(str | None, sequence_id))

    print_block = document(payload)["print"]
    assert isinstance(print_block, dict)
    assert "sequence_id" not in print_block


def test_non_string_snapshot_is_fail_closed() -> None:
    payload = project_report(cast(PrinterSnapshot, object()))

    assert document(payload) == {"print": {"command": "push_status"}}


def test_report_size_bound_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reports, "MAX_REPORT_BYTES", 1)

    with pytest.raises(ValueError, match="report_too_large"):
        project_report(snapshot())
