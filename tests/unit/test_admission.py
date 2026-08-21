from __future__ import annotations

import asyncio

import pytest

from klove.orchestration.admission import PrinterAdmissionGates, PrinterAdmissionLease


@pytest.mark.asyncio
async def test_same_printer_serializes_while_other_printer_remains_independent() -> None:
    gates = PrinterAdmissionGates()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    same_entered = asyncio.Event()
    other_entered = asyncio.Event()

    async def first() -> None:
        async with gates.hold("first"):
            first_entered.set()
            await release_first.wait()

    async def same() -> None:
        async with gates.hold("first"):
            same_entered.set()

    async def other() -> None:
        async with gates.hold("other"):
            other_entered.set()

    first_task = asyncio.create_task(first())
    await first_entered.wait()
    same_task = asyncio.create_task(same())
    other_task = asyncio.create_task(other())
    await other_entered.wait()
    await asyncio.sleep(0)
    assert not same_entered.is_set()

    release_first.set()
    await asyncio.gather(first_task, same_task, other_task)
    assert same_entered.is_set()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_leak_or_release_the_holder() -> None:
    gates = PrinterAdmissionGates()
    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()
    waiter_entered = asyncio.Event()

    async def holder() -> None:
        async with gates.hold("printer"):
            holder_entered.set()
            await release_holder.wait()

    async def waiter() -> None:
        async with gates.hold("printer"):
            waiter_entered.set()

    holder_task = asyncio.create_task(holder())
    await holder_entered.wait()
    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    waiter_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter_task
    assert not waiter_entered.is_set()

    release_holder.set()
    await holder_task
    async with gates.hold("printer"):
        pass


@pytest.mark.asyncio
async def test_cancelled_holder_releases_the_gate() -> None:
    gates = PrinterAdmissionGates()
    entered = asyncio.Event()

    async def holder() -> None:
        async with gates.hold("printer"):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(holder())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with gates.hold("printer"):
        pass


@pytest.mark.asyncio
async def test_explicit_lease_keeps_the_gate_held_until_a_shielded_child_finishes() -> None:
    gates = PrinterAdmissionGates()
    child_entered = asyncio.Event()
    release_child = asyncio.Event()
    contender_entered = asyncio.Event()

    async def child(lease: PrinterAdmissionLease) -> None:
        async with gates.hold("printer", lease=lease):
            child_entered.set()
            await release_child.wait()

    async def outer() -> None:
        async with gates.lease("printer") as lease:
            task = asyncio.create_task(child(lease))
            await child_entered.wait()
            await asyncio.shield(task)

    outer_task = asyncio.create_task(outer())
    await child_entered.wait()
    outer_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer_task

    async def contender() -> None:
        async with gates.hold("printer"):
            contender_entered.set()

    contender_task = asyncio.create_task(contender())
    await asyncio.sleep(0)
    assert not contender_entered.is_set()
    release_child.set()
    await contender_task
    assert contender_entered.is_set()


@pytest.mark.asyncio
async def test_lease_rejects_cross_printer_cross_gate_and_closed_use() -> None:
    first = PrinterAdmissionGates()
    second = PrinterAdmissionGates()
    async with first.lease("printer") as lease:
        with pytest.raises(RuntimeError):
            async with first.hold("other", lease=lease):
                pass
        with pytest.raises(RuntimeError):
            async with second.hold("printer", lease=lease):
                pass
    with pytest.raises(RuntimeError):
        async with first.hold("printer", lease=lease):
            pass


@pytest.mark.asyncio
async def test_one_lease_allows_only_one_concurrent_child_but_sequential_reuse() -> None:
    gates = PrinterAdmissionGates()
    entered = asyncio.Event()
    release = asyncio.Event()

    async with gates.lease("printer") as lease:

        async def first_child() -> None:
            async with gates.hold("printer", lease=lease):
                entered.set()
                await release.wait()

        first = asyncio.create_task(first_child())
        await entered.wait()
        with pytest.raises(RuntimeError, match="unavailable"):
            async with gates.hold("printer", lease=lease):
                pass
        release.set()
        await first
        async with gates.hold("printer", lease=lease):
            pass


@pytest.mark.asyncio
async def test_same_task_nested_hold_and_cancelled_waiting_lease_release_cleanly() -> None:
    gates = PrinterAdmissionGates()
    waiting = asyncio.Event()

    async with gates.hold("printer"):
        async with gates.hold("printer"):
            pass

        async def borrow() -> None:
            waiting.set()
            async with gates.lease("printer"):
                pytest.fail("cancelled waiter acquired the gate")

        task = asyncio.create_task(borrow())
        await waiting.wait()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_hold_rejects_execution_without_an_asyncio_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gates = PrinterAdmissionGates()
    monkeypatch.setattr(asyncio, "current_task", lambda: None)

    with pytest.raises(RuntimeError, match="requires an asyncio task"):
        async with gates.hold("printer"):
            pass
