from __future__ import annotations

import asyncio
from collections.abc import Iterable

import pytest

from klove.domain.control import (
    ControlIntent,
    ControlOperation,
    ControlStatus,
    LiveControlState,
)
from klove.domain.discovery import discover_capabilities
from klove.domain.models import PrinterPhase, initial_snapshot
from klove.errors import ControlTransportError
from klove.orchestration.control import ControlService
from klove.registry import PrinterRegistry


class FakeTransport:
    def __init__(self, queries: Iterable[LiveControlState | Exception]) -> None:
        self.queries = iter(queries)
        self.dispatched: list[ControlOperation] = []
        self.dispatch_error = False
        self.dispatch_entered = asyncio.Event()
        self.release_dispatch = asyncio.Event()
        self.release_dispatch.set()

    async def query(self) -> LiveControlState:
        value = next(self.queries)
        if isinstance(value, Exception):
            raise value
        return value

    async def dispatch(self, operation: ControlOperation) -> None:
        self.dispatch_entered.set()
        await self.release_dispatch.wait()
        self.dispatched.append(operation)
        if self.dispatch_error:
            raise ControlTransportError


async def setup(
    transport: FakeTransport,
    *,
    capacity: int = 10,
    timeout: float = 0.01,
    poll: float = 0.001,
) -> tuple[ControlService, ControlIntent, PrinterRegistry]:
    registry = PrinterRegistry(["voron"])
    current = initial_snapshot("voron").model_copy(
        update={
            "revision": 1,
            "connected": True,
            "phase": PrinterPhase.PRINTING,
            "reason": "observed",
            "eventtime": 10.0,
            "capabilities": discover_capabilities(
                {"pause_resume", "print_stats", "virtual_sdcard"}
            ),
            "status": {
                "print_stats": {"state": "printing", "filename": "job.gcode"},
                "virtual_sdcard": {"file_position": 100},
            },
        }
    )
    await registry.replace(current)
    service = ControlService(
        registry,
        {"voron": transport},
        confirmation_timeout_seconds=timeout,
        poll_interval_seconds=poll,
        idempotency_capacity=capacity,
    )
    return (
        service,
        ControlIntent(
            printer_id="voron",
            operation=ControlOperation.PAUSE,
            state_token=current.state_token,
            idempotency_key="00000000-0000-4000-8000-000000000000",
        ),
        registry,
    )


def live(
    eventtime: float = 10.0,
    phase: PrinterPhase = PrinterPhase.PRINTING,
    filename: str = "job.gcode",
    position: int = 100,
) -> LiveControlState:
    return LiveControlState(eventtime, phase, filename, position)


@pytest.mark.asyncio
async def test_confirmed_command_is_dispatched_once_and_duplicate_reuses_result() -> None:
    transport = FakeTransport([live(), live(11.0, PrinterPhase.PAUSED)])
    service, intent, _registry = await setup(transport)

    first = await service.execute(intent)
    second = await service.execute(intent)

    assert first == second
    assert first.status is ControlStatus.CONFIRMED
    assert transport.dispatched == [ControlOperation.PAUSE]


@pytest.mark.asyncio
async def test_concurrent_duplicate_awaits_the_same_single_dispatch() -> None:
    transport = FakeTransport([live(), live(11.0, PrinterPhase.PAUSED)])
    transport.release_dispatch.clear()
    service, intent, _registry = await setup(transport)

    first = asyncio.create_task(service.execute(intent))
    await transport.dispatch_entered.wait()
    second = asyncio.create_task(service.execute(intent))
    transport.release_dispatch.set()

    assert await first == await second
    assert transport.dispatched == [ControlOperation.PAUSE]


@pytest.mark.asyncio
async def test_same_key_with_different_intent_is_denied() -> None:
    transport = FakeTransport([live(), live(11.0, PrinterPhase.PAUSED)])
    service, intent, _registry = await setup(transport)
    await service.execute(intent)

    conflict = ControlIntent(
        printer_id=intent.printer_id,
        operation=ControlOperation.CANCEL,
        state_token=intent.state_token,
        idempotency_key=intent.idempotency_key,
    )
    result = await service.execute(conflict)

    assert result.status is ControlStatus.DENIED
    assert result.code == "idempotency_conflict"


