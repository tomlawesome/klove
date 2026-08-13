"""Canonical, immutable printer domain models."""

from __future__ import annotations

import hashlib
import json
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_BOOT_EPOCH = str(uuid.uuid4())


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


class JobHistoryStatus(StrEnum):
    """Moonraker history states relevant to exact print identity."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"
    KLIPPY_SHUTDOWN = "klippy_shutdown"
    KLIPPY_DISCONNECT = "klippy_disconnect"
    INTERRUPTED = "interrupted"
    SERVER_EXIT = "server_exit"


class JobIdentitySnapshot(BaseModel):
    """One immutable Moonraker history identity for a print instance."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    job_id: str = Field(pattern=r"^[0-9A-F]{6,16}$")
    filename: str = Field(min_length=1)
    start_time: float = Field(ge=0)
    status: JobHistoryStatus

    @field_validator("start_time", mode="before")
    @classmethod
    def reject_boolean_start_time(cls, value: object) -> object:
        """Keep JSON booleans out of numeric job identity evidence."""
        if isinstance(value, bool):
            raise ValueError("start_time must be numeric")
        return value


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
    epoch: str = Field(
        default=_BOOT_EPOCH,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    )
    revision: int = Field(ge=0)
    control_revision: int = Field(default=0, ge=0)
    connected: bool
    phase: PrinterPhase
    reason: str
    eventtime: float | None
    capabilities: CapabilitySnapshot | None
    job: JobIdentitySnapshot | None = None
    status: dict[str, dict[str, Any]]

    @property
    def state_token(self) -> str:
        """Return a boot-scoped opaque token binding exact control state."""
        capability_fingerprint = (
            None if self.capabilities is None else self.capabilities.fingerprint
        )
        evidence = json.dumps(
            {
                "printer_id": self.printer_id,
                "epoch": self.epoch,
                "control_revision": self.control_revision,
                "connected": self.connected,
                "phase": self.phase,
                "reason": self.reason,
                "capability_fingerprint": capability_fingerprint,
                "filename": self.status.get("print_stats", {}).get("filename"),
                "job": None if self.job is None else self.job.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(evidence.encode()).hexdigest()


def initial_snapshot(printer_id: str) -> PrinterSnapshot:
    """Create an offline snapshot before any remote evidence exists."""
    return PrinterSnapshot(
        printer_id=printer_id,
        revision=0,
        control_revision=0,
        connected=False,
        phase=PrinterPhase.OFFLINE,
        reason="not_connected",
        eventtime=None,
        capabilities=None,
        job=None,
        status={},
    )
