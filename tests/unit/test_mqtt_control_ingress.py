from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from klove.domain.control import (
    ControlIntent,
    ControlOperation,
    ControlResult,
    ControlStatus,
)
from klove.domain.models import PrinterSnapshot, initial_snapshot
from klove.domain.mqtt_ingress import (
    MqttIngressRecord,
    MqttIngressState,
    completed_ingress,
)
from klove.domain.translation import CommandKind, DecodedCommand
from klove.errors import JournalError
from klove.orchestration.admission import PrinterAdmissionGates, PrinterAdmissionLease
from klove.orchestration.control import ControlService
from klove.orchestration.mqtt_control import MqttControlIngress, _InflightRequest
from klove.persistence.mqtt_ingress_journal import (
    MqttIngressCapacityError,
    MqttIngressConflictError,
    MqttIngressJournal,
)
from klove.registry import PrinterRegistry
from klove.security.compatibility import CompatibilityAuthenticator, CompatibilityPrincipal

PRINTER = "11111111-1111-4111-8111-111111111111"
OTHER_PRINTER = "22222222-2222-4222-8222-222222222222"
SERIAL = "KLOVE-11111111-1111-4111-8111-111111111111"
IDENTIFIER = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def principal(**updates: object) -> CompatibilityPrincipal:
    values: dict[str, object] = {
        "printer_uuid": PRINTER,
        "proxy_serial": SERIAL,
        "record_revision": 7,
        "control_enabled": True,
        "dispatch_enabled": False,
    }
    values.update(updates)
    return CompatibilityPrincipal(**values)  # type: ignore[arg-type]


def command(
    kind: CommandKind = CommandKind.PAUSE,
    sequence: str = "1",
    digest: str = "a" * 64,
) -> DecodedCommand:
    return DecodedCommand(kind=kind, sequence_id=sequence, payload_hash=digest)


def outcome(
    operation: ControlOperation = ControlOperation.PAUSE,
    status: ControlStatus = ControlStatus.CONFIRMED,
    code: str = "confirmed",
) -> ControlResult:
    return ControlResult(operation=operation, status=status, code=code)


class FakeAuthenticator:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[CompatibilityPrincipal, PrinterAdmissionLease | None]] = []

    async def revalidate(
        self,
        selected: CompatibilityPrincipal,
        *,
        admission_lease: PrinterAdmissionLease | None = None,
    ) -> bool:
        self.calls.append((selected, admission_lease))
        return self.allowed


class FakeRegistry:
    def __init__(self, snapshot: PrinterSnapshot | None = None) -> None:
        self.snapshot = snapshot
        self.failure: BaseException | None = None
        self.calls: list[str] = []

    async def get(self, printer_id: str) -> PrinterSnapshot | None:
        self.calls.append(printer_id)
        if self.failure is not None:
            raise self.failure
        return self.snapshot


