"""Canonical runtime-state provider for the bounded MQTT report adapter."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Protocol

from klove.domain.models import PrinterSnapshot
from klove.northbound.mqtt.reports import project_report
from klove.security.compatibility import CompatibilityPrincipal

_EMPTY_REPORT = b'{"print":{"command":"push_status"}}'


class PrinterSnapshotReader(Protocol):
    """Read one current canonical snapshot without listing printer identities."""

    def get(self, printer_id: str) -> Awaitable[PrinterSnapshot | None]: ...


class RegistryMqttReportProvider:
    """Project only the authenticated printer's current canonical snapshot."""

    def __init__(self, snapshots: PrinterSnapshotReader) -> None:
        self._snapshots = snapshots

    async def report(self, principal: CompatibilityPrincipal) -> bytes:
        """Return the conservative empty report for every unavailable evidence path."""
        if type(principal) is not CompatibilityPrincipal or type(principal.printer_uuid) is not str:
            return _EMPTY_REPORT
        try:
            snapshot = await self._snapshots.get(principal.printer_uuid)
            if (
                type(snapshot) is not PrinterSnapshot
                or snapshot.printer_id != principal.printer_uuid
            ):
                return _EMPTY_REPORT
            return project_report(snapshot)
        except Exception:
            # A report failure must not disclose registry state.
            return _EMPTY_REPORT
