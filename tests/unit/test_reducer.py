import json
from pathlib import Path
from typing import Any, cast

import pytest

from klove.domain.discovery import discover_capabilities
from klove.domain.models import PrinterPhase, initial_snapshot
from klove.domain.reducer import (
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
    assert not_ready.capabilities == CAPABILITIES
    assert not_ready.status == {} and not_ready.eventtime is None
    assert limited.reason == "missing_required_objects"
    assert limited.capabilities is not None and not limited.capabilities.dispatch_eligible
    assert offline.phase is PrinterPhase.OFFLINE and not offline.connected
    assert offline.reason == "moonraker_disconnected"
    assert offline.capabilities is None
    assert explicit_offline.reason == "klippy_disconnected"