class FakeControls:
    def __init__(self, result: ControlResult | BaseException | None = None) -> None:
        self.result = result or outcome()
        self.calls: list[tuple[ControlIntent, PrinterAdmissionLease | None]] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.before_execute: Callable[[ControlIntent], None] | None = None

    async def execute(
        self,
        intent: ControlIntent,
        *,
        admission_lease: PrinterAdmissionLease | None = None,
    ) -> ControlResult:
        self.calls.append((intent, admission_lease))
        if self.before_execute is not None:
            self.before_execute(intent)
        self.entered.set()
        await self.release.wait()
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeJournal:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], MqttIngressRecord] = {}
        self.initialize_failure: BaseException | None = None
        self.lookup_failure: BaseException | None = None
        self.lookup_absent = False
        self.reserve_failure: BaseException | None = None
        self.complete_failure: BaseException | None = None
        self.complete_without_result = False
        self.initialized = 0
        self.reserve_calls: list[tuple[MqttIngressRecord, int]] = []
        self.complete_calls: list[tuple[MqttIngressRecord, ControlResult]] = []

    def initialize(self) -> None:
        self.initialized += 1
        if self.initialize_failure is not None:
            raise self.initialize_failure

    def lookup(self, printer_uuid: str, sequence_id: str) -> MqttIngressRecord | None:
        if self.lookup_failure is not None:
            raise self.lookup_failure
        if self.lookup_absent:
            return None
        return self.records.get((printer_uuid, sequence_id))

    def reserve(
        self, record: MqttIngressRecord, *, capacity: int
    ) -> tuple[MqttIngressRecord, bool]:
        self.reserve_calls.append((record, capacity))
        if self.reserve_failure is not None:
            raise self.reserve_failure
        key = (record.printer_uuid, record.sequence_id)
        saved = self.records.get(key)
        if saved is not None:
            return saved, False
        self.records[key] = record
        return record, True

    def complete(self, reserved: MqttIngressRecord, result: ControlResult) -> MqttIngressRecord:
        self.complete_calls.append((reserved, result))
        if self.complete_failure is not None:
            raise self.complete_failure
        terminal = completed_ingress(reserved, result)
        self.records[(reserved.printer_uuid, reserved.sequence_id)] = terminal
        if self.complete_without_result:
            return cast(MqttIngressRecord, _MissingResultRecord())
        return terminal


class _MissingResultRecord:
    result = None


def ingress(  # noqa: PLR0913 -- explicit safety dependency fakes keep tests readable.
    *,
    auth: FakeAuthenticator | None = None,
    registry: FakeRegistry | None = None,
    controls: FakeControls | None = None,
    journal: FakeJournal | MqttIngressJournal | None = None,
    admissions: PrinterAdmissionGates | None = None,
    capacity: int = 10,
    id_factory: Callable[[], uuid.UUID] = lambda: IDENTIFIER,
) -> tuple[MqttControlIngress, FakeAuthenticator, FakeRegistry, FakeControls]:
    selected_auth = auth or FakeAuthenticator()
    selected_registry = registry or FakeRegistry(initial_snapshot(PRINTER))
    selected_controls = controls or FakeControls()
    selected_journal = journal or FakeJournal()
    service = MqttControlIngress(
        cast(CompatibilityAuthenticator, selected_auth),
        cast(PrinterRegistry, selected_registry),
        cast(ControlService, selected_controls),
        cast(MqttIngressJournal, selected_journal),
        capacity=capacity,
        admissions=admissions or PrinterAdmissionGates(),
        id_factory=id_factory,
    )
    return service, selected_auth, selected_registry, selected_controls


@pytest.mark.parametrize("capacity", [True, 0, 100_001])
def test_constructor_rejects_non_integer_and_out_of_range_capacity(capacity: object) -> None:
    with pytest.raises(ValueError, match="outside its accepted bounds"):
        ingress(capacity=cast(int, capacity))


@pytest.mark.asyncio
async def test_initialize_is_fail_closed_and_can_recover_readiness() -> None:
    journal = FakeJournal()
    service, auth, _, _ = ingress(journal=journal)

    before = await service.execute(principal(), command())
    assert (before.status, before.code) == (
        ControlStatus.DENIED,
        "ingress_storage_unavailable",
    )
    assert auth.calls == []

    journal.initialize_failure = JournalError("private detail")
    service.initialize()
    assert (await service.execute(principal(), command())).code == "ingress_storage_unavailable"

    journal.initialize_failure = None
    service.initialize()
    assert (await service.execute(principal(), command())).status is ControlStatus.CONFIRMED
    assert journal.initialized == 2


