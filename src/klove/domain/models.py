"""Canonical, immutable printer domain models."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PrinterPhase(StrEnum):
    """Canonical printer lifecycle states exposed by Klove."""

    OFFLINE = "offline"
    NOT_READY = "not_ready"
    IDLE = "idle"
    PREPARING = "preparing"
    PRINTING = "printing"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"


class CapabilitySnapshot(BaseModel):
    """Known capabilities discovered from loaded Klipper objects."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    objects: frozenset[str]
    missing_dispatch_objects: tuple[str, ...]
    extruders: tuple[str, ...]
    heaters: tuple[str, ...]
    fans: tuple[str, ...]
    filament_sensors: tuple[str, ...]
    has_heated_bed: bool
    has_exclude_object: bool
    dispatch_eligible: bool
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class PrinterSnapshot(BaseModel):
    """Latest complete evidence held for one printer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    printer_id: str
    revision: int = Field(ge=0)
    connected: bool
    phase: PrinterPhase
    reason: str
    eventtime: float | None
    capabilities: CapabilitySnapshot | None
    status: dict[str, dict[str, Any]]


def initial_snapshot(printer_id: str) -> PrinterSnapshot:
    """Create an offline snapshot before any remote evidence exists."""
    return PrinterSnapshot(
        printer_id=printer_id,
        revision=0,
        connected=False,
        phase=PrinterPhase.OFFLINE,
        reason="not_connected",
        eventtime=None,
        capabilities=None,
        status={},
    )
