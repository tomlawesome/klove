from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from klove.domain.onboarding import PrinterLifecycle, RegisteredPrinter
from klove.security.compatibility import CompatibilityPrincipal
from klove.security.compatibility_sessions import CompatibilitySessionRegistry

from ..onboarding_helpers import OTHER_PRINTER_UUID, printer, safety_profile


class Writer:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def principal(record: RegisteredPrinter) -> CompatibilityPrincipal:
    return CompatibilityPrincipal(
        printer_uuid=record.printer_uuid,
        proxy_serial=record.proxy_serial,
        record_revision=record.revision,
        control_enabled=record.control_enabled,
        dispatch_enabled=record.dispatch_enabled,
    )


async def parked() -> None:
    await asyncio.Event().wait()


async def test_commit_revokes_stale_and_inactive_sessions_without_waiting() -> None:
    sessions = CompatibilitySessionRegistry()
    active = printer()
    sessions.reconcile_committed(active)
    current_writer = Writer()
    current_task = asyncio.create_task(parked())
    assert sessions.register(principal(active), current_task, current_writer)

    replacement = active.model_copy(update={"revision": 2, "updated_at_unix_ms": 1_100})
    sessions.reconcile_committed(replacement)

    assert current_writer.closed
    with pytest.raises(asyncio.CancelledError):
        await current_task

    replacement_writer = Writer()
    replacement_task = asyncio.create_task(parked())
    assert sessions.register(principal(replacement), replacement_task, replacement_writer)
    disabled = replacement.model_copy(
        update={
            "lifecycle": PrinterLifecycle.DISABLED,
            "control_enabled": False,
            "dispatch_enabled": False,
            "revision": 3,
            "updated_at_unix_ms": 1_200,
        }
    )
    sessions.reconcile_committed(disabled)

    assert replacement_writer.closed
    with pytest.raises(asyncio.CancelledError):
        await replacement_task


async def test_late_old_registration_is_closed_and_current_unrelated_sessions_remain() -> None:
    sessions = CompatibilitySessionRegistry()
    first = printer()
    second = printer(
        printer_uuid=OTHER_PRINTER_UUID,
        safety_profiles=(safety_profile(printer_uuid=OTHER_PRINTER_UUID),),
    )
    sessions.reconcile_committed(first)
    sessions.reconcile_committed(second)
    first_writer = Writer()
    first_task = asyncio.create_task(parked())
    second_writer = Writer()
    second_task = asyncio.create_task(parked())
    assert sessions.register(principal(first), first_task, first_writer)
    assert sessions.register(principal(second), second_task, second_writer)

    replacement = first.model_copy(update={"revision": 2, "updated_at_unix_ms": 1_100})
    sessions.reconcile_committed(replacement)
    stale_writer = Writer()
    stale_task = asyncio.create_task(parked())
    assert not sessions.register(principal(first), stale_task, stale_writer)

    assert first_writer.closed and stale_writer.closed
    assert not second_writer.closed and not second_task.done()
    with pytest.raises(asyncio.CancelledError):
        await first_task
    with pytest.raises(asyncio.CancelledError):
        await stale_task
    second_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second_task


async def test_reconciliation_is_monotonic_and_unregister_is_idempotent() -> None:
    sessions = CompatibilitySessionRegistry()
    first = printer()
    second = first.model_copy(update={"revision": 2, "updated_at_unix_ms": 1_100})
    sessions.reconcile_committed(second)
    sessions.reconcile_committed(first)
    task = asyncio.create_task(parked())
    writer = Writer()
    assert sessions.register(principal(second), task, writer)
    sessions.reconcile_committed(second)
    assert not writer.closed and not task.done()
    sessions.unregister(principal(second), task, writer)
    sessions.unregister(principal(second), task, writer)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_reconciliation_rejects_conflicting_or_noncanonical_records() -> None:
    sessions = CompatibilitySessionRegistry()
    record = printer()
    sessions.reconcile_committed(record)
    conflict = record.model_copy(update={"display_name": "Changed"})
    with pytest.raises(ValueError, match="conflicting"):
        sessions.reconcile_committed(conflict)
    with pytest.raises(ValueError, match="canonical"):
        sessions.reconcile_committed(cast(RegisteredPrinter, object()))


async def test_registration_rejects_malformed_principals_and_tasks() -> None:
    sessions = CompatibilitySessionRegistry()
    record = printer()
    sessions.reconcile_committed(record)
    writer = Writer()
    assert not sessions.register(
        cast(CompatibilityPrincipal, object()), cast(asyncio.Task[None], object()), writer
    )
    assert writer.closed
    assert not sessions.register(
        cast(CompatibilityPrincipal, object()),
        cast(asyncio.Task[None], object()),
        cast(Any, object()),
    )
    sessions.unregister(
        cast(CompatibilityPrincipal, object()), cast(asyncio.Task[None], object()), writer
    )

    for field, value in (
        ("printer_uuid", 1),
        ("proxy_serial", 1),
        ("record_revision", True),
        ("control_enabled", 1),
        ("dispatch_enabled", 1),
    ):
        malformed = principal(record)
        object.__setattr__(malformed, field, value)
        task = asyncio.create_task(parked())
        malformed_writer = Writer()
        assert not sessions.register(malformed, task, malformed_writer)
        assert malformed_writer.closed
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_unregister_preserves_another_current_session() -> None:
    sessions = CompatibilitySessionRegistry()
    record = printer()
    sessions.reconcile_committed(record)
    first_task = asyncio.create_task(parked())
    second_task = asyncio.create_task(parked())
    first_writer = Writer()
    second_writer = Writer()
    session_principal = principal(record)
    assert sessions.register(session_principal, first_task, first_writer)
    assert sessions.register(session_principal, second_task, second_writer)
    sessions.unregister(session_principal, first_task, first_writer)
    sessions.reconcile_committed(
        record.model_copy(update={"revision": 2, "updated_at_unix_ms": 1_100})
    )
    assert not first_writer.closed and second_writer.closed
    first_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_task
    with pytest.raises(asyncio.CancelledError):
        await second_task