@pytest.mark.asyncio
async def test_malformed_calls_are_stable_non_reflecting_denials() -> None:
    service, auth, _, controls = ingress()
    service.initialize()
    hostile = "secret-do-not-reflect"

    invalid_principal = await service.execute(cast(CompatibilityPrincipal, hostile), command())
    invalid_command = await service.execute(principal(), cast(DecodedCommand, hostile))

    assert invalid_principal == outcome(status=ControlStatus.DENIED, code="invalid_ingress")
    assert invalid_command == invalid_principal
    assert hostile not in repr((invalid_principal, invalid_command))
    assert auth.calls == []
    assert controls.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "operation"),
    [
        (CommandKind.PAUSE, ControlOperation.PAUSE),
        (CommandKind.RESUME, ControlOperation.RESUME),
        (CommandKind.CANCEL, ControlOperation.CANCEL),
    ],
)
async def test_exact_principal_command_and_intent_are_bound_after_reservation(
    kind: CommandKind, operation: ControlOperation
) -> None:
    journal = FakeJournal()
    selected_controls = FakeControls(outcome(operation))

    def require_reserved(intent: ControlIntent) -> None:
        saved = journal.records[(PRINTER, "37")]
        assert saved.state is MqttIngressState.RESERVED
        assert saved.result is None
        assert intent == ControlIntent(
            printer_id=PRINTER,
            operation=operation,
            state_token=saved.state_token,
            idempotency_key=str(IDENTIFIER),
        )

    selected_controls.before_execute = require_reserved
    service, auth, registry, controls = ingress(controls=selected_controls, journal=journal)
    service.initialize()
    selected = principal()
    decoded = command(kind, "37", "b" * 64)

    result = await service.execute(selected, decoded)

    assert result == outcome(operation)
    assert auth.calls[0][0] == selected
    assert auth.calls[0][1] is not None
    assert registry.calls == [PRINTER]
    assert len(controls.calls) == 1
    saved, capacity = journal.reserve_calls[0]
    assert capacity == 10
    assert saved.printer_record_revision == selected.record_revision
    assert saved.sequence_id == decoded.sequence_id
    assert saved.operation is operation
    assert saved.payload_hash == decoded.payload_hash
    assert saved.idempotency_key == str(IDENTIFIER)


@pytest.mark.asyncio
async def test_revalidation_unknown_printer_and_per_printer_opt_in_fail_before_reservation() -> (
    None
):
    for selected_auth, selected_principal, selected_registry, code in (
        (
            FakeAuthenticator(False),
            principal(),
            FakeRegistry(initial_snapshot(PRINTER)),
            "access_denied",
        ),
        (
            FakeAuthenticator(),
            principal(control_enabled=False),
            FakeRegistry(initial_snapshot(PRINTER)),
            "control_disabled",
        ),
        (FakeAuthenticator(), principal(), FakeRegistry(None), "printer_unknown"),
    ):
        journal = FakeJournal()
        service, _, _, controls = ingress(
            auth=selected_auth, registry=selected_registry, journal=journal
        )
        service.initialize()
        result = await service.execute(selected_principal, command())
        assert (result.status, result.code) == (ControlStatus.DENIED, code)
        assert journal.reserve_calls == []
        assert controls.calls == []


@pytest.mark.asyncio
async def test_registry_failure_is_a_stable_non_reflecting_denial() -> None:
    selected_registry = FakeRegistry(initial_snapshot(PRINTER))
    selected_registry.failure = RuntimeError("private registry detail")
    service, _, _, controls = ingress(registry=selected_registry)
    service.initialize()

    result = await service.execute(principal(), command())
    assert (result.status, result.code) == (
        ControlStatus.DENIED,
        "printer_unavailable",
    )
    assert "private" not in repr(result)
    assert controls.calls == []


@pytest.mark.asyncio
async def test_global_control_denial_is_durably_preserved() -> None:
    denied = outcome(status=ControlStatus.DENIED, code="control_disabled")
    journal = FakeJournal()
    service, _, _, controls = ingress(controls=FakeControls(denied), journal=journal)
    service.initialize()

    assert await service.execute(principal(), command()) == denied
    assert len(controls.calls) == 1
    saved = journal.records[(PRINTER, "1")]
    assert saved.state is MqttIngressState.COMPLETE
    assert saved.result == denied


