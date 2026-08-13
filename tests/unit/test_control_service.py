from __future__ import annotations

import asyncio
from collections.abc import Iterable

import pytest

from klove.domain.control import (
    ControlIntent,
    ControlOperation,
    ControlResult,
    ControlStatus,
    LiveControlState,
)
from klove.domain.discovery import discover_capabilities
from klove.domain.models import (
    JobHistoryStatus,
    JobIdentitySnapshot,
    PrinterPhase,
    initial_snapshot,
)
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
        self.query_count = 0

    async def query(self) -> LiveControlState:
        self.query_count += 1
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
            "job": JobIdentitySnapshot(
                job_id="000001",
                filename="job.gcode",
                start_time=1_700_000_000.0,
                status=JobHistoryStatus.IN_PROGRESS,
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
    job_id: str = "000001",
) -> LiveControlState:
    return LiveControlState(eventtime, phase, job_id, 1_700_000_000.0, filename, position)


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
async def test_distinct_operations_for_one_printer_are_serialized_through_reconciliation() -> None:
    class ReconciliationBarrierTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__(
                [
                    live(),
                    live(11.0, PrinterPhase.PAUSED),
                    live(11.0, PrinterPhase.PAUSED),
                ]
            )
            self.confirmation_entered = asyncio.Event()
            self.release_confirmation = asyncio.Event()

        async def query(self) -> LiveControlState:
            result = await super().query()
            if self.query_count == 2:
                self.confirmation_entered.set()
                await self.release_confirmation.wait()
            return result

    transport = ReconciliationBarrierTransport()
    service, intent, _registry = await setup(transport)
    second_intent = ControlIntent(
        intent.printer_id,
        intent.operation,
        intent.state_token,
        "10000000-0000-4000-8000-000000000000",
    )

    first = asyncio.create_task(service.execute(intent))
    await transport.confirmation_entered.wait()
    second = asyncio.create_task(service.execute(second_intent))
    await asyncio.sleep(0)

    assert transport.query_count == 2
    assert not second.done()

    transport.release_confirmation.set()
    assert (await first).status is ControlStatus.CONFIRMED
    assert (await second).code == "preflight_state_mismatch"
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
async def test_registry_advance_bounded_by_preflight_can_dispatch() -> None:
    transport = FakeTransport(
        [live(11.0, position=110), live(12.0, PrinterPhase.PAUSED, position=110)]
    )
    service, intent, registry = await setup(transport)
    original_query = transport.query

    async def query_then_change() -> LiveControlState:
        result = await original_query()
        if transport.query_count == 1:
            current = await registry.get("voron")
            assert current is not None
            status = {name: dict(values) for name, values in current.status.items()}
            status["virtual_sdcard"]["file_position"] = 110
            await registry.replace(
                current.model_copy(update={"revision": 2, "eventtime": 11.0, "status": status})
            )
            advanced = await registry.get("voron")
            assert advanced is not None and advanced.state_token == intent.state_token
        return result

    transport.query = query_then_change  # type: ignore[method-assign]
    result = await service.execute(intent)

    assert result.status is ControlStatus.CONFIRMED
    assert transport.dispatched == [ControlOperation.PAUSE]


@pytest.mark.asyncio
async def test_registry_change_newer_than_preflight_is_denied() -> None:
    transport = FakeTransport([live()])
    service, intent, registry = await setup(transport)
    original_query = transport.query

    async def query_then_change() -> LiveControlState:
        result = await original_query()
        current = await registry.get("voron")
        assert current is not None
        await registry.replace(
            current.model_copy(update={"revision": 2, "control_revision": 2, "eventtime": 11.0})
        )
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
@pytest.mark.parametrize("failure", ["dispatch", "reconciliation"])
async def test_unknown_fences_every_new_key_for_the_same_state_token(failure: str) -> None:
    if failure == "dispatch":
        transport = FakeTransport([live()])
        transport.dispatch_error = True
    else:
        transport = FakeTransport([live(), live(11.0, filename="other.gcode")])
    service, intent, _registry = await setup(transport)

    first = await service.execute(intent)
    second = await service.execute(
        ControlIntent(
            intent.printer_id,
            ControlOperation.CANCEL,
            intent.state_token,
            "10000000-0000-4000-8000-000000000000",
        )
    )

    assert first.status is ControlStatus.OUTCOME_UNKNOWN
    assert second == ControlResult(
        operation=ControlOperation.CANCEL,
        status=ControlStatus.OUTCOME_UNKNOWN,
        code="outcome_unknown",
    )
    assert transport.dispatched == [ControlOperation.PAUSE]
    expected_queries = 1 if failure == "dispatch" else 2
    assert transport.query_count == expected_queries


