from __future__ import annotations

import asyncio

import pytest

from klove.orchestration.admission import PrinterAdmissionGates


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