@pytest.mark.asyncio
async def test_shared_admission_gate_closes_revalidation_race() -> None:
    admissions = PrinterAdmissionGates()
    service, auth, _, controls = ingress(admissions=admissions)
    service.initialize()

    async with admissions.hold(PRINTER):
        task = asyncio.create_task(service.execute(principal(), command()))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert auth.calls == []
        assert controls.calls == []
    assert (await task).status is ControlStatus.CONFIRMED


@pytest.mark.asyncio
async def test_concurrent_exact_duplicates_share_one_dispatch_and_cleanup() -> None:
    controls = FakeControls()
    controls.release.clear()
    service, _, _, _ = ingress(controls=controls)
    service.initialize()
    selected = principal()
    decoded = command()

    first = asyncio.create_task(service.execute(selected, decoded))
    await controls.entered.wait()
    second = asyncio.create_task(service.execute(selected, decoded))
    await asyncio.sleep(0)
    controls.release.set()

    assert await first == await second == outcome()
    assert len(controls.calls) == 1
    assert service._inflight == {}


@pytest.mark.asyncio
async def test_concurrent_conflicting_duplicate_is_denied_without_reflection() -> None:
    controls = FakeControls()
    controls.release.clear()
    service, _, _, _ = ingress(controls=controls)
    service.initialize()
    first = asyncio.create_task(service.execute(principal(), command()))
    await controls.entered.wait()

    conflicts = (
        (principal(proxy_serial="hostile-secret"), command()),
        (principal(), command(CommandKind.CANCEL, digest="c" * 64)),
    )
    for conflicting_principal, conflicting_command in conflicts:
        result = await service.execute(conflicting_principal, conflicting_command)
        assert (result.status, result.code) == (
            ControlStatus.DENIED,
            "idempotency_conflict",
        )
        assert "hostile-secret" not in repr(result)

    controls.release.set()
    assert (await first).status is ControlStatus.CONFIRMED
    assert len(controls.calls) == 1


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_reserved_dispatch() -> None:
    controls = FakeControls()
    controls.release.clear()
    service, _, _, _ = ingress(controls=controls)
    service.initialize()

    waiter = asyncio.create_task(service.execute(principal(), command()))
    await controls.entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert len(service._inflight) == 1

    duplicate = asyncio.create_task(service.execute(principal(), command()))
    controls.release.set()
    assert (await duplicate).status is ControlStatus.CONFIRMED
    assert len(controls.calls) == 1
    assert service._inflight == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [MqttIngressConflictError(), MqttIngressCapacityError(), JournalError(), ValueError()],
)
async def test_reservation_failures_are_stable_denials(failure: BaseException) -> None:
    journal = FakeJournal()
    journal.reserve_failure = failure
    service, _, _, controls = ingress(journal=journal)
    service.initialize()

    result = await service.execute(principal(), command())
    expected = (
        "idempotency_conflict"
        if isinstance(failure, MqttIngressConflictError)
        else "idempotency_capacity"
        if isinstance(failure, MqttIngressCapacityError)
        else "ingress_storage_unavailable"
    )
    assert (result.status, result.code) == (ControlStatus.DENIED, expected)
    assert controls.calls == []


@pytest.mark.asyncio
async def test_lookup_storage_failure_is_a_stable_denial() -> None:
    journal = FakeJournal()
    journal.lookup_failure = JournalError("private lookup detail")
    service, _, _, controls = ingress(journal=journal)
    service.initialize()

    result = await service.execute(principal(), command())
    assert (result.status, result.code) == (
        ControlStatus.DENIED,
        "ingress_storage_unavailable",
    )
    assert "private" not in repr(result)
    assert controls.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [False, True])
