"""Concurrency-safe in-memory view of current printer evidence."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from klove.domain.models import PrinterSnapshot, initial_snapshot


class PrinterRegistry:
    """Store one immutable snapshot per configured printer."""

    def __init__(self, printer_ids: Iterable[str]) -> None:
        """Create offline entries for every explicit printer id."""
        self._snapshots = {printer_id: initial_snapshot(printer_id) for printer_id in printer_ids}
        self._lock = asyncio.Lock()

    async def replace(self, snapshot: PrinterSnapshot) -> None:
        """Replace an entry only when its revision advances monotonically."""
        async with self._lock:
            current = self._snapshots.get(snapshot.printer_id)
            if current is None:
                raise KeyError("printer is not configured")
            if snapshot.revision <= current.revision:
                raise ValueError("snapshot revision did not advance")
            self._snapshots[snapshot.printer_id] = snapshot

    async def get(self, printer_id: str) -> PrinterSnapshot | None:
        """Return the current immutable snapshot for one printer."""
        async with self._lock:
            return self._snapshots.get(printer_id)

    async def list(self) -> tuple[PrinterSnapshot, ...]:
        """Return all snapshots in stable printer-id order."""
        async with self._lock:
            return tuple(self._snapshots[key] for key in sorted(self._snapshots))
