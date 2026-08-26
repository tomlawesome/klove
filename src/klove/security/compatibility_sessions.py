"""Immediate revocation of Grove compatibility sessions on lifecycle commits."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from klove.domain.onboarding import PrinterLifecycle, RegisteredPrinter
from klove.security.compatibility import CompatibilityPrincipal


class _ControlWriter(Protocol):
    """The narrow writer surface needed to terminate a compatibility session."""

    def close(self) -> None:
        """Start closing the control connection without waiting for the peer."""


@dataclass(frozen=True, slots=True)
class _Session:
    principal: CompatibilityPrincipal
    task: asyncio.Task[None]
    writer: _ControlWriter


class CompatibilitySessionRegistry:
    """Own live compatibility sessions and revoke stale principals immediately."""

    def __init__(self) -> None:
        self._committed: dict[str, RegisteredPrinter] = {}
        self._sessions: dict[str, set[_Session]] = {}

    def register(
        self,
        principal: CompatibilityPrincipal,
        task: asyncio.Task[None],
        writer: _ControlWriter,
    ) -> bool:
        """Admit one already-authenticated, locally admitted current session.

        A concurrent lifecycle commit installs its authoritative record before
        this method can run again, so an old principal is closed instead of
        becoming a live session after authentication returned.
        """
        if not _principal_is_valid(principal) or not isinstance(task, asyncio.Task):
            _terminate(task, writer)
            return False
        record = self._committed.get(principal.printer_uuid)
        if record is None or not _principal_matches(record, principal):
            _terminate(task, writer)
            return False
        self._sessions.setdefault(principal.printer_uuid, set()).add(
            _Session(principal, task, writer)
        )
        return True

    def unregister(
        self,
        principal: CompatibilityPrincipal,
        task: asyncio.Task[None],
        writer: _ControlWriter,
    ) -> None:
        """Forget one session; repeated cleanup is deliberately harmless."""
        if not _principal_is_valid(principal) or not isinstance(task, asyncio.Task):
            return
        sessions = self._sessions.get(principal.printer_uuid)
        if sessions is None:
            return
        sessions.discard(_Session(principal, task, writer))
        if not sessions:
            self._sessions.pop(principal.printer_uuid, None)

    def reconcile_committed(self, record: RegisteredPrinter) -> None:
        """Record a canonical commit and synchronously revoke stale sessions.

        This method must be called while the lifecycle admission gate is held.
        It deliberately never awaits task termination: cancellation must return
        control to lifecycle reconciliation without holding that gate hostage.
        """
        if type(record) is not RegisteredPrinter:
            raise ValueError("canonical registered printer required")
        previous = self._committed.get(record.printer_uuid)
        if previous is not None:
            if record.revision < previous.revision:
                return
            if record.revision == previous.revision and record != previous:
                raise ValueError("conflicting committed printer revision")
        self._committed[record.printer_uuid] = record
        stale = tuple(
            session
            for session in self._sessions.get(record.printer_uuid, ())
            if not _principal_matches(record, session.principal)
        )
        for session in stale:
            self.unregister(session.principal, session.task, session.writer)
            _terminate(session.task, session.writer)


def _terminate(task: object, writer: object) -> None:
    close = getattr(writer, "close", None)
    if callable(close):
        close()
    cancel = getattr(task, "cancel", None)
    if callable(cancel):
        cancel()


def _principal_is_valid(principal: object) -> bool:
    return (
        type(principal) is CompatibilityPrincipal
        and type(principal.printer_uuid) is str
        and type(principal.proxy_serial) is str
        and type(principal.record_revision) is int
        and type(principal.control_enabled) is bool
        and type(principal.dispatch_enabled) is bool
    )


def _principal_matches(record: RegisteredPrinter, principal: CompatibilityPrincipal) -> bool:
    return (
        record.lifecycle is PrinterLifecycle.ACTIVE
        and record.printer_uuid == principal.printer_uuid
        and record.proxy_serial == principal.proxy_serial
        and record.revision == principal.record_revision
        and record.control_enabled is principal.control_enabled
        and record.dispatch_enabled is principal.dispatch_enabled
    )