async def test_reservation_race_reuses_the_exact_winner_without_dispatch(
    completed: bool,
) -> None:
    journal = FakeJournal()
    pending = MqttIngressRecord(
        printer_uuid=PRINTER,
        printer_record_revision=7,
        sequence_id="1",
        operation=ControlOperation.PAUSE,
        payload_hash="a" * 64,
        state_token=initial_snapshot(PRINTER).state_token,
        idempotency_key=str(IDENTIFIER),
        state=MqttIngressState.RESERVED,
    )
    journal.records[(PRINTER, "1")] = (
        completed_ingress(pending, outcome()) if completed else pending
    )
    journal.lookup_absent = True
    service, _, _, controls = ingress(journal=journal)
    service.initialize()

    result = await service.execute(principal(), command())
    assert result == (
        outcome()
        if completed
        else outcome(status=ControlStatus.OUTCOME_UNKNOWN, code="outcome_unknown")
    )
    assert controls.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["shape", "exception"])
async def test_id_factory_fault_is_storage_denial(failure: str) -> None:
    def failed_identifier() -> uuid.UUID:
        raise RuntimeError("private identity detail")

    factory = cast(Callable[[], uuid.UUID], lambda: 7) if failure == "shape" else failed_identifier
    service, _, _, controls = ingress(id_factory=factory)
    service.initialize()

    result = await service.execute(principal(), command())
    assert (result.status, result.code) == (
        ControlStatus.DENIED,
        "ingress_storage_unavailable",
    )
    assert "private" not in repr(result)
    assert controls.calls == []


@pytest.mark.asyncio
async def test_control_cancellation_and_completion_loss_are_unknown() -> None:
    for control_result, completion_failure in (
        (asyncio.CancelledError(), None),
        (RuntimeError("private dispatch detail"), None),
        (outcome(), JournalError("private journal detail")),
    ):
        journal = FakeJournal()
        journal.complete_failure = completion_failure
        service, _, _, _ = ingress(controls=FakeControls(control_result), journal=journal)
        service.initialize()

        result = await service.execute(principal(), command())
        assert result == outcome(status=ControlStatus.OUTCOME_UNKNOWN, code="outcome_unknown")
        assert "private" not in repr(result)
        if completion_failure is None:
            assert journal.records[(PRINTER, "1")].state is MqttIngressState.OUTCOME_UNKNOWN


@pytest.mark.asyncio
async def test_missing_completed_result_is_unknown() -> None:
    journal = FakeJournal()
    journal.complete_without_result = True
    service, _, _, _ = ingress(journal=journal)
    service.initialize()

    assert await service.execute(principal(), command()) == outcome(
        status=ControlStatus.OUTCOME_UNKNOWN, code="outcome_unknown"
    )


@pytest.mark.asyncio
async def test_sequential_and_restarted_duplicate_reuses_result_without_redispatch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mqtt.sqlite3"
    journal = MqttIngressJournal(path)
    controls = FakeControls()
    service, _, _, _ = ingress(journal=journal, controls=controls)
    service.initialize()

    first = await service.execute(principal(), command())
    second = await service.execute(principal(), command())
    restarted_controls = FakeControls()
    restarted, _, _, _ = ingress(journal=MqttIngressJournal(path), controls=restarted_controls)
    restarted.initialize()
    third = await restarted.execute(principal(), command())

    assert first == second == third == outcome()
    assert len(controls.calls) == 1
    assert restarted_controls.calls == []


@pytest.mark.asyncio
async def test_exact_duplicate_reuses_prior_result_before_reading_changed_state(
    tmp_path: Path,
) -> None:
    journal = MqttIngressJournal(tmp_path / "mqtt.sqlite3")
    selected_registry = FakeRegistry(initial_snapshot(PRINTER))
    controls = FakeControls()
    service, _, _, _ = ingress(journal=journal, registry=selected_registry, controls=controls)
    service.initialize()

    first = await service.execute(principal(), command())
    selected_registry.failure = RuntimeError("new state must not affect a duplicate")
    duplicate = await service.execute(principal(), command())

    assert first == duplicate == outcome()
    assert selected_registry.calls == [PRINTER]
    assert len(controls.calls) == 1


