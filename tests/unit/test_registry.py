import asyncio
from dataclasses import FrozenInstanceError

import pytest

from klove.domain.models import initial_snapshot
from klove.registry import PrinterObservation, PrinterRegistry


class FakeClock:
    def __init__(self, *values: float) -> None:
        self._values = iter(values)
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return next(self._values)


@pytest.mark.asyncio
async def test_registry_is_ordered_and_requires_advancing_known_snapshots() -> None:
    clock = FakeClock(10.0, 11.0, 12.0)
    registry = PrinterRegistry(["zeta", "alpha"], clock=clock)

    assert [snapshot.printer_id for snapshot in await registry.list()] == ["alpha", "zeta"]
    assert await registry.get("missing") is None
    assert (await registry.get("alpha")) == initial_snapshot("alpha")
    assert await registry.observation("missing") is None
    assert await registry.observation("alpha") == PrinterObservation(
        snapshot=initial_snapshot("alpha"),
        received_monotonic=11.0,
    )

    advanced = initial_snapshot("alpha").model_copy(update={"revision": 1})
    await registry.replace(advanced)
    assert await registry.get("alpha") == advanced
    assert await registry.observation("alpha") == PrinterObservation(
        snapshot=advanced,
        received_monotonic=12.0,
    )

    with pytest.raises(ValueError, match="did not advance"):
        await registry.replace(advanced)
    with pytest.raises(KeyError, match="not configured"):
        await registry.replace(initial_snapshot("missing").model_copy(update={"revision": 1}))
    assert clock.calls == 3


def test_observation_record_is_immutable() -> None:
    observation = PrinterObservation(initial_snapshot("alpha"), 1.0)

    with pytest.raises(FrozenInstanceError):
        observation.received_monotonic = 2.0  # type: ignore[misc]


@pytest.mark.asyncio
async def test_wait_returns_an_already_newer_observation_without_sleeping() -> None:
    registry = PrinterRegistry(["alpha"], clock=FakeClock(1.0, 2.0))
    advanced = initial_snapshot("alpha").model_copy(update={"revision": 2})
    await registry.replace(advanced)

    assert await registry.wait_for_revision("alpha", 1, timeout=0) == PrinterObservation(
        snapshot=advanced,
        received_monotonic=2.0,
    )


@pytest.mark.asyncio
async def test_wait_returns_only_after_a_strictly_newer_revision() -> None:
    registry = PrinterRegistry(["alpha"], clock=FakeClock(1.0, 2.0, 3.0, 4.0))
    waiter = asyncio.create_task(registry.wait_for_revision("alpha", 2, timeout=1))
    await asyncio.sleep(0)

    await registry.replace(initial_snapshot("alpha").model_copy(update={"revision": 1}))
    await asyncio.sleep(0)
    assert not waiter.done()

    await registry.replace(initial_snapshot("alpha").model_copy(update={"revision": 2}))
    await asyncio.sleep(0)
    assert not waiter.done()

    expected = initial_snapshot("alpha").model_copy(update={"revision": 3})
    await registry.replace(expected)
    assert await waiter == PrinterObservation(snapshot=expected, received_monotonic=4.0)


@pytest.mark.asyncio
async def test_wait_does_not_lose_a_replacement_before_the_waiter_runs() -> None:
    registry = PrinterRegistry(["alpha"], clock=FakeClock(1.0, 2.0))
    waiter = asyncio.create_task(registry.wait_for_revision("alpha", 0, timeout=0.1))
    expected = initial_snapshot("alpha").model_copy(update={"revision": 1})
    await registry.replace(expected)

    assert await waiter == PrinterObservation(snapshot=expected, received_monotonic=2.0)


@pytest.mark.asyncio
async def test_wait_fails_for_an_unknown_printer_before_timeout() -> None:
    registry = PrinterRegistry(["alpha"])

    with pytest.raises(KeyError, match="not configured"):
        await registry.wait_for_revision("missing", 0, timeout=0)


@pytest.mark.asyncio
async def test_wait_times_out_without_a_newer_revision() -> None:
    registry = PrinterRegistry(["alpha"])

    with pytest.raises(TimeoutError):
        await registry.wait_for_revision("alpha", 0, timeout=0)
