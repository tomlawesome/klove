"""Concurrency-safe in-memory view of current printer evidence."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from klove.domain.models import PrinterSnapshot, initial_snapshot


@dataclass(frozen=True, slots=True)
class PrinterObservation:
    """One immutable snapshot paired with its local receipt time."""

    snapshot: PrinterSnapshot
    received_monotonic: float


class PrinterRegistry:
    """Store one immutable snapshot per configured printer."""

    def __init__(
        self,
        printer_ids: Iterable[str],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create offline entries for every explicit printer id."""
        snapshots = {printer_id: initial_snapshot(printer_id) for printer_id in printer_ids}
        self._clock = clock
        self._observations = {
            printer_id: PrinterObservation(
                snapshot=snapshot,
                received_monotonic=self._clock(),
            )
            for printer_id, snapshot in snapshots.items()
        }
        self._lock = asyncio.Lock()
        self._changed = asyncio.Condition(self._lock)

    async def replace(self, snapshot: PrinterSnapshot) -> None:
        """Replace an entry only when its revision advances monotonically."""
        async with self._changed:
            current = self._observations.get(snapshot.printer_id)
            if current is None:
                raise KeyError("printer is not configured")
            if snapshot.revision <= current.snapshot.revision:
                raise ValueError("snapshot revision did not advance")
            self._observations[snapshot.printer_id] = PrinterObservation(
                snapshot=snapshot,
                received_monotonic=self._clock(),
            )
            self._changed.notify_all()

    async def get(self, printer_id: str) -> PrinterSnapshot | None:
        """Return the current immutable snapshot for one printer."""
        async with self._lock:
            observation = self._observations.get(printer_id)
            return None if observation is None else observation.snapshot

    async def observation(self, printer_id: str) -> PrinterObservation | None:
        """Return the current snapshot and its local monotonic receipt time."""
        async with self._lock:
            return self._observations.get(printer_id)

    async def wait_for_revision(
        self,
        printer_id: str,
        after_revision: int,
        timeout: float,  # noqa: ASYNC109 -- timeout is part of the bounded-wait API.
    ) -> PrinterObservation:
        """Wait until a configured printer has a revision newer than the caller's."""
        async with self._changed:
            if printer_id not in self._observations:
                raise KeyError("printer is not configured")

            async with asyncio.timeout(timeout):
                while True:
                    observation = self._observations[printer_id]
                    if observation.snapshot.revision > after_revision:
                        return observation
                    await self._changed.wait()

    async def list(self) -> tuple[PrinterSnapshot, ...]:
        """Return all snapshots in stable printer-id order."""
        async with self._lock:
            return tuple(self._observations[key].snapshot for key in sorted(self._observations))
