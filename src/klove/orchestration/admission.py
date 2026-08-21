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
    owners: dict[asyncio.Task[object], int] = field(default_factory=dict)
    leases: dict[object, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PrinterAdmissionLease:
    """Opaque live permission to compose one already-held printer admission."""

    _gates: PrinterAdmissionGates
    _printer_id: str
    _token: object


class PrinterAdmissionGates:
    """Serialize safety-sensitive work for each exact printer identity."""

    def __init__(self) -> None:
        self._entries: dict[str, _GateEntry] = {}
        self._entries_lock = asyncio.Lock()

    @asynccontextmanager
    async def hold(
        self,
        printer_id: str,
        *,
        lease: PrinterAdmissionLease | None = None,
    ) -> AsyncIterator[None]:
        """Hold one printer gate and remove its idle bookkeeping afterward.

        Same-task re-entry is safe for synchronous composition.  Cross-task
        composition requires a live :class:`PrinterAdmissionLease`, supplied
        explicitly by the outer holder.  Child tasks therefore cannot inherit
        admission accidentally, while a coordinator can safely invoke a
        service that creates its own task without deadlocking.
        """
        entry = await self._claim(printer_id)
        if lease is not None:
            if not self._claim_lease(printer_id, entry, lease):
                await self._release(printer_id, entry)
                raise RuntimeError("printer admission lease is unavailable")
            try:
                yield
            finally:
                self._release_lease(entry, lease._token)
                await self._release(printer_id, entry)
            return
        owner = asyncio.current_task()
        if owner is None:
            await self._release(printer_id, entry)
            raise RuntimeError("printer admission requires an asyncio task")
        acquired = False
        nested = False
        try:
            if owner in entry.owners:
                entry.owners[owner] += 1
                nested = True
            else:
                await entry.lock.acquire()
                entry.owners[owner] = 1
                acquired = True
            yield
        finally:
            if nested:
                entry.owners[owner] -= 1
            elif acquired:
                del entry.owners[owner]
                entry.lock.release()
            await self._release(printer_id, entry)

    @asynccontextmanager
    async def lease(self, printer_id: str) -> AsyncIterator[PrinterAdmissionLease]:
        """Hold a gate and issue one explicit non-inheriting composition lease."""
        entry = await self._claim(printer_id)
        acquired = False
        token = object()
        try:
            await entry.lock.acquire()
            acquired = True
            entry.leases[token] = 1
            yield PrinterAdmissionLease(self, printer_id, token)
        finally:
            if acquired:
                self._release_lease(entry, token)
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

    def _claim_lease(
        self,
        printer_id: str,
        entry: _GateEntry,
        lease: PrinterAdmissionLease,
    ) -> bool:
        if lease._gates is not self or lease._printer_id != printer_id:
            return False
        count = entry.leases.get(lease._token)
        if count != 1:
            return False
        entry.leases[lease._token] = 2
        return True

    @staticmethod
    def _release_lease(entry: _GateEntry, token: object) -> None:
        count = entry.leases[token] - 1
        if count:
            entry.leases[token] = count
            return
        del entry.leases[token]
        entry.lock.release()
