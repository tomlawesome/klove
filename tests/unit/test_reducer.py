import json
from pathlib import Path
from typing import Any, cast

import pytest

from klove.domain.discovery import discover_capabilities
from klove.domain.models import (
    JobHistoryStatus,
    JobIdentitySnapshot,
    PrinterPhase,
    initial_snapshot,
)
from klove.domain.reducer import (
    apply_history_job,
    apply_status_update,
    establish_snapshot,
    mark_capability_limited,
    mark_disconnected,
    mark_not_ready,
)
from klove.errors import StateEvidenceError

FIXTURE = Path(__file__).parents[1] / "fixtures" / "moonraker" / "ready-subscription.json"
OBJECTS = frozenset({"pause_resume", "print_stats", "virtual_sdcard"})
CAPABILITIES = discover_capabilities(OBJECTS)
SERVER_READY = {"klippy_connected": True, "klippy_state": "ready"}
PRINTER_READY = {"state": "ready"}


def subscription(state: str = "standby") -> dict[str, Any]:
    result = cast(dict[str, Any], json.loads(FIXTURE.read_text(encoding="utf-8")))
    result["status"]["print_stats"]["state"] = state
    result["status"]["pause_resume"]["is_paused"] = state == "paused"
    result["status"]["virtual_sdcard"]["is_active"] = state == "printing"
    return result


@pytest.mark.parametrize(
    ("remote", "phase"),
    [
        ("standby", PrinterPhase.IDLE),
        ("printing", PrinterPhase.PRINTING),
        ("paused", PrinterPhase.PAUSED),
        ("complete", PrinterPhase.COMPLETED),
        ("cancelled", PrinterPhase.CANCELLED),
        ("error", PrinterPhase.ERROR),
    ],
)
def test_complete_ready_evidence_maps_known_states(remote: str, phase: PrinterPhase) -> None:
    result = establish_snapshot(
        initial_snapshot("voron"),
        server_info=SERVER_READY,
        printer_info=PRINTER_READY,
        capabilities=CAPABILITIES,
        subscription=subscription(remote),
    )

    assert result.connected
    assert result.phase is phase
    assert result.revision == 1
    assert result.control_revision == 1
    assert result.reason == "observed"


def test_status_diff_is_shallow_merged_only_for_known_objects() -> None:
    baseline = establish_snapshot(
        initial_snapshot("voron"),
        server_info=SERVER_READY,
        printer_info=PRINTER_READY,
        capabilities=CAPABILITIES,
        subscription=subscription(),
    )
    result = apply_status_update(
        baseline,
        status_diff={
            "pause_resume": {"is_paused": False},
            "print_stats": {"state": "printing", "filename": "safe.gcode"},
            "virtual_sdcard": {"is_active": True},
        },
        eventtime=101,
    )

    assert result.phase is PrinterPhase.PRINTING
    assert result.status["print_stats"]["filename"] == "safe.gcode"
    assert result.status["print_stats"]["message"] == ""
    assert result.revision == 2
    assert result.control_revision == 2


def test_forward_progress_and_telemetry_do_not_rotate_the_control_token() -> None:
    initial = subscription("printing")
    initial["status"]["print_stats"].update(
        {"filename": "safe.gcode", "print_duration": 10.0, "total_duration": 12.0}
    )
    initial["status"]["virtual_sdcard"].update({"file_position": 100, "progress": 0.1})
    baseline = establish_snapshot(
        initial_snapshot("voron"),
        server_info=SERVER_READY,
        printer_info=PRINTER_READY,
        capabilities=CAPABILITIES,
        subscription=initial,
    )
    advanced = apply_status_update(
        baseline,
        status_diff={
            "print_stats": {"message": "working", "print_duration": 11.0},
            "virtual_sdcard": {"file_position": 110, "progress": 0.11},
        },
        eventtime=101,
    )

    assert advanced.revision == baseline.revision + 1
    assert advanced.control_revision == baseline.control_revision
    assert advanced.state_token == baseline.state_token


