from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import klove.orchestration.start as start_module
from klove.config import PrinterConfig
from klove.domain.models import JobIdentitySnapshot, PrinterPhase
from klove.domain.start import (
    StartBoundary,
    StartFailure,
    StartFailureCode,
    StartJournalRecord,
    StartJournalState,
    StartObservation,
    StartOperationResult,
    StartReconciliationDecision,
    StartState,
)
from klove.domain.upload import MoonrakerGcodeMetadata, RemoteFileDigest, VerifiedUpload
from klove.errors import JournalError, StartTransportError
from klove.orchestration.start import StartService
from klove.persistence.start_journal import JournalConflictError, StartJournal

from ..start_helpers import (
    PATH,
    history_job,
    observation,
    preflight,
    printer_config,
    safety_profile,
    verified_upload,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay


class FakeTransport:
    def __init__(self, verified: tuple[VerifiedUpload, ...] = ()) -> None:
        values = verified or (verified_upload(),)
        self.files = {item.path: item for item in values}
        self.metadata_values: list[MoonrakerGcodeMetadata | Exception | None] = []
        self.download_values: list[RemoteFileDigest | Exception] = []
        self.query_values: list[StartObservation | Exception] = []
        self.query_default: StartObservation | Exception = observation(
            phase=PrinterPhase.IDLE,
            filename="",
            file_position=0,
            latest_job=None,
        )
        self.dispatches: list[str] = []
        self.dispatch_error = False
        self.dispatch_entered = asyncio.Event()
        self.release_dispatch = asyncio.Event()
        self.release_dispatch.set()
        self.query_entered = asyncio.Event()
        self.release_query = asyncio.Event()
        self.release_query.set()

    async def metadata(self, path: str) -> MoonrakerGcodeMetadata | None:
        if self.metadata_values:
            value = self.metadata_values.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        return self.files[path].metadata

    async def download(self, path: str, _expected_size: int) -> RemoteFileDigest:
        if self.download_values:
            value = self.download_values.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        return self.files[path].remote_file

    async def query(self) -> StartObservation:
        self.query_entered.set()
        await self.release_query.wait()
        value = self.query_values.pop(0) if self.query_values else self.query_default
        if isinstance(value, Exception):
            raise value
        return value

    async def dispatch(self, path: str) -> None:
        self.dispatch_entered.set()
        await self.release_dispatch.wait()
        self.dispatches.append(path)
        if self.dispatch_error:
            raise StartTransportError


def journal(tmp_path: Path) -> StartJournal:
    return StartJournal(tmp_path / "start-journal.sqlite3")


def make_service(  # noqa: PLR0913 -- compact explicit service fixture.
    tmp_path: Path,
    transport: FakeTransport,
    *,
    store: StartJournal | None = None,
    enabled: bool = True,
    configured_printer: PrinterConfig | None = None,
    clock: FakeClock | None = None,
) -> StartService:
    actual_clock = clock or FakeClock()
    return StartService(
        configured_printer or printer_config(),
        transport,
        store or journal(tmp_path),
        enabled=enabled,
        confirmation_timeout_seconds=0.2,
        poll_interval_seconds=0.1,
        clock=actual_clock,
        sleep=actual_clock.sleep,
    )


def idle(*, prior: JobIdentitySnapshot | None = None) -> StartObservation:
    return observation(
        eventtime=10,
        phase=PrinterPhase.IDLE,
        filename="previous.gcode" if prior is not None else "",
        file_position=0,
        latest_job=prior,
    )


def assert_failure(
    result: StartOperationResult,
    state: StartState,
    boundary: StartBoundary,
    code: StartFailureCode,
) -> None:
    assert result.state is state
    assert result.failure == StartFailure(boundary=boundary, code=code)
    assert result.confirmation is None


@pytest.mark.asyncio
async def test_one_start_is_confirmed_and_exact_duplicate_survives_restart(
    tmp_path: Path,
) -> None:
    verified = verified_upload()
    transport = FakeTransport()
    transport.query_values = [idle(), observation()]
    store = journal(tmp_path)
    service = make_service(tmp_path, transport, store=store)
    await service.initialize()

    first = await service.execute(verified)
    duplicate = await service.execute(verified)

    assert first == duplicate
    assert first.state is StartState.CONFIRMED
    assert first.confirmation is not None
    assert first.confirmation.path == PATH
    assert transport.dispatches == [PATH]

    restarted_transport = FakeTransport()
    restarted = make_service(tmp_path, restarted_transport, store=StartJournal(store._path))
    await restarted.initialize()
    restored = await restarted.execute(verified)
    assert restored == first
    assert restarted_transport.dispatches == []
    assert restarted_transport.query_values == []


@pytest.mark.asyncio
async def test_lost_start_response_reconciles_without_retry(tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.dispatch_error = True
    transport.query_values = [idle(), observation()]
    service = make_service(tmp_path, transport)
    await service.initialize()

    result = await service.execute(verified_upload())

    assert result.state is StartState.CONFIRMED
    assert transport.dispatches == [PATH]


@pytest.mark.asyncio
async def test_concurrent_duplicate_shares_one_dispatch_and_survives_caller_cancel(
    tmp_path: Path,
) -> None:
    transport = FakeTransport()
    transport.query_values = [idle(), observation()]
    transport.release_dispatch.clear()
    service = make_service(tmp_path, transport)
    await service.initialize()

    first = asyncio.create_task(service.execute(verified_upload()))
    await transport.dispatch_entered.wait()
    duplicate = asyncio.create_task(service.execute(verified_upload()))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    transport.release_dispatch.set()

    assert (await duplicate).state is StartState.CONFIRMED
    assert transport.dispatches == [PATH]


@pytest.mark.asyncio
async def test_distinct_starts_are_serialized_for_one_printer(tmp_path: Path) -> None:
    first_upload = verified_upload()
    second_upload = verified_upload(
        operation_id="55555555-5555-4555-8555-555555555555",
        idempotency_key="66666666-6666-4666-8666-666666666666",
    )
    first_job = history_job()
    second_job = history_job(
        job_id="000003",
        filename=second_upload.path,
        start_time=1_700_000_200,
    )
    transport = FakeTransport((first_upload, second_upload))
    transport.query_values = [
        idle(),
        observation(latest_job=first_job),
        idle(prior=first_job),
        observation(filename=second_upload.path, latest_job=second_job),
    ]
    transport.release_dispatch.clear()
    service = make_service(tmp_path, transport)
    await service.initialize()

    first = asyncio.create_task(service.execute(first_upload))
    await transport.dispatch_entered.wait()
    second = asyncio.create_task(service.execute(second_upload))
    await asyncio.sleep(0)
    assert transport.dispatches == []
    transport.release_dispatch.set()

    assert (await first).state is StartState.CONFIRMED
    assert (await second).state is StartState.CONFIRMED
    assert transport.dispatches == [first_upload.path, second_upload.path]


@pytest.mark.asyncio
async def test_durable_reservation_serializes_separate_service_instances(
    tmp_path: Path,
) -> None:
    first_upload = verified_upload()
    second_upload = verified_upload(
        operation_id="55555555-5555-4555-8555-555555555555",
        idempotency_key="66666666-6666-4666-8666-666666666666",
    )
    first_transport = FakeTransport((first_upload,))
    second_transport = FakeTransport((second_upload,))
    for index, (transport, upload) in enumerate(
        ((first_transport, first_upload), (second_transport, second_upload)),
        start=2,
    ):
        transport.query_values = [
            idle(),
            observation(
                filename=upload.path,
                latest_job=history_job(
                    job_id=f"00000{index}",
                    filename=upload.path,
                    start_time=1_700_000_000 + index,
                ),
            ),
        ]
        transport.release_query.clear()
        transport.release_dispatch.clear()
    path = journal(tmp_path)._path
    first_service = make_service(tmp_path, first_transport, store=StartJournal(path))
    second_service = make_service(tmp_path, second_transport, store=StartJournal(path))
    await first_service.initialize()
    await second_service.initialize()

    first = asyncio.create_task(first_service.execute(first_upload))
    second = asyncio.create_task(second_service.execute(second_upload))
    await first_transport.query_entered.wait()
    await second_transport.query_entered.wait()
    first_transport.release_query.set()
    second_transport.release_query.set()
    entered = {
        asyncio.create_task(first_transport.dispatch_entered.wait()): (first, first_transport),
        asyncio.create_task(second_transport.dispatch_entered.wait()): (second, second_transport),
    }
    completed, waiting = await asyncio.wait(entered, return_when=asyncio.FIRST_COMPLETED)
    assert len(completed) == 1
    winner_task, winner_transport = entered[next(iter(completed))]
    loser_task = second if winner_task is first else first
    loser_transport = second_transport if winner_transport is first_transport else first_transport

    loser = await asyncio.wait_for(loser_task, timeout=1)
    assert_failure(
        loser,
        StartState.OUTCOME_UNKNOWN,
        StartBoundary.RECONCILIATION,
        StartFailureCode.PRINTER_FENCED,
    )
    assert loser_transport.dispatches == []
    winner_transport.release_dispatch.set()
    assert (await winner_task).state is StartState.CONFIRMED
    assert sum(len(transport.dispatches) for transport in (first_transport, second_transport)) == 1
    for waiter in waiting:
        waiter.cancel()
    await asyncio.gather(*waiting, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("collision", ["operation", "key"])
async def test_in_memory_operation_or_key_collision_is_denied(
    tmp_path: Path, collision: str
) -> None:
    transport = FakeTransport()
    transport.query_values = [idle(), observation()]
    transport.release_dispatch.clear()
    service = make_service(tmp_path, transport)
    await service.initialize()
    first = asyncio.create_task(service.execute(verified_upload()))
    await transport.dispatch_entered.wait()
    conflicting = (
        verified_upload(idempotency_key="55555555-5555-4555-8555-555555555555")
        if collision == "operation"
        else verified_upload(operation_id="55555555-5555-4555-8555-555555555555")
    )

    result = await service.execute(conflicting)
    transport.release_dispatch.set()
    await first

    assert_failure(
        result,
        StartState.DENIED,
        StartBoundary.IDEMPOTENCY,
        StartFailureCode.IDEMPOTENCY_CONFLICT,
    )
    assert transport.dispatches == [PATH]


@pytest.mark.asyncio
async def test_dispatch_requires_both_enablement_and_completed_initialization(
    tmp_path: Path,
) -> None:
    transport = FakeTransport()
    service = make_service(tmp_path, transport, enabled=False)
    before_init = await service.execute(verified_upload())
    await service.initialize()
    disabled = await service.execute(
        verified_upload(
            operation_id="55555555-5555-4555-8555-555555555555",
            idempotency_key="66666666-6666-4666-8666-666666666666",
        )
    )

    assert_failure(
        before_init,
        StartState.DENIED,
        StartBoundary.TARGET,
        StartFailureCode.DISPATCH_DISABLED,
    )
    assert_failure(
        disabled,
        StartState.DENIED,
        StartBoundary.TARGET,
        StartFailureCode.DISPATCH_DISABLED,
    )
    assert transport.dispatches == []

    per_printer_disabled = make_service(
        tmp_path / "per-printer",
        transport,
        configured_printer=printer_config().model_copy(update={"dispatch_enabled": False}),
    )
    (tmp_path / "per-printer").mkdir()
    await per_printer_disabled.initialize()
    denied = await per_printer_disabled.execute(
        verified_upload(
            operation_id="77777777-7777-4777-8777-777777777777",
            idempotency_key="88888888-8888-4888-8888-888888888888",
        )
    )
    assert_failure(
        denied,
        StartState.DENIED,
        StartBoundary.TARGET,
        StartFailureCode.DISPATCH_DISABLED,
    )


@pytest.mark.asyncio
async def test_reconciliation_gate_closes_during_reconnect(tmp_path: Path) -> None:
    verified = verified_upload()
    store = journal(tmp_path)
    store.initialize()
    store.reserve(
        StartJournalRecord(
            operation_id=verified.qualification.operation_id,
            idempotency_key=verified.qualification.idempotency_key,
            printer_uuid=verified.qualification.target.printer_uuid,
            verified=verified,
            preflight=preflight(),
            state=StartJournalState.DISPATCHING,
        )
    )
    transport = FakeTransport()
    transport.release_query.clear()
    service = make_service(tmp_path, transport, store=store)
    reconnect = asyncio.create_task(service.initialize())
    await transport.query_entered.wait()
    other = verified_upload(
        operation_id="55555555-5555-4555-8555-555555555555",
        idempotency_key="66666666-6666-4666-8666-666666666666",
    )

    result = await service.execute(other)
    transport.release_query.set()
    await reconnect

    assert_failure(
        result,
        StartState.DENIED,
        StartBoundary.RECONCILIATION,
        StartFailureCode.RECONCILIATION_PENDING,
    )


@pytest.mark.asyncio
async def test_timeout_is_durable_unknown_and_restart_can_later_confirm(
    tmp_path: Path,
) -> None:
    verified = verified_upload()
    clock = FakeClock()
    transport = FakeTransport()
    transport.query_values = [idle()]
    transport.query_default = idle()
    store = journal(tmp_path)
    service = make_service(tmp_path, transport, store=store, clock=clock)
    await service.initialize()
    unknown = await service.execute(verified)
    assert_failure(
        unknown,
        StartState.OUTCOME_UNKNOWN,
        StartBoundary.TRANSPORT,
        StartFailureCode.CONFIRMATION_TIMEOUT,
    )
    assert transport.dispatches == [PATH]

    restarted_transport = FakeTransport()
    restarted_transport.query_values = [observation()]
    restarted = make_service(
        tmp_path,
        restarted_transport,
        store=StartJournal(store._path),
    )
    await restarted.initialize()
    confirmed = await restarted.execute(verified)
    assert confirmed.state is StartState.CONFIRMED
    assert restarted_transport.dispatches == []


@pytest.mark.asyncio
async def test_unresolved_restart_fences_new_operations_without_dispatch(tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.query_values = [idle()]
    transport.query_default = idle()
    store = journal(tmp_path)
    first_service = make_service(tmp_path, transport, store=store)
    await first_service.initialize()
    await first_service.execute(verified_upload())

    restarted_transport = FakeTransport()
    restarted_transport.query_default = idle()
    restarted = make_service(
        tmp_path,
        restarted_transport,
        store=StartJournal(store._path),
    )
    await restarted.initialize()
    other = verified_upload(
        operation_id="55555555-5555-4555-8555-555555555555",
        idempotency_key="66666666-6666-4666-8666-666666666666",
    )
    result = await restarted.execute(other)

    assert_failure(
        result,
        StartState.OUTCOME_UNKNOWN,
        StartBoundary.RECONCILIATION,
        StartFailureCode.PRINTER_FENCED,
    )
    assert restarted_transport.dispatches == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", ["printer", "missing_profile", "generation"])
async def test_current_target_is_rechecked_immediately_before_reservation(
    tmp_path: Path, stale: str
) -> None:
    verified = verified_upload()
    configured = printer_config()
    if stale == "printer":
        verified = verified.model_copy(
            update={
                "qualification": verified.qualification.model_copy(
                    update={
                        "target": verified.qualification.target.model_copy(
                            update={"printer_uuid": "77777777-7777-4777-8777-777777777777"}
                        )
                    }
                )
            }
        )
    elif stale == "missing_profile":
        configured = printer_config(profiles=())
    else:
        configured = printer_config(profiles=(safety_profile(generation=8),))
    transport = FakeTransport((verified,))
    service = make_service(tmp_path, transport, configured_printer=configured)
    await service.initialize()

    result = await service.execute(verified)

    assert_failure(
        result,
        StartState.DENIED,
        StartBoundary.TARGET,
        StartFailureCode.TARGET_STALE,
    )
    assert transport.dispatches == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch", ["metadata_before", "nozzle", "digest", "metadata_after", "error"]
)
async def test_remote_file_is_reverified_before_every_start(tmp_path: Path, mismatch: str) -> None:
    verified = verified_upload()
    transport = FakeTransport()
    if mismatch == "metadata_before":
        transport.metadata_values = [None]
    elif mismatch == "nozzle":
        transport.metadata_values = [
            verified.metadata.model_copy(update={"nozzle_diameter_micrometres": 600})
        ]
    elif mismatch == "digest":
        transport.download_values = [
            verified.remote_file.model_copy(update={"sha256": "sha256:" + "0" * 64})
        ]
    elif mismatch == "metadata_after":
        transport.metadata_values = [
            verified.metadata,
            verified.metadata.model_copy(
                update={"metadata_uuid": "77777777-7777-4777-8777-777777777777"}
            ),
        ]
    else:
        transport.metadata_values = [StartTransportError()]
    service = make_service(tmp_path, transport)
    await service.initialize()

    result = await service.execute(verified)

    assert_failure(
        result,
        StartState.DENIED,
        StartBoundary.REMOTE_FILE,
        StartFailureCode.REMOTE_MISMATCH,
    )
    assert transport.dispatches == []


@pytest.mark.asyncio
@pytest.mark.parametrize("preflight", ["unavailable", "active", "same_path_history"])
async def test_direct_preflight_requires_idle_without_prior_operation_path(
    tmp_path: Path, preflight: str
) -> None:
    transport = FakeTransport()
    if preflight == "unavailable":
        transport.query_values = [StartTransportError()]
    elif preflight == "active":
        transport.query_values = [observation()]
    else:
        transport.query_values = [
            idle(prior=history_job(job_id="000001", start_time=1_700_000_050))
        ]
    service = make_service(tmp_path, transport)
    await service.initialize()

    result = await service.execute(verified_upload())

    assert_failure(
        result,
        StartState.DENIED,
        StartBoundary.PRINTER_STATE,
        StartFailureCode.PRINTER_NOT_IDLE,
    )
    assert transport.dispatches == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["query", "ambiguous"])
async def test_post_dispatch_transport_failure_or_contradiction_is_durable_unknown(
    tmp_path: Path, failure: str
) -> None:
    transport = FakeTransport()
    transport.query_values = [idle()]
    if failure == "query":
        transport.query_default = StartTransportError()
    else:
        transport.query_default = observation(
            filename="other.gcode",
            latest_job=history_job(filename="other.gcode"),
        )
    service = make_service(tmp_path, transport)
    await service.initialize()

    result = await service.execute(verified_upload())

    assert_failure(
        result,
        StartState.OUTCOME_UNKNOWN,
        StartBoundary.TRANSPORT,
        StartFailureCode.TRANSPORT_AMBIGUOUS,
    )
    assert transport.dispatches == [PATH]


@pytest.mark.asyncio
async def test_journal_creation_failure_blocks_dispatch(tmp_path: Path) -> None:
    transport = FakeTransport()
    service = make_service(
        tmp_path,
        transport,
        store=StartJournal(tmp_path / "missing" / "journal.sqlite3"),
    )
    await service.initialize()

    result = await service.execute(verified_upload())

    assert_failure(
        result,
        StartState.DENIED,
        StartBoundary.JOURNAL,
        StartFailureCode.JOURNAL_UNAVAILABLE,
    )
    assert transport.dispatches == []


@pytest.mark.asyncio
async def test_persisted_collision_and_dispatching_duplicate_never_dispatch(
    tmp_path: Path,
) -> None:
    verified = verified_upload()
    store = journal(tmp_path)
    store.initialize()
    pending = StartJournalRecord(
        operation_id=verified.qualification.operation_id,
        idempotency_key=verified.qualification.idempotency_key,
        printer_uuid=verified.qualification.target.printer_uuid,
        verified=verified,
        preflight=preflight(),
        state=StartJournalState.DISPATCHING,
    )
    store.reserve(pending)
    duplicate_service = make_service(tmp_path, FakeTransport(), store=store)
    duplicate_service._ready = True

    duplicate = await duplicate_service.execute(verified)
    conflict_service = make_service(tmp_path, FakeTransport(), store=store)
    conflict_service._ready = True
    conflict = await conflict_service.execute(
        verified_upload(idempotency_key="55555555-5555-4555-8555-555555555555")
    )

    assert_failure(
        duplicate,
        StartState.OUTCOME_UNKNOWN,
        StartBoundary.TRANSPORT,
        StartFailureCode.TRANSPORT_AMBIGUOUS,
    )
    assert_failure(
        conflict,
        StartState.DENIED,
        StartBoundary.IDEMPOTENCY,
        StartFailureCode.IDEMPOTENCY_CONFLICT,
    )


@pytest.mark.asyncio
async def test_internal_cancellation_after_reservation_never_retries(tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.query_values = [idle()]
    transport.release_dispatch.clear()
    service = make_service(tmp_path, transport)
    await service.initialize()
    caller = asyncio.create_task(service.execute(verified_upload()))
    await transport.dispatch_entered.wait()
    entry = service._by_key[verified_upload().qualification.idempotency_key]

    entry.task.cancel()
    result = await caller

    assert_failure(
        result,
        StartState.OUTCOME_UNKNOWN,
        StartBoundary.TRANSPORT,
        StartFailureCode.TRANSPORT_AMBIGUOUS,
    )
    assert transport.dispatches == []
    records = service._journal.unresolved(printer_config().uuid)
    assert len(records) == 1
    assert records[0].state is StartJournalState.DISPATCHING


@pytest.mark.asyncio
async def test_internal_cancellation_before_reservation_is_a_denial(tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.release_query.clear()
    service = make_service(tmp_path, transport)
    await service.initialize()
    caller = asyncio.create_task(service.execute(verified_upload()))
    await transport.query_entered.wait()
    entry = service._by_key[verified_upload().qualification.idempotency_key]

    entry.task.cancel()
    result = await caller

    assert_failure(
        result,
        StartState.DENIED,
        StartBoundary.INTERNAL,
        StartFailureCode.INTERNAL_FAILURE,
    )
    assert service._journal.unresolved(printer_config().uuid) == ()


@pytest.mark.asyncio
async def test_unexpected_failure_after_reservation_is_unknown_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeTransport()
    transport.query_values = [idle()]
    service = make_service(tmp_path, transport)
    await service.initialize()

    async def broken_confirm(_record: StartJournalRecord) -> StartOperationResult:
        raise RuntimeError("internal detail")

    monkeypatch.setattr(service, "_confirm", broken_confirm)
    result = await service.execute(verified_upload())

    assert_failure(
        result,
        StartState.OUTCOME_UNKNOWN,
        StartBoundary.INTERNAL,
        StartFailureCode.INTERNAL_FAILURE,
    )
    assert transport.dispatches == [PATH]
    assert len(service._journal.unresolved(printer_config().uuid)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "lookup",
        "lookup_conflict",
        "unresolved",
        "reserve",
        "reserve_conflict",
        "mark_confirmed",
        "mark_unknown",
    ],
)
async def test_journal_errors_at_each_boundary_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    verified = verified_upload()
    transport = FakeTransport()
    transport.query_values = [idle(), observation()]
    if case == "mark_unknown":
        transport.query_values = [
            idle(),
            observation(
                filename="other.gcode",
                latest_job=history_job(filename="other.gcode"),
            ),
        ]
    store = journal(tmp_path)
    service = make_service(tmp_path, transport, store=store)
    await service.initialize()

    error: JournalError = JournalConflictError() if case.endswith("conflict") else JournalError()

    def broken(*_args: object) -> object:
        raise error

    method = case.removesuffix("_conflict")
    monkeypatch.setattr(store, method, broken)
    result = await service.execute(verified)

    if case.endswith("conflict"):
        assert_failure(
            result,
            StartState.DENIED,
            StartBoundary.IDEMPOTENCY,
            StartFailureCode.IDEMPOTENCY_CONFLICT,
        )
    elif case in {"mark_confirmed", "mark_unknown"}:
        assert_failure(
            result,
            StartState.OUTCOME_UNKNOWN,
            StartBoundary.JOURNAL,
            StartFailureCode.JOURNAL_UNAVAILABLE,
        )
    else:
        assert_failure(
            result,
            StartState.DENIED,
            StartBoundary.JOURNAL,
            StartFailureCode.JOURNAL_UNAVAILABLE,
        )
    assert transport.dispatches == (
        [verified.path] if case in {"mark_confirmed", "mark_unknown"} else []
    )


@pytest.mark.asyncio
async def test_concurrent_reservation_returns_existing_without_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeTransport()
    transport.query_values = [idle()]
    store = journal(tmp_path)
    service = make_service(tmp_path, transport, store=store)
    await service.initialize()

    def existing(record: StartJournalRecord) -> tuple[StartJournalRecord, bool]:
        return record, False

    monkeypatch.setattr(store, "reserve", existing)
    result = await service.execute(verified_upload())

    assert_failure(
        result,
        StartState.OUTCOME_UNKNOWN,
        StartBoundary.TRANSPORT,
        StartFailureCode.TRANSPORT_AMBIGUOUS,
    )
    assert transport.dispatches == []


@pytest.mark.asyncio
async def test_reconnect_journal_failures_keep_the_dispatch_gate_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = journal(tmp_path)
    service = make_service(tmp_path, FakeTransport(), store=store)

    def unavailable(_printer_uuid: str) -> tuple[StartJournalRecord, ...]:
        raise JournalError

    monkeypatch.setattr(store, "unresolved", unavailable)
    await service.initialize()
    await service.reconcile_after_reconnect()
    result = await service.execute(verified_upload())

    assert_failure(
        result,
        StartState.DENIED,
        StartBoundary.JOURNAL,
        StartFailureCode.JOURNAL_UNAVAILABLE,
    )


@pytest.mark.asyncio
async def test_reconnect_stops_when_confirmation_cannot_be_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified = verified_upload()
    store = journal(tmp_path)
    store.initialize()
    store.reserve(
        StartJournalRecord(
            operation_id=verified.qualification.operation_id,
            idempotency_key=verified.qualification.idempotency_key,
            printer_uuid=verified.qualification.target.printer_uuid,
            verified=verified,
            preflight=preflight(),
            state=StartJournalState.DISPATCHING,
        )
    )
    transport = FakeTransport()
    transport.query_values = [observation()]
    service = make_service(tmp_path, transport, store=store)

    def unavailable(*_args: object) -> object:
        raise JournalError

    monkeypatch.setattr(store, "mark_confirmed", unavailable)
    await service.initialize()
    result = await service.execute(verified)

    assert_failure(
        result,
        StartState.DENIED,
        StartBoundary.JOURNAL,
        StartFailureCode.JOURNAL_UNAVAILABLE,
    )


@pytest.mark.asyncio
async def test_dispatch_gate_is_rechecked_after_waiting_for_printer_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service(tmp_path, FakeTransport())
    await service.initialize()
    states = iter((True, False))

    def gate() -> bool:
        return next(states)

    monkeypatch.setattr(service, "_dispatch_ready", gate)

    result = await service.execute(verified_upload())
    assert_failure(
        result,
        StartState.DENIED,
        StartBoundary.RECONCILIATION,
        StartFailureCode.RECONCILIATION_PENDING,
    )


@pytest.mark.asyncio
async def test_defensive_impossible_confirmation_and_unexpected_error_are_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeTransport()
    transport.query_values = [idle(), observation()]
    service = make_service(tmp_path, transport)
    await service.initialize()

    def impossible(*_args: object) -> tuple[object, None]:
        return StartReconciliationDecision.CONFIRMED, None

    monkeypatch.setattr(start_module, "reconcile_start", impossible)
    result = await service.execute(verified_upload())
    assert_failure(
        result,
        StartState.OUTCOME_UNKNOWN,
        StartBoundary.JOURNAL,
        StartFailureCode.JOURNAL_UNAVAILABLE,
    )

    other = verified_upload(
        operation_id="55555555-5555-4555-8555-555555555555",
        idempotency_key="66666666-6666-4666-8666-666666666666",
    )
    broken_transport = FakeTransport((other,))

    async def broken_metadata(_path: str) -> MoonrakerGcodeMetadata | None:
        raise RuntimeError("internal detail")

    monkeypatch.setattr(broken_transport, "metadata", broken_metadata)
    broken_service = make_service(tmp_path / "other", broken_transport)
    (tmp_path / "other").mkdir(mode=0o700)
    await broken_service.initialize()
    unexpected = await broken_service.execute(other)
    assert_failure(
        unexpected,
        StartState.DENIED,
        StartBoundary.INTERNAL,
        StartFailureCode.INTERNAL_FAILURE,
    )
    assert "internal detail" not in unexpected.model_dump_json()