@pytest.mark.asyncio
async def test_unknown_printer_or_stale_token_never_reaches_transport() -> None:
    transport = FakeTransport([])
    service, intent, _registry = await setup(transport)

    disabled = await service.execute(
        ControlIntent(
            "missing", intent.operation, intent.state_token, "10000000-0000-4000-8000-000000000000"
        )
    )
    stale = await service.execute(
        ControlIntent("voron", intent.operation, "0" * 64, "20000000-0000-4000-8000-000000000000")
    )

    assert disabled.code == "control_disabled"
    assert stale.code == "state_token_mismatch"
    assert transport.dispatched == []


@pytest.mark.asyncio
async def test_preflight_transport_failure_and_mismatch_are_denied() -> None:
    failed = FakeTransport([ControlTransportError()])
    service, intent, _registry = await setup(failed)
    assert (await service.execute(intent)).code == "preflight_unavailable"

    mismatched = FakeTransport([live(filename="other.gcode")])
    service, intent, _registry = await setup(mismatched)
    assert (await service.execute(intent)).code == "preflight_job_mismatch"
    assert mismatched.dispatched == []


@pytest.mark.asyncio
async def test_registry_change_between_poll_and_dispatch_is_denied() -> None:
    transport = FakeTransport([live()])
    service, intent, registry = await setup(transport)
    original_query = transport.query

    async def query_then_change() -> LiveControlState:
        result = await original_query()
        current = await registry.get("voron")
        assert current is not None
        await registry.replace(current.model_copy(update={"revision": 2}))
        return result

    transport.query = query_then_change  # type: ignore[method-assign]
    result = await service.execute(intent)

    assert result.code == "state_token_mismatch"
    assert transport.dispatched == []


@pytest.mark.asyncio
async def test_every_post_dispatch_failure_is_outcome_unknown_and_never_retried() -> None:
    dispatch_failed = FakeTransport([live()])
    dispatch_failed.dispatch_error = True
    service, intent, _registry = await setup(dispatch_failed)
    first = await service.execute(intent)
    second = await service.execute(intent)
    assert first.status is ControlStatus.OUTCOME_UNKNOWN
    assert first == second
    assert dispatch_failed.dispatched == [ControlOperation.PAUSE]

    confirm_failed = FakeTransport([live(), ControlTransportError()])
    service, intent, _registry = await setup(confirm_failed)
    assert (await service.execute(intent)).status is ControlStatus.OUTCOME_UNKNOWN


@pytest.mark.asyncio
async def test_unexpected_state_until_deadline_is_outcome_unknown() -> None:
    transport = FakeTransport([live(), *([live()] * 100)])
    service, intent, _registry = await setup(transport, timeout=0.002, poll=0.001)

    assert (await service.execute(intent)).status is ControlStatus.OUTCOME_UNKNOWN


@pytest.mark.asyncio
async def test_capacity_evicts_confirmed_but_never_uncertain_or_inflight_entries() -> None:
    confirmed_transport = FakeTransport(
        [live(), live(11.0, PrinterPhase.PAUSED), ControlTransportError()]
    )
    service, intent, _registry = await setup(confirmed_transport, capacity=1)
    assert (await service.execute(intent)).status is ControlStatus.CONFIRMED
    second = ControlIntent(
        intent.printer_id,
        intent.operation,
        intent.state_token,
        "10000000-0000-4000-8000-000000000000",
    )
    assert (await service.execute(second)).code == "preflight_unavailable"

    uncertain_transport = FakeTransport([live()])
    uncertain_transport.dispatch_error = True
    service, intent, _registry = await setup(uncertain_transport, capacity=1)
    assert (await service.execute(intent)).status is ControlStatus.OUTCOME_UNKNOWN
    assert (await service.execute(second)).code == "idempotency_capacity"

    inflight_transport = FakeTransport([live(), live(11.0, PrinterPhase.PAUSED)])
    inflight_transport.release_dispatch.clear()
    service, intent, _registry = await setup(inflight_transport, capacity=1)
    first = asyncio.create_task(service.execute(intent))
    await inflight_transport.dispatch_entered.wait()
    assert (await service.execute(second)).code == "idempotency_capacity"
    inflight_transport.release_dispatch.set()
    await first


@pytest.mark.asyncio
async def test_unexpected_internal_failure_is_contained_as_unknown() -> None:
    transport = FakeTransport([ValueError("hostile detail")])
    service, intent, _registry = await setup(transport)

    result = await service.execute(intent)

    assert result.status is ControlStatus.OUTCOME_UNKNOWN
    assert result.code == "outcome_unknown"