def test_phase_filename_and_position_regression_rotate_the_control_token() -> None:
    initial = subscription("printing")
    initial["status"]["print_stats"]["filename"] = "safe.gcode"
    initial["status"]["virtual_sdcard"]["file_position"] = 100
    baseline = establish_snapshot(
        initial_snapshot("voron"),
        server_info=SERVER_READY,
        printer_info=PRINTER_READY,
        capabilities=CAPABILITIES,
        subscription=initial,
    )
    renamed = apply_status_update(
        baseline,
        status_diff={"print_stats": {"filename": "other.gcode"}},
        eventtime=101,
    )
    regressed = apply_status_update(
        renamed,
        status_diff={"virtual_sdcard": {"file_position": 50}},
        eventtime=102,
    )
    paused = apply_status_update(
        regressed,
        status_diff={
            "pause_resume": {"is_paused": True},
            "print_stats": {"state": "paused"},
            "virtual_sdcard": {"is_active": False},
        },
        eventtime=103,
    )

    assert renamed.control_revision == baseline.control_revision + 1
    assert regressed.control_revision == renamed.control_revision + 1
    assert paused.control_revision == regressed.control_revision + 1
    tokens = {
        baseline.state_token,
        renamed.state_token,
        regressed.state_token,
        paused.state_token,
    }
    assert len(tokens) == 4


@pytest.mark.parametrize(
    ("previous_position", "current_position", "rotates"),
    [
        (None, None, False),
        (True, 1, True),
        (1.5, 1, True),
        (-1, 1, True),
        (1, False, True),
        (1, 1.5, True),
        (1, -1, True),
    ],
)
def test_file_position_validity_changes_rotate_control_evidence(
    previous_position: object,
    current_position: object,
    rotates: bool,
) -> None:
    initial = subscription("printing")
    initial["status"]["print_stats"]["filename"] = "safe.gcode"
    initial["status"]["virtual_sdcard"]["file_position"] = previous_position
    baseline = establish_snapshot(
        initial_snapshot("voron"),
        server_info=SERVER_READY,
        printer_info=PRINTER_READY,
        capabilities=CAPABILITIES,
        subscription=initial,
    )
    updated = apply_status_update(
        baseline,
        status_diff={"virtual_sdcard": {"file_position": current_position}},
        eventtime=101,
    )

    assert (updated.control_revision != baseline.control_revision) is rotates


@pytest.mark.parametrize(
    ("server", "printer"),
    [
        ({"klippy_connected": False, "klippy_state": "ready"}, PRINTER_READY),
        ({"klippy_connected": True, "klippy_state": "startup"}, PRINTER_READY),
        (SERVER_READY, {"state": "shutdown"}),
    ],
)
def test_not_ready_or_contradictory_bootstrap_is_rejected(
    server: dict[str, object], printer: dict[str, object]
) -> None:
    with pytest.raises(StateEvidenceError):
        establish_snapshot(
            initial_snapshot("voron"),
            server_info=server,
            printer_info=printer,
            capabilities=CAPABILITIES,
            subscription=subscription(),
        )


@pytest.mark.parametrize(
    "bad_subscription",
    [
        {"eventtime": 1, "status": {}, "extra": True},
        {"eventtime": "1", "status": {}},
        {"eventtime": True, "status": {}},
        {"eventtime": -1, "status": {}},
        {"eventtime": float("inf"), "status": {}},
        {"eventtime": 1, "status": []},
        {"eventtime": 1, "status": {"unknown": {}}},
        {"eventtime": 1, "status": {"print_stats": []}},
        {"eventtime": 1, "status": {}},
        {
            "eventtime": 1,
            "status": {
                "pause_resume": {"is_paused": False},
                "print_stats": {"state": 1},
                "virtual_sdcard": {},
            },
        },
        {
            "eventtime": 1,
            "status": {
                "pause_resume": {"is_paused": False},
                "print_stats": {"state": "printing"},
                "virtual_sdcard": {"is_active": False},
            },
        },
        {
            "eventtime": 1,
            "status": {
                "pause_resume": {"is_paused": False},
                "print_stats": {"state": "mystery"},
                "virtual_sdcard": {},
            },
        },
        {
            "eventtime": 1,
            "status": {
                "pause_resume": {},
                "print_stats": {"state": "standby"},
                "virtual_sdcard": {},
            },
        },
        {
            "eventtime": 1,
            "status": {
                "pause_resume": {"is_paused": True},
                "print_stats": {"state": "standby"},
                "virtual_sdcard": {},
            },
        },
    ],
)
def test_malformed_bootstrap_evidence_is_rejected(bad_subscription: dict[str, object]) -> None:
    with pytest.raises(StateEvidenceError):
        establish_snapshot(
            initial_snapshot("voron"),
            server_info=SERVER_READY,
            printer_info=PRINTER_READY,
            capabilities=CAPABILITIES,
            subscription=bad_subscription,
        )