@pytest.mark.asyncio
async def test_prior_sequence_rejects_a_different_authenticated_record_revision(
    tmp_path: Path,
) -> None:
    journal = MqttIngressJournal(tmp_path / "mqtt.sqlite3")
    controls = FakeControls()
    service, _, _, _ = ingress(journal=journal, controls=controls)
    service.initialize()
    assert (await service.execute(principal(), command())).status is ControlStatus.CONFIRMED

    conflict = await service.execute(principal(record_revision=8), command())
    assert (conflict.status, conflict.code) == (
        ControlStatus.DENIED,
        "idempotency_conflict",
    )
    assert len(controls.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        command(CommandKind.CANCEL),
        command(digest="d" * 64),
        command(sequence="01"),
    ],
)
async def test_sequential_conflicts_and_distinct_sequence_are_bound_exactly(
    tmp_path: Path, changed: DecodedCommand
) -> None:
    journal = MqttIngressJournal(tmp_path / "mqtt.sqlite3")
    controls = FakeControls()
    identifiers = iter(
        (
            IDENTIFIER,
            uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        )
    )
    service, _, _, _ = ingress(
        journal=journal, controls=controls, id_factory=lambda: next(identifiers)
    )
    service.initialize()
    assert (await service.execute(principal(), command())).status is ControlStatus.CONFIRMED

    result = await service.execute(principal(), changed)
    if changed.sequence_id == "1":
        assert (result.status, result.code) == (
            ControlStatus.DENIED,
            "idempotency_conflict",
        )
        assert len(controls.calls) == 1
    else:
        assert result.status is ControlStatus.CONFIRMED
        assert len(controls.calls) == 2


@pytest.mark.asyncio
async def test_restarted_reserved_request_is_unknown_without_dispatch(tmp_path: Path) -> None:
    path = tmp_path / "mqtt.sqlite3"
    journal = MqttIngressJournal(path)
    journal.initialize()
    current = initial_snapshot(PRINTER)
    pending = MqttIngressRecord(
        printer_uuid=PRINTER,
        printer_record_revision=7,
        sequence_id="1",
        operation=ControlOperation.PAUSE,
        payload_hash="a" * 64,
        state_token=current.state_token,
        idempotency_key=str(IDENTIFIER),
        state=MqttIngressState.RESERVED,
    )
    journal.reserve(pending, capacity=10)
    controls = FakeControls()
    restarted, _, _, _ = ingress(
        registry=FakeRegistry(current),
        controls=controls,
        journal=MqttIngressJournal(path),
    )
    restarted.initialize()

    result = await restarted.execute(principal(), command())
    assert result.status is ControlStatus.OUTCOME_UNKNOWN
    assert controls.calls == []


@pytest.mark.asyncio
async def test_completed_record_without_result_is_conservatively_unknown() -> None:
    journal = FakeJournal()
    pending = MqttIngressRecord(
        printer_uuid=PRINTER,
        printer_record_revision=7,
        sequence_id="1",
        operation=ControlOperation.PAUSE,
        payload_hash="a" * 64,
        state_token=initial_snapshot(PRINTER).state_token,
        idempotency_key=str(IDENTIFIER),
        state=MqttIngressState.RESERVED,
    )
    journal.records[(PRINTER, "1")] = pending
    service, _, _, controls = ingress(journal=journal)
    service.initialize()

    assert (await service.execute(principal(), command())).status is ControlStatus.OUTCOME_UNKNOWN
    assert controls.calls == []


@pytest.mark.asyncio
async def test_inflight_cleanup_does_not_remove_a_replacement_entry() -> None:
    controls = FakeControls()
    controls.release.clear()
    service, _, _, _ = ingress(controls=controls)
    service.initialize()
    task = asyncio.create_task(service.execute(principal(), command()))
    await controls.entered.wait()
    key = (PRINTER, "1")
    replacement_task = asyncio.create_task(asyncio.sleep(0, result=outcome()))
    service._inflight[key] = _InflightRequest(principal(), command(), replacement_task)
    controls.release.set()

    assert (await task).status is ControlStatus.CONFIRMED
    assert service._inflight[key].task is replacement_task
    await replacement_task
    service._inflight.clear()
