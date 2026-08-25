from __future__ import annotations

import json
import math
from typing import Any, cast

import pytest

from klove.domain.discovery import discover_capabilities
from klove.domain.models import PrinterPhase, PrinterSnapshot, initial_snapshot
from klove.northbound.mqtt import reports
from klove.northbound.mqtt.codec import encode_report
from klove.northbound.mqtt.reports import (
    ReportOperationResult,
    ReportOperationStatus,
    project_report,
)

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


@pytest.mark.parametrize(
    "phase", [phase for phase in PrinterPhase if phase is not PrinterPhase.IDLE]
)
def test_every_unobserved_phase_is_never_fabricated_as_idle(phase: PrinterPhase) -> None:
    current = snapshot(phase=phase)

    assert document(project_report(current)) == {"print": {"command": "push_status"}}


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


@pytest.mark.parametrize(
    "current",
    [
        snapshot(printer_id=""),
        snapshot(printer_id="not-a-uuid"),
        snapshot(printer_id=1),
        snapshot(printer_id="00000000-0000-0000-0000-000000000000"),
        snapshot(printer_id="AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
        snapshot(capabilities=None),
    ],
)
def test_missing_or_noncanonical_identity_never_claims_idle(current: PrinterSnapshot) -> None:
    assert document(project_report(current)) == {"print": {"command": "push_status"}}


@pytest.mark.parametrize(
    ("operation_result", "claims_idle"),
    [
        (None, True),
        (ReportOperationResult(ReportOperationStatus.CONFIRMED, "control_confirmed"), True),
        (ReportOperationResult(ReportOperationStatus.DENIED, "access_denied"), False),
        (
            ReportOperationResult(ReportOperationStatus.OUTCOME_UNKNOWN, "transport_ambiguous"),
            False,
        ),
        (cast(ReportOperationResult, object()), False),
    ],
)
def test_operation_result_cannot_turn_denial_or_uncertainty_into_success(
    operation_result: ReportOperationResult | None, claims_idle: bool
) -> None:
    print_block = document(project_report(snapshot(), operation_result))["print"]
    assert isinstance(print_block, dict)

    assert ("gcode_state" in print_block) is claims_idle


@pytest.mark.parametrize(
    "operation_result",
    [
        ReportOperationResult(ReportOperationStatus.CONFIRMED, ""),
        ReportOperationResult(ReportOperationStatus.CONFIRMED, "contains-dash"),
        ReportOperationResult(ReportOperationStatus.CONFIRMED, "A"),
        ReportOperationResult(ReportOperationStatus.CONFIRMED, "x" * 65),
        cast(ReportOperationResult, object()),
    ],
)
def test_unbounded_operation_result_is_omitted(operation_result: ReportOperationResult) -> None:
    assert document(project_report(snapshot(), operation_result)) == {
        "print": {"command": "push_status"}
    }


def test_non_string_snapshot_is_fail_closed() -> None:
    payload = project_report(cast(PrinterSnapshot, object()))

    assert document(payload) == {"print": {"command": "push_status"}}


def test_report_size_bound_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reports, "MAX_REPORT_BYTES", 1)

    with pytest.raises(ValueError, match="report_too_large"):
        project_report(snapshot())


def test_report_bytes_are_deterministic() -> None:
    current = snapshot(
        status={
            "print_stats": {"state": "standby", "filename": ""},
            "extruder": {"target": 210, "temperature": 201.5},
        }
    )

    first = project_report(current, sequence_id="42")
    second = project_report(current, sequence_id="42")

    assert first == second
    assert first == (
        b'{"print":{"command":"push_status","gcode_file":"","gcode_state":"IDLE",'
        b'"nozzle_target_temper":210,"nozzle_temper":201.5,"sequence_id":"42"}}'
    )


def test_report_payload_is_accepted_by_existing_encoder() -> None:
    payload = project_report(snapshot())

    wire = encode_report("KLOVE-11111111-1111-4111-8111-111111111111", payload)

    assert wire.endswith(payload)