@pytest.mark.asyncio
async def test_newly_observed_state_token_can_cross_an_uncertainty_fence() -> None:
    transport = FakeTransport([live(), live(12.0), live(13.0, PrinterPhase.PAUSED)])
    transport.dispatch_error = True
    service, intent, registry = await setup(transport)
    assert (await service.execute(intent)).status is ControlStatus.OUTCOME_UNKNOWN

    current = await registry.get(intent.printer_id)
    assert current is not None
    observed = current.model_copy(update={"revision": 2, "control_revision": 2, "eventtime": 12.0})
    await registry.replace(observed)
    transport.dispatch_error = False
    result = await service.execute(
        ControlIntent(
            intent.printer_id,
            intent.operation,
            observed.state_token,
            "10000000-0000-4000-8000-000000000000",
        )
    )

    assert result.status is ControlStatus.CONFIRMED
    assert transport.dispatched == [ControlOperation.PAUSE, ControlOperation.PAUSE]


@pytest.mark.asyncio
async def test_forward_progress_does_not_cross_an_uncertainty_fence() -> None:
    transport = FakeTransport([live()])
    transport.dispatch_error = True
    service, intent, registry = await setup(transport)
    assert (await service.execute(intent)).status is ControlStatus.OUTCOME_UNKNOWN

    current = await registry.get(intent.printer_id)
    assert current is not None
    status = {name: dict(values) for name, values in current.status.items()}
    status["virtual_sdcard"]["file_position"] = 110
    progressed = current.model_copy(update={"revision": 2, "eventtime": 11.0, "status": status})
    assert progressed.state_token == intent.state_token
    await registry.replace(progressed)

    result = await service.execute(
        ControlIntent(
            intent.printer_id,
            intent.operation,
            progressed.state_token,
            "10000000-0000-4000-8000-000000000000",
        )
    )

    assert result.status is ControlStatus.OUTCOME_UNKNOWN
    assert transport.query_count == 1
    assert transport.dispatched == [ControlOperation.PAUSE]


@pytest.mark.asyncio
async def test_unexpected_state_until_deadline_is_outcome_unknown() -> None:
    transport = FakeTransport([live(), *([live()] * 100)])
    service, intent, _registry = await setup(transport, timeout=0.002, poll=0.001)

    assert (await service.execute(intent)).status is ControlStatus.OUTCOME_UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ambiguous",
    [
        live(11.0, PrinterPhase.PAUSED, filename="other.gcode"),
        live(9.0, PrinterPhase.PAUSED),
        live(11.0, PrinterPhase.PAUSED, position=99),
    ],
)
async def test_ambiguous_post_dispatch_result_is_retained_and_never_retried(
    ambiguous: LiveControlState,
) -> None:
    transport = FakeTransport([live(), ambiguous])
    service, intent, _registry = await setup(transport)

    first = await service.execute(intent)
    second = await service.execute(intent)

    assert first.status is ControlStatus.OUTCOME_UNKNOWN
    assert second == first
    assert transport.dispatched == [ControlOperation.PAUSE]
    assert transport.query_count == 2


@pytest.mark.asyncio
async def test_capacity_evicts_denials_but_never_dispatched_or_inflight_entries() -> None:
    denied_transport = FakeTransport(
        [ControlTransportError(), live(), live(11.0, PrinterPhase.PAUSED)]
    )
    service, intent, _registry = await setup(denied_transport, capacity=1)
    assert (await service.execute(intent)).status is ControlStatus.DENIED
    second = ControlIntent(
        intent.printer_id,
        intent.operation,
        intent.state_token,
        "10000000-0000-4000-8000-000000000000",
    )
    assert (await service.execute(second)).status is ControlStatus.CONFIRMED

    confirmed_transport = FakeTransport([live(), live(11.0, PrinterPhase.PAUSED)])
    service, intent, _registry = await setup(confirmed_transport, capacity=1)
    assert (await service.execute(intent)).status is ControlStatus.CONFIRMED
    assert (await service.execute(second)).code == "idempotency_capacity"

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
