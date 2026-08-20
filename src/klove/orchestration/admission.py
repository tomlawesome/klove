"""Shared cancellation-safe per-printer admission gates."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass(slots=True)
class _GateEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class PrinterAdmissionGates:
    """Serialize safety-sensitive work for each exact printer identity."""

    def __init__(self) -> None:
        self._entries: dict[str, _GateEntry] = {}
        self._entries_lock = asyncio.Lock()

    @asynccontextmanager
    async def hold(self, printer_id: str) -> AsyncIterator[None]:
        """Hold one printer gate and remove its idle bookkeeping afterward."""
        entry = await self._claim(printer_id)
        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            await self._release(printer_id, entry)

    async def _claim(self, printer_id: str) -> _GateEntry:
        async with self._entries_lock:
            entry = self._entries.setdefault(printer_id, _GateEntry())
            entry.users += 1
            return entry

    async def _release(self, printer_id: str, entry: _GateEntry) -> None:
        async with self._entries_lock:
            entry.users -= 1
            if entry.users == 0 and self._entries.get(printer_id) is entry:
                del self._entries[printer_id]