def test_status_update_requires_a_complete_newer_baseline() -> None:
    with pytest.raises(StateEvidenceError, match="complete baseline"):
        apply_status_update(initial_snapshot("voron"), status_diff={}, eventtime=1)

    baseline = establish_snapshot(
        initial_snapshot("voron"),
        server_info=SERVER_READY,
        printer_info=PRINTER_READY,
        capabilities=CAPABILITIES,
        subscription=subscription(),
    )
    with pytest.raises(StateEvidenceError, match="stale or duplicated"):
        apply_status_update(baseline, status_diff={}, eventtime=100.5)
    with pytest.raises(StateEvidenceError, match="unknown object"):
        apply_status_update(baseline, status_diff={"intruder": {}}, eventtime=101)


def test_history_identity_updates_rotate_only_for_a_different_job_observation() -> None:
    baseline = establish_snapshot(
        initial_snapshot("voron"),
        server_info=SERVER_READY,
        printer_info=PRINTER_READY,
        capabilities=CAPABILITIES,
        subscription=subscription("printing"),
    )
    active = JobIdentitySnapshot(
        job_id="000001",
        filename="job.gcode",
        start_time=1_700_000_000.0,
        status=JobHistoryStatus.IN_PROGRESS,
    )
    identified = apply_history_job(baseline, active)
    duplicate = apply_history_job(identified, active)
    finished = apply_history_job(
        duplicate, active.model_copy(update={"status": JobHistoryStatus.COMPLETED})
    )

    assert identified.control_revision == baseline.control_revision + 1
    assert duplicate.revision == identified.revision + 1
    assert duplicate.control_revision == identified.control_revision
    assert duplicate.state_token == identified.state_token
    assert finished.control_revision == duplicate.control_revision + 1
    assert finished.state_token != duplicate.state_token


def test_history_identity_requires_a_complete_baseline() -> None:
    with pytest.raises(StateEvidenceError, match="complete baseline"):
        apply_history_job(
            initial_snapshot("voron"),
            JobIdentitySnapshot(
                job_id="000001",
                filename="job.gcode",
                start_time=1_700_000_000.0,
                status=JobHistoryStatus.IN_PROGRESS,
            ),
        )


def test_invalidation_helpers_remove_volatile_evidence() -> None:
    baseline = establish_snapshot(
        initial_snapshot("voron"),
        server_info=SERVER_READY,
        printer_info=PRINTER_READY,
        capabilities=CAPABILITIES,
        subscription=subscription(),
    )
    not_ready = mark_not_ready(baseline, "klippy_shutdown")
    limited = mark_capability_limited(
        not_ready, discover_capabilities(["print_stats", "virtual_sdcard"])
    )
    offline = mark_disconnected(limited)
    explicit_offline = mark_disconnected(limited, "klippy_disconnected")

    assert not_ready.connected and not_ready.phase is PrinterPhase.NOT_READY
    assert not_ready.control_revision == baseline.control_revision + 1
    assert not_ready.capabilities == CAPABILITIES
    assert not_ready.status == {} and not_ready.eventtime is None
    assert limited.reason == "missing_required_objects"
    assert limited.control_revision == not_ready.control_revision + 1
    assert limited.capabilities is not None and not limited.capabilities.dispatch_eligible
    assert offline.phase is PrinterPhase.OFFLINE and not offline.connected
    assert offline.control_revision == limited.control_revision + 1
    assert offline.reason == "moonraker_disconnected"
    assert offline.capabilities is None
    assert explicit_offline.reason == "klippy_disconnected"
