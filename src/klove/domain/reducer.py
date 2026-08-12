"""Fail-closed reduction of Moonraker evidence into canonical state."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from klove.domain.models import CapabilitySnapshot, PrinterPhase, PrinterSnapshot
from klove.errors import StateEvidenceError

_PRINT_PHASES = {
    "standby": PrinterPhase.IDLE,
    "printing": PrinterPhase.PRINTING,
    "paused": PrinterPhase.PAUSED,
    "complete": PrinterPhase.COMPLETED,
    "cancelled": PrinterPhase.CANCELLED,
    "error": PrinterPhase.ERROR,
}
_REQUIRED_STATUS_OBJECTS = frozenset({"pause_resume", "print_stats", "virtual_sdcard"})


def establish_snapshot(
    previous: PrinterSnapshot,
    *,
    server_info: Mapping[str, Any],
    printer_info: Mapping[str, Any],
    capabilities: CapabilitySnapshot,
    subscription: Mapping[str, Any],
) -> PrinterSnapshot:
    """Replace all prior evidence after a complete discovery transaction."""
    _require_ready(server_info, printer_info)
    eventtime, status = _validate_subscription(subscription, capabilities.objects)
    phase = _phase_from_status(status)
    return PrinterSnapshot(
        printer_id=previous.printer_id,
        revision=previous.revision + 1,
        connected=True,
        phase=phase,
        reason="observed",
        eventtime=eventtime,
        capabilities=capabilities,
        status=status,
    )


def apply_status_update(
    previous: PrinterSnapshot,
    *,
    status_diff: Mapping[str, Any],
    eventtime: float,
) -> PrinterSnapshot:
    """Apply one strictly newer notification to known subscribed objects."""
    if not previous.connected or previous.capabilities is None or previous.eventtime is None:
        raise StateEvidenceError("status update arrived without a complete baseline")
    checked_eventtime = _eventtime(eventtime)
    if checked_eventtime <= previous.eventtime:
        raise StateEvidenceError("status update is stale or duplicated")
    checked_diff = _validate_status(status_diff, previous.capabilities.objects)
    merged = {name: dict(values) for name, values in previous.status.items()}
    for name, values in checked_diff.items():
        merged.setdefault(name, {}).update(values)
    return previous.model_copy(
        update={
            "revision": previous.revision + 1,
            "phase": _phase_from_status(merged),
            "reason": "observed",
            "eventtime": checked_eventtime,
            "status": merged,
        }
    )


def mark_not_ready(previous: PrinterSnapshot, reason: str) -> PrinterSnapshot:
    """Invalidate actuation-relevant evidence while retaining bounded diagnostics."""
    return previous.model_copy(
        update={
            "revision": previous.revision + 1,
            "connected": True,
            "phase": PrinterPhase.NOT_READY,
            "reason": reason,
            "eventtime": None,
            "status": {},
        }
    )


def mark_capability_limited(
    previous: PrinterSnapshot, capabilities: CapabilitySnapshot
) -> PrinterSnapshot:
    """Record discovery while denying operation without mandatory objects."""
    return previous.model_copy(
        update={
            "revision": previous.revision + 1,
            "connected": True,
            "phase": PrinterPhase.NOT_READY,
            "reason": "missing_required_objects",
            "eventtime": None,
            "capabilities": capabilities,
            "status": {},
        }
    )


def mark_disconnected(
    previous: PrinterSnapshot, reason: str = "moonraker_disconnected"
) -> PrinterSnapshot:
    """Discard all volatile evidence when Moonraker or Klippy disconnects."""
    return previous.model_copy(
        update={
            "revision": previous.revision + 1,
            "connected": False,
            "phase": PrinterPhase.OFFLINE,
            "reason": reason,
            "eventtime": None,
            "capabilities": None,
            "status": {},
        }
    )


def _require_ready(server_info: Mapping[str, Any], printer_info: Mapping[str, Any]) -> None:
    if server_info.get("klippy_connected") is not True:
        raise StateEvidenceError("Moonraker does not report a Klippy connection")
    if server_info.get("klippy_state") != "ready" or printer_info.get("state") != "ready":
        raise StateEvidenceError("Klippy is not ready")


def _validate_subscription(
    subscription: Mapping[str, Any], known_objects: frozenset[str]
) -> tuple[float, dict[str, dict[str, Any]]]:
    if set(subscription) != {"eventtime", "status"}:
        raise StateEvidenceError("subscription response has an unexpected shape")
    return (
        _eventtime(subscription["eventtime"]),
        _validate_status(subscription["status"], known_objects),
    )


def _eventtime(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StateEvidenceError("eventtime is not numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise StateEvidenceError("eventtime is outside the accepted range")
    return result


def _validate_status(status: object, known_objects: frozenset[str]) -> dict[str, dict[str, Any]]:
    if not isinstance(status, Mapping):
        raise StateEvidenceError("printer status is not an object")
    result: dict[str, dict[str, Any]] = {}
    for raw_name, raw_values in status.items():
        if not isinstance(raw_name, str) or raw_name not in known_objects:
            raise StateEvidenceError("printer status contains an unknown object")
        if not isinstance(raw_values, Mapping):
            raise StateEvidenceError("printer object status is not an object")
        result[raw_name] = dict(raw_values)
    return result


def _phase_from_status(status: Mapping[str, Mapping[str, Any]]) -> PrinterPhase:
    if not _REQUIRED_STATUS_OBJECTS.issubset(status):
        raise StateEvidenceError("required printer status evidence is missing")
    print_stats = status["print_stats"]
    pause_resume = status["pause_resume"]
    raw_state = print_stats.get("state")
    if not isinstance(raw_state, str) or raw_state not in _PRINT_PHASES:
        raise StateEvidenceError("print_stats state is missing or unknown")
    is_paused = pause_resume.get("is_paused")
    if not isinstance(is_paused, bool) or is_paused is not (raw_state == "paused"):
        raise StateEvidenceError("pause state evidence is missing or contradictory")
    return _PRINT_PHASES[raw_state]
