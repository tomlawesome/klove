from __future__ import annotations

import asyncio
import base64
import itertools
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import SecretStr, ValidationError

import klove.orchestration.onboarding as onboarding_module
from klove.adapters.moonraker.onboarding import MoonrakerProbeError
from klove.domain.onboarding import (
    MoonrakerEndpoint,
    PrinterIdentityEvidence,
    PrinterLifecycle,
    RegisteredPrinter,
    RegistryOperationKind,
    RegistryOperationRecord,
    RegistryOperationState,
)
from klove.domain.onboarding_requests import (
    CreatePrinterRequest,
    DisablePrinterRequest,
    InspectPrinterRequest,
    LifecycleResultRequest,
    RemovePrinterRequest,
    RotateCompatibilityCredentialRequest,
    RotateMoonrakerCredentialRequest,
    UpdatePrinterRequest,
)
from klove.orchestration.admission import PrinterAdmissionGates
from klove.orchestration.onboarding import (
    CompositeActuatorFenceInspector,
    FenceInspectionError,
    LifecycleFailureCode,
    LifecycleServiceError,
    PrinterLifecycleService,
    _required_reference,
    _same_request,
    _service_error,
    _updated_printer,
)
from klove.persistence.printer_registry import (
    PrinterStore,
    RegistryBusyError,
    RegistryConflictError,
    RegistryStoreError,
    RegistryTransitionError,
)
from klove.persistence.secret_store import SecretStore, SecretStoreError

from ..onboarding_helpers import (
    IDEMPOTENCY_KEY,
    OTHER_PRINTER_UUID,
    PRINTER_UUID,
    identity,
    make_stores,
    operation,
    printer,
    safety_profile,
)

ENDPOINT = MoonrakerEndpoint(url="http://127.0.0.1:7125", verify_tls=False)
MOONRAKER_CREDENTIAL = "m" * 32
NEW_MOONRAKER_CREDENTIAL = "n" * 32


def request_key(index: int) -> str:
    return f"{index:08x}-0000-4000-8000-{index:012x}"


def evidence(index: int = 0) -> PrinterIdentityEvidence:
    return identity().model_copy(
        update={
            "server_hostname": f"192.0.2.{index + 1}",
            "observed_at_unix_ms": 900 + index,
        }
    )


class FakeProbe:
    def __init__(self, results: Iterable[PrinterIdentityEvidence | Exception] = ()) -> None:
        self.results = list(results)
        self.calls: list[tuple[MoonrakerEndpoint, str]] = []

    async def probe(
        self,
        endpoint: MoonrakerEndpoint,
        api_key: str,
    ) -> PrinterIdentityEvidence:
        self.calls.append((endpoint, api_key))
        result = self.results.pop(0) if self.results else evidence()
        if isinstance(result, Exception):
            raise result
        return result


class BlockingProbe:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def probe(
        self,
        _endpoint: MoonrakerEndpoint,
        _api_key: str,
    ) -> PrinterIdentityEvidence:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return evidence()


class FakeFences:
    def __init__(self, responses: Iterable[object] = ()) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    async def clear(self, printer_uuid: str) -> bool:
        self.calls.append(printer_uuid)
        result = self.responses.pop(0) if self.responses else True
        if isinstance(result, Exception):
            raise result
        return cast(bool, result)


class FakeRuntime:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.records: list[RegisteredPrinter] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False

    async def reconcile_committed(self, record: RegisteredPrinter) -> None:
        self.records.append(record)
        self.entered.set()
        if self.block:
            await self.release.wait()
        if self.failure is not None:
            raise self.failure


def entropy_sequence() -> Callable[[int], bytes]:
    calls = itertools.count(1)

    def generate(length: int) -> bytes:
        return bytes([next(calls)]) * length

    return generate


def make_service(  # noqa: PLR0913 -- compact explicit service fixture.
    tmp_path: Path,
    *,
    probe: FakeProbe | BlockingProbe | None = None,
    fences: FakeFences | None = None,
    random_bytes: Callable[[int], bytes] | None = None,
    clock_ms: Callable[[], int] | None = None,
    admissions: PrinterAdmissionGates | None = None,
    runtime: FakeRuntime | None = None,
) -> tuple[
    PrinterLifecycleService, SecretStore, PrinterStore, FakeProbe | BlockingProbe, FakeFences
]:
    secrets, store = make_stores(tmp_path)
    selected_probe = probe or FakeProbe()
    selected_fences = fences or FakeFences()
    ticks = itertools.count(1_000, 100)
    service = PrinterLifecycleService(
        store,
        secrets,
        selected_probe,
        selected_fences,
        admissions=admissions or PrinterAdmissionGates(),
        runtime=runtime or FakeRuntime(),
        random_bytes=random_bytes or entropy_sequence(),
        clock_ms=clock_ms or (lambda: next(ticks)),
    )
    return service, secrets, store, selected_probe, selected_fences


def create_request(
    *,
    idempotency_key: str = IDEMPOTENCY_KEY,
    credential: str = MOONRAKER_CREDENTIAL,
) -> CreatePrinterRequest:
    return CreatePrinterRequest(
        idempotency_key=idempotency_key,
        printer_uuid=PRINTER_UUID,
        actor="owner:local",
        request_origin="https://grove.example.test",
        display_name="Workshop Voron",
        endpoint=ENDPOINT,
        moonraker_credential=SecretStr(credential),
        safety_profiles=(safety_profile(),),
        control_enabled=True,
        dispatch_enabled=True,
    )


def inspect_request() -> InspectPrinterRequest:
    return InspectPrinterRequest(
        printer_uuid=PRINTER_UUID,
        actor="owner:local",
        request_origin="https://grove.example.test",
        endpoint=ENDPOINT,
        moonraker_credential=SecretStr(MOONRAKER_CREDENTIAL),
    )


def result_request(**updates: object) -> LifecycleResultRequest:
    values: dict[str, object] = {
        "idempotency_key": IDEMPOTENCY_KEY,
        "printer_uuid": PRINTER_UUID,
        "operation": RegistryOperationKind.CREATE,
        "actor": "owner:local",
        "request_origin": "https://grove.example.test",
    }
    values.update(updates)
    return LifecycleResultRequest.model_validate(values)


def update_request(
    revision: int,
    index: int,
    *,
    reactivate: bool = False,
) -> UpdatePrinterRequest:
    return UpdatePrinterRequest(
        idempotency_key=request_key(index),
        printer_uuid=PRINTER_UUID,
        actor="owner:local",
        request_origin="https://grove.example.test",
        expected_revision=revision,
        display_name=f"Workshop Voron {index}",
        endpoint=ENDPOINT,
        safety_profiles=(safety_profile(),),
        control_enabled=True,
        dispatch_enabled=True,
        reactivate=reactivate,
    )


def disable_request(revision: int, index: int) -> DisablePrinterRequest:
    return DisablePrinterRequest(
        idempotency_key=request_key(index),
        printer_uuid=PRINTER_UUID,
        actor="owner:local",
        request_origin="https://grove.example.test",
        expected_revision=revision,
    )


def remove_request(revision: int, index: int) -> RemovePrinterRequest:
    return RemovePrinterRequest(
        idempotency_key=request_key(index),
        printer_uuid=PRINTER_UUID,
        actor="owner:local",
        request_origin="https://grove.example.test",
        expected_revision=revision,
    )


def rotate_moonraker_request(revision: int, index: int) -> RotateMoonrakerCredentialRequest:
    return RotateMoonrakerCredentialRequest(
        idempotency_key=request_key(index),
        printer_uuid=PRINTER_UUID,
        actor="owner:local",
        request_origin="https://grove.example.test",
        expected_revision=revision,
        moonraker_credential=SecretStr(NEW_MOONRAKER_CREDENTIAL),
    )


def rotate_compatibility_request(
    revision: int,
    index: int,
) -> RotateCompatibilityCredentialRequest:
    return RotateCompatibilityCredentialRequest(
        idempotency_key=request_key(index),
        printer_uuid=PRINTER_UUID,
        actor="owner:local",
        request_origin="https://grove.example.test",
        expected_revision=revision,
    )


@pytest.mark.asyncio
async def test_inspection_returns_only_bounded_probe_evidence(tmp_path: Path) -> None:
    service, _secrets, _store, probe, _fences = make_service(tmp_path)
    assert isinstance(probe, FakeProbe)

    observed = await service.inspect(inspect_request())

    assert observed == evidence()
    assert probe.calls == [(ENDPOINT, MOONRAKER_CREDENTIAL)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (MoonrakerProbeError(), LifecycleFailureCode.PROBE_FAILED),
        (RuntimeError("private remote body"), LifecycleFailureCode.INTERNAL_FAILURE),
    ],
)
async def test_inspection_maps_probe_failures_to_bounded_codes(
    tmp_path: Path,
    failure: Exception,
    expected: LifecycleFailureCode,
) -> None:
    service, _secrets, _store, _probe, _fences = make_service(
        tmp_path,
        probe=FakeProbe([failure]),
    )
    with pytest.raises(LifecycleServiceError) as denied:
        await service.inspect(inspect_request())
    assert denied.value.code is expected
    assert "private remote body" not in str(denied.value)


@pytest.mark.asyncio
async def test_result_recovers_exact_committed_operation_without_reprobe(tmp_path: Path) -> None:
    service, secrets, _store, probe, _fences = make_service(tmp_path)
    assert isinstance(probe, FakeProbe)
    committed = await service.create(create_request())
    probe_calls = len(probe.calls)
    restarted_store = PrinterStore(tmp_path / "registry.sqlite3", secrets)
    restarted_store.initialize()
    restarted = PrinterLifecycleService(
        restarted_store,
        secrets,
        probe,
        FakeFences(),
        admissions=PrinterAdmissionGates(),
        runtime=FakeRuntime(),
    )

    recovered = await restarted.result(result_request())

    assert recovered == committed
    assert len(probe.calls) == probe_calls


@pytest.mark.asyncio
async def test_cancelled_handoff_finishes_fail_closed_and_result_recovers(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    runtime.block = True
    service, _secrets, store, _probe, _fences = make_service(tmp_path, runtime=runtime)

    create = asyncio.create_task(service.create(create_request()))
    await runtime.entered.wait()
    create.cancel()
    runtime.release.set()

    with pytest.raises(asyncio.CancelledError):
        await create
    committed = store.lookup_operation(IDEMPOTENCY_KEY)
    assert committed is not None and committed.state is RegistryOperationState.COMMITTED
    assert runtime.records == [committed.result]

    recovered = await service.result(result_request())
    assert recovered == committed.result
    assert runtime.records == [committed.result, committed.result]


@pytest.mark.asyncio
async def test_committed_handoff_failure_preserves_durable_result_for_recovery(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime(RuntimeError("runtime route uncertain"))
    service, _secrets, store, _probe, _fences = make_service(tmp_path, runtime=runtime)

    with pytest.raises(LifecycleServiceError) as failed:
        await service.create(create_request())
    assert failed.value.code is LifecycleFailureCode.RUNTIME_UNAVAILABLE
    committed = store.lookup_operation(IDEMPOTENCY_KEY)
    assert committed is not None and committed.state is RegistryOperationState.COMMITTED

    runtime.failure = None
    assert await service.result(result_request()) == committed.result


@pytest.mark.asyncio
async def test_result_denies_missing_or_mismatched_operation(tmp_path: Path) -> None:
    service, _secrets, _store, _probe, _fences = make_service(tmp_path)
    with pytest.raises(LifecycleServiceError) as missing:
        await service.result(result_request())
    assert missing.value.code is LifecycleFailureCode.CONFLICT
    await service.create(create_request())
    for request in (
        result_request(actor="owner:other"),
        result_request(operation=RegistryOperationKind.UPDATE),
        result_request(printer_uuid=OTHER_PRINTER_UUID),
        result_request(request_origin="https://other.example.test"),
    ):
        with pytest.raises(LifecycleServiceError) as denied:
            await service.result(request)
        assert denied.value.code is LifecycleFailureCode.CONFLICT


@pytest.mark.asyncio
async def test_result_maps_registry_lookup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _secrets, store, _probe, _fences = make_service(tmp_path)

    def fail_lookup(_key: str) -> None:
        raise RegistryStoreError

    monkeypatch.setattr(store, "lookup_operation", fail_lookup)
    with pytest.raises(LifecycleServiceError) as denied:
        await service.result(result_request())
    assert denied.value.code is LifecycleFailureCode.STORAGE_UNAVAILABLE


@pytest.mark.asyncio
async def test_lifecycle_proof_and_commit_wait_for_the_shared_printer_gate(
    tmp_path: Path,
) -> None:
    admissions = PrinterAdmissionGates()
    service, _secrets, store, _probe, fences = make_service(
        tmp_path,
        admissions=admissions,
    )
    current = await service.create(create_request())
    request = disable_request(current.revision, 90)

    async with admissions.hold(PRINTER_UUID):
        task = asyncio.create_task(service.disable(request))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert fences.calls == []
        assert store.get(PRINTER_UUID) == current
    disabled = await task
    assert disabled.lifecycle is PrinterLifecycle.DISABLED
    assert fences.calls == [PRINTER_UUID]


@pytest.mark.asyncio
async def test_service_executes_and_idempotently_replays_the_exact_full_lifecycle(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    probe = FakeProbe(evidence(index) for index in range(4))
    service, secrets, store, _, fences = make_service(tmp_path, probe=probe)
    create = create_request()

    current = await service.create(create)
    assert current.revision == 1
    assert current.identity == evidence(0)
    assert current.moonraker_credential_ref is not None
    assert current.compatibility_credential_ref is not None
    assert secrets.read(current.moonraker_credential_ref, minimum_length=32) == MOONRAKER_CREDENTIAL
    assert secrets.read(current.compatibility_credential_ref, minimum_length=20) == (
        base64.urlsafe_b64encode(b"\x01" * 15).decode("ascii")
    )
    assert MOONRAKER_CREDENTIAL not in current.model_dump_json()
    assert await service.create(create) == current
    assert len(probe.calls) == 1

    update = update_request(current.revision, 2)
    current = await service.update(update)
    assert current.identity == evidence(1)
    assert await service.update(update) == current
    current = await service.disable(disable_request(current.revision, 3))
    assert current.lifecycle is PrinterLifecycle.DISABLED
    assert not current.control_enabled and not current.dispatch_enabled
    current = await service.update(update_request(current.revision, 4, reactivate=True))
    assert current.lifecycle is PrinterLifecycle.ACTIVE
    assert current.identity == evidence(2)

    old_moonraker_ref = current.moonraker_credential_ref
    rotate_moonraker = rotate_moonraker_request(current.revision, 5)
    current = await service.rotate_moonraker(rotate_moonraker)
    assert current.identity == evidence(3)
    assert current.moonraker_credential_ref != old_moonraker_ref
    assert old_moonraker_ref is not None and not secrets.contains(old_moonraker_ref)
    assert current.moonraker_credential_ref is not None
    assert (
        secrets.read(current.moonraker_credential_ref, minimum_length=32)
        == NEW_MOONRAKER_CREDENTIAL
    )
    assert await service.rotate_moonraker(rotate_moonraker) == current

    old_compatibility_ref = current.compatibility_credential_ref
    old_compatibility = secrets.read(_required_reference(old_compatibility_ref), minimum_length=20)
    rotate_compatibility = rotate_compatibility_request(current.revision, 6)
    current = await service.rotate_compatibility(rotate_compatibility)
    assert current.compatibility_credential_ref != old_compatibility_ref
    assert old_compatibility_ref is not None and not secrets.contains(old_compatibility_ref)
    assert current.compatibility_credential_ref is not None
    assert (
        secrets.read(current.compatibility_credential_ref, minimum_length=20) != old_compatibility
    )
    assert await service.rotate_compatibility(rotate_compatibility) == current

    current = await service.disable(disable_request(current.revision, 7))
    assert await service.disable(disable_request(current.revision - 1, 7)) == current
    remove = remove_request(current.revision, 8)
    removed = await service.remove(remove)
    assert removed.lifecycle is PrinterLifecycle.REMOVED
    assert removed.moonraker_credential_ref is None
    assert removed.compatibility_credential_ref is None
    assert await service.remove(remove) == removed
    assert store.list() == ()
    assert store.list(include_removed=True) == (removed,)
    assert secrets.references() == frozenset()
    assert len(probe.calls) == 4
    assert fences.calls == [PRINTER_UUID] * 7
    operations = store.operations(PRINTER_UUID)
    assert len(operations) == 8
    assert all(item.state is RegistryOperationState.COMMITTED for item in operations)
    assert all(item.result is not None for item in operations)
    assert all(MOONRAKER_CREDENTIAL not in item.model_dump_json() for item in operations)


@pytest.mark.asyncio
async def test_preparing_duplicate_is_busy_and_cancellation_aborts_for_exact_retry(
    tmp_path: Path,
) -> None:
    probe = BlockingProbe()
    service, secrets, store, _, _ = make_service(tmp_path, probe=probe)
    request = create_request()
    task = asyncio.create_task(service.create(request))
    await probe.entered.wait()

    with pytest.raises(LifecycleServiceError) as duplicate:
        await service.create(request)
    assert duplicate.value.code is LifecycleFailureCode.BUSY
    with pytest.raises(LifecycleServiceError) as competing:
        await service.create(create_request(idempotency_key=request_key(20)))
    assert competing.value.code is LifecycleFailureCode.BUSY

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    aborted = store.lookup_operation(request.idempotency_key)
    assert aborted is not None
    assert aborted.state is RegistryOperationState.ABORTED
    assert secrets.references() == frozenset()

    probe.release.set()
    restarted_store = PrinterStore(tmp_path / "registry.sqlite3", secrets)
    restarted_store.initialize()
    restarted = PrinterLifecycleService(
        restarted_store,
        secrets,
        probe,
        FakeFences(),
        admissions=PrinterAdmissionGates(),
        runtime=FakeRuntime(),
        random_bytes=entropy_sequence(),
        clock_ms=lambda: 2_000,
    )
    result = await restarted.create(request)
    retried = restarted_store.lookup_operation(request.idempotency_key)
    assert result.revision == 1
    assert retried is not None and retried.attempt == 2


@pytest.mark.asyncio
async def test_probe_failure_is_bounded_and_cleans_every_uncommitted_secret(
    tmp_path: Path,
) -> None:
    probe = FakeProbe([MoonrakerProbeError("must not escape")])
    service, secrets, store, _, _ = make_service(tmp_path, probe=probe)
    request = create_request()

    with pytest.raises(LifecycleServiceError) as failure:
        await service.create(request)

    assert failure.value.code is LifecycleFailureCode.PROBE_FAILED
    assert str(failure.value) == "probe_failed"
    assert MOONRAKER_CREDENTIAL not in str(failure.value)
    assert secrets.references() == frozenset()
    record = store.lookup_operation(request.idempotency_key)
    assert record is not None and record.state is RegistryOperationState.ABORTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fence_result", "expected"),
    [
        (False, LifecycleFailureCode.PRINTER_FENCED),
        (FenceInspectionError(), LifecycleFailureCode.FENCE_UNAVAILABLE),
        (RuntimeError("unavailable"), LifecycleFailureCode.FENCE_UNAVAILABLE),
        ("not-bool", LifecycleFailureCode.FENCE_UNAVAILABLE),
    ],
)
async def test_mutations_fail_closed_on_every_incomplete_fence_proof(
    tmp_path: Path,
    fence_result: object,
    expected: LifecycleFailureCode,
) -> None:
    fences = FakeFences([fence_result])
    service, _secrets, store, _, _ = make_service(tmp_path, fences=fences)
    current = await service.create(create_request())
    request = disable_request(current.revision, 30)

    with pytest.raises(LifecycleServiceError) as failure:
        await service.disable(request)

    assert failure.value.code is expected
    assert store.get(PRINTER_UUID) == current
    record = store.lookup_operation(request.idempotency_key)
    assert record is not None and record.state is RegistryOperationState.ABORTED


@pytest.mark.asyncio
async def test_exact_revision_and_lifecycle_transitions_are_mandatory(tmp_path: Path) -> None:
    service, _secrets, store, _, _ = make_service(tmp_path)
    with pytest.raises(LifecycleServiceError) as missing:
        await service.update(update_request(1, 40))
    assert missing.value.code is LifecycleFailureCode.STALE_REVISION

    current = await service.create(create_request())
    with pytest.raises(LifecycleServiceError) as stale:
        await service.disable(disable_request(current.revision + 1, 41))
    assert stale.value.code is LifecycleFailureCode.STALE_REVISION
    with pytest.raises(LifecycleServiceError) as false_reactivation:
        await service.update(update_request(current.revision, 42, reactivate=True))
    assert false_reactivation.value.code is LifecycleFailureCode.INVALID_TRANSITION
    with pytest.raises(LifecycleServiceError) as early_remove:
        await service.remove(remove_request(current.revision, 43))
    assert early_remove.value.code is LifecycleFailureCode.INVALID_TRANSITION

    current = await service.disable(disable_request(current.revision, 44))
    with pytest.raises(LifecycleServiceError) as missing_reactivation:
        await service.update(update_request(current.revision, 45))
    assert missing_reactivation.value.code is LifecycleFailureCode.INVALID_TRANSITION
    with pytest.raises(LifecycleServiceError) as disabled_rotation:
        await service.rotate_moonraker(rotate_moonraker_request(current.revision, 46))
    assert disabled_rotation.value.code is LifecycleFailureCode.INVALID_TRANSITION
    assert store.get(PRINTER_UUID) == current


@pytest.mark.asyncio
async def test_create_rejects_existing_uuid_and_idempotency_collisions(tmp_path: Path) -> None:
    service, _secrets, _store, _, _ = make_service(tmp_path)
    request = create_request()
    await service.create(request)

    with pytest.raises(LifecycleServiceError) as existing_uuid:
        await service.create(create_request(idempotency_key=request_key(47)))
    assert existing_uuid.value.code is LifecycleFailureCode.CONFLICT

    changed = create_request(credential="x" * 32)
    with pytest.raises(LifecycleServiceError) as changed_duplicate:
        await service.create(changed)
    assert changed_duplicate.value.code is LifecycleFailureCode.CONFLICT


@pytest.mark.asyncio
async def test_generated_credential_references_must_be_new_and_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, secrets, _store, _, _ = make_service(tmp_path)
    same_reference = "credential-55555555-5555-4555-8555-555555555555"
    with monkeypatch.context() as scoped:
        scoped.setattr(secrets, "allocate_reference", lambda: same_reference)
        with pytest.raises(LifecycleServiceError) as duplicate_create_refs:
            await service.create(create_request())
    assert duplicate_create_refs.value.code is LifecycleFailureCode.STORAGE_UNAVAILABLE

    current = await service.create(create_request(idempotency_key=request_key(48)))
    with monkeypatch.context() as scoped:
        scoped.setattr(
            secrets,
            "allocate_reference",
            lambda: _required_reference(current.moonraker_credential_ref),
        )
        with pytest.raises(LifecycleServiceError) as moonraker_collision:
            await service.rotate_moonraker(rotate_moonraker_request(current.revision, 49))
    assert moonraker_collision.value.code is LifecycleFailureCode.STORAGE_UNAVAILABLE

    with monkeypatch.context() as scoped:
        scoped.setattr(
            secrets,
            "allocate_reference",
            lambda: _required_reference(current.compatibility_credential_ref),
        )
        with pytest.raises(LifecycleServiceError) as compatibility_collision:
            await service.rotate_compatibility(rotate_compatibility_request(current.revision, 50))
    assert compatibility_collision.value.code is LifecycleFailureCode.STORAGE_UNAVAILABLE

    with monkeypatch.context() as scoped:
        scoped.setattr(secrets, "allocate_reference", lambda: "invalid-reference")
        with pytest.raises(LifecycleServiceError) as invalid_reference:
            await service.rotate_moonraker(rotate_moonraker_request(current.revision, 51))
    assert invalid_reference.value.code is LifecycleFailureCode.INVALID_TRANSITION


@pytest.mark.asyncio
async def test_credential_rotations_reject_the_existing_secret_value(tmp_path: Path) -> None:
    service, secrets, _store, _, _ = make_service(
        tmp_path,
        random_bytes=lambda length: b"\x01" * length,
    )
    current = await service.create(create_request())

    same_moonraker = rotate_moonraker_request(current.revision, 50).model_copy(
        update={"moonraker_credential": create_request().moonraker_credential}
    )
    with pytest.raises(LifecycleServiceError) as moonraker_failure:
        await service.rotate_moonraker(same_moonraker)
    assert moonraker_failure.value.code is LifecycleFailureCode.INVALID_TRANSITION

    with pytest.raises(LifecycleServiceError) as compatibility_failure:
        await service.rotate_compatibility(rotate_compatibility_request(current.revision, 51))
    assert compatibility_failure.value.code is LifecycleFailureCode.INVALID_TRANSITION
    assert secrets.references() == frozenset(
        {
            _required_reference(current.moonraker_credential_ref),
            _required_reference(current.compatibility_credential_ref),
        }
    )


@pytest.mark.asyncio
async def test_post_commit_cleanup_ambiguity_is_reconciled_without_repeating_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, secrets, store, probe, _ = make_service(tmp_path)
    request = create_request()
    original_finalize = store.finalize
    monkeypatch.setattr(
        store,
        "finalize",
        lambda _operation: (_ for _ in ()).throw(RegistryStoreError()),
    )

    with pytest.raises(LifecycleServiceError) as ambiguous:
        await service.create(request)
    assert ambiguous.value.code is LifecycleFailureCode.STORAGE_UNAVAILABLE
    durable = store.lookup_operation(request.idempotency_key)
    assert durable is not None and durable.state is RegistryOperationState.COMMITTED
    assert store.get(PRINTER_UUID) is not None

    monkeypatch.setattr(store, "finalize", original_finalize)
    restarted_store = PrinterStore(tmp_path / "registry.sqlite3", secrets)
    restarted_store.initialize()
    restarted = PrinterLifecycleService(
        restarted_store,
        secrets,
        probe,
        FakeFences(),
        admissions=PrinterAdmissionGates(),
        runtime=FakeRuntime(),
        random_bytes=entropy_sequence(),
        clock_ms=lambda: 2_000,
    )
    replayed = await restarted.create(request)
    assert replayed == restarted_store.get(PRINTER_UUID)
    assert isinstance(probe, FakeProbe) and len(probe.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("generated", [b"short", b"x" * 16, "not-bytes"])
async def test_compatibility_entropy_must_be_exactly_fifteen_bytes(
    tmp_path: Path,
    generated: object,
) -> None:
    def invalid_entropy(_length: int) -> bytes:
        return cast(bytes, generated)

    service, _secrets, store, _, _ = make_service(
        tmp_path,
        random_bytes=invalid_entropy,
    )
    with pytest.raises(LifecycleServiceError) as failure:
        await service.create(create_request())
    assert failure.value.code is LifecycleFailureCode.STORAGE_UNAVAILABLE
    assert store.lookup_operation(IDEMPOTENCY_KEY) is None


@pytest.mark.asyncio
async def test_entropy_and_clock_provider_failures_are_bounded(tmp_path: Path) -> None:
    service, _secrets, _store, _, _ = make_service(
        tmp_path,
        random_bytes=lambda _length: (_ for _ in ()).throw(RuntimeError("entropy")),
    )
    with pytest.raises(LifecycleServiceError) as entropy_failure:
        await service.create(create_request())
    assert entropy_failure.value.code is LifecycleFailureCode.STORAGE_UNAVAILABLE

    service, _secrets, _store, _, _ = make_service(
        tmp_path / "clock-exception",
        clock_ms=lambda: (_ for _ in ()).throw(RuntimeError("clock")),
    )
    with pytest.raises(LifecycleServiceError) as clock_exception:
        await service.create(create_request())
    assert clock_exception.value.code is LifecycleFailureCode.INTERNAL_FAILURE

    for value in (-1, 2**63, True, "not-an-int"):

        def invalid_clock(value: object = value) -> int:
            return cast(int, value)

        service, _secrets, store, _, _ = make_service(
            tmp_path / str(value),
            clock_ms=invalid_clock,
        )
        with pytest.raises(LifecycleServiceError) as clock_failure:
            await service.create(create_request())
        assert clock_failure.value.code is LifecycleFailureCode.INTERNAL_FAILURE
        assert store.lookup_operation(IDEMPOTENCY_KEY) is None


@pytest.mark.asyncio
async def test_preparation_dependency_failures_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, secrets, store, _, _ = make_service(tmp_path)

    with monkeypatch.context() as scoped:
        scoped.setattr(
            secrets,
            "fingerprint",
            lambda _document: (_ for _ in ()).throw(SecretStoreError()),
        )
        with pytest.raises(LifecycleServiceError) as fingerprint_failure:
            await service.create(create_request())
    assert fingerprint_failure.value.code is LifecycleFailureCode.STORAGE_UNAVAILABLE

    with monkeypatch.context() as scoped:
        scoped.setattr(
            store,
            "lookup_operation",
            lambda _key: (_ for _ in ()).throw(RegistryStoreError()),
        )
        with pytest.raises(LifecycleServiceError) as lookup_failure:
            await service.create(create_request())
    assert lookup_failure.value.code is LifecycleFailureCode.STORAGE_UNAVAILABLE

    with monkeypatch.context() as scoped:
        scoped.setattr(
            store,
            "get",
            lambda _uuid: (_ for _ in ()).throw(RegistryStoreError()),
        )
        with pytest.raises(LifecycleServiceError) as create_get_failure:
            await service.create(create_request())
    assert create_get_failure.value.code is LifecycleFailureCode.STORAGE_UNAVAILABLE

    with monkeypatch.context() as scoped:
        scoped.setattr(
            secrets,
            "allocate_reference",
            lambda: (_ for _ in ()).throw(SecretStoreError()),
        )
        with pytest.raises(LifecycleServiceError) as allocation_failure:
            await service.create(create_request())
    assert allocation_failure.value.code is LifecycleFailureCode.STORAGE_UNAVAILABLE

    with monkeypatch.context() as scoped:
        scoped.setattr(
            store,
            "get",
            lambda _uuid: (_ for _ in ()).throw(RegistryStoreError()),
        )
        with pytest.raises(LifecycleServiceError) as update_get_failure:
            await service.update(update_request(1, 60))
    assert update_get_failure.value.code is LifecycleFailureCode.STORAGE_UNAVAILABLE


@pytest.mark.asyncio
async def test_prepared_and_durable_result_boundaries_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _secrets, store, _, _ = make_service(tmp_path)
    preparing = operation()
    aborted = operation(state=RegistryOperationState.ABORTED, error_code="interrupted")
    committed = operation(
        state=RegistryOperationState.COMMITTED,
        result=printer(),
        committed_at_unix_ms=1_000,
    )

    with pytest.raises(LifecycleServiceError) as preparing_failure:
        service._durable_result(preparing)
    assert preparing_failure.value.code is LifecycleFailureCode.BUSY
    with pytest.raises(LifecycleServiceError) as aborted_failure:
        service._durable_result(aborted)
    assert aborted_failure.value.code is LifecycleFailureCode.STORAGE_UNAVAILABLE

    async def must_not_complete(_operation: RegistryOperationRecord) -> RegisteredPrinter:
        raise AssertionError

    with monkeypatch.context() as scoped:
        scoped.setattr(store, "reserve", lambda _operation: (committed, False))
        scoped.setattr(store, "finalize", lambda _operation: committed)
        assert await service._run_prepared(preparing, must_not_complete) == printer()

    with monkeypatch.context() as scoped:
        scoped.setattr(
            store,
            "finalize",
            lambda _operation: (_ for _ in ()).throw(RegistryStoreError()),
        )
        with pytest.raises(LifecycleServiceError) as finalize_failure:
            service._durable_result(committed)
    assert finalize_failure.value.code is LifecycleFailureCode.STORAGE_UNAVAILABLE

    with monkeypatch.context() as scoped:
        scoped.setattr(store, "finalize", lambda _operation: SimpleNamespace(result=None))
        with pytest.raises(LifecycleServiceError) as missing_result:
            service._durable_result(committed)
    assert missing_result.value.code is LifecycleFailureCode.STORAGE_UNAVAILABLE

    with monkeypatch.context() as scoped:
        scoped.setattr(store, "commit", lambda *_args, **_kwargs: SimpleNamespace(result=None))
        with pytest.raises(LifecycleServiceError) as null_commit:
            service._commit(preparing, printer(), 1_000)
    assert null_commit.value.code is LifecycleFailureCode.STORAGE_UNAVAILABLE


def test_abort_cleanup_and_compatibility_encoding_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _secrets, store, _, _ = make_service(tmp_path)
    preparing = operation()

    with monkeypatch.context() as scoped:
        scoped.setattr(store, "lookup_operation", lambda _key: None)
        with pytest.raises(LifecycleServiceError) as missing_operation:
            service._abort_if_preparing(preparing)
    assert missing_operation.value.code is LifecycleFailureCode.STORAGE_UNAVAILABLE

    with monkeypatch.context() as scoped:
        scoped.setattr(store, "lookup_operation", lambda _key: preparing)
        scoped.setattr(
            store,
            "abort",
            lambda _operation: (_ for _ in ()).throw(RegistryStoreError()),
        )
        with pytest.raises(LifecycleServiceError) as abort_failure:
            service._abort_if_preparing(preparing)
    assert abort_failure.value.code is LifecycleFailureCode.STORAGE_UNAVAILABLE

    with monkeypatch.context() as scoped:
        scoped.setattr(base64, "urlsafe_b64encode", lambda _value: b"bad")
        with pytest.raises(LifecycleServiceError) as encoding_failure:
            service._new_compatibility_secret()
    assert encoding_failure.value.code is LifecycleFailureCode.STORAGE_UNAVAILABLE

    with monkeypatch.context() as scoped:
        scoped.setattr(
            base64,
            "urlsafe_b64encode",
            lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        with pytest.raises(LifecycleServiceError) as encoding_exception:
            service._new_compatibility_secret()
    assert encoding_exception.value.code is LifecycleFailureCode.STORAGE_UNAVAILABLE


class FakeControlFence:
    def __init__(self, result: object) -> None:
        self.result = result

    async def has_unresolved(self, _printer_id: str) -> bool:
        if isinstance(self.result, Exception):
            raise self.result
        return cast(bool, self.result)


class FakeDispatchFence:
    def __init__(self, result: object) -> None:
        self.result = result

    def unresolved(self, _printer_uuid: str) -> tuple[object, ...]:
        if isinstance(self.result, Exception):
            raise self.result
        return cast(tuple[object, ...], self.result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("control", "dispatch", "expected"),
    [
        (False, (), True),
        (True, (), False),
        (False, (object(),), False),
    ],
)
async def test_composite_fence_requires_both_control_planes_to_be_clear(
    control: bool,
    dispatch: tuple[object, ...],
    expected: bool,
) -> None:
    inspector = CompositeActuatorFenceInspector(
        FakeControlFence(control),
        FakeDispatchFence(dispatch),
    )
    assert await inspector.clear(PRINTER_UUID) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("control", "dispatch"),
    [
        (RuntimeError("control"), ()),
        (False, RuntimeError("dispatch")),
        ("not-bool", ()),
        (False, []),
    ],
)
async def test_composite_fence_rejects_errors_and_non_exact_result_types(
    control: object,
    dispatch: object,
) -> None:
    inspector = CompositeActuatorFenceInspector(
        FakeControlFence(control),
        FakeDispatchFence(dispatch),
    )
    with pytest.raises(FenceInspectionError):
        await inspector.clear(PRINTER_UUID)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (LifecycleServiceError(LifecycleFailureCode.BUSY), LifecycleFailureCode.BUSY),
        (MoonrakerProbeError(), LifecycleFailureCode.PROBE_FAILED),
        (FenceInspectionError(), LifecycleFailureCode.FENCE_UNAVAILABLE),
        (RegistryConflictError(), LifecycleFailureCode.CONFLICT),
        (RegistryBusyError(), LifecycleFailureCode.BUSY),
        (RegistryTransitionError(), LifecycleFailureCode.INVALID_TRANSITION),
        (RegistryStoreError(), LifecycleFailureCode.STORAGE_UNAVAILABLE),
        (SecretStoreError(), LifecycleFailureCode.STORAGE_UNAVAILABLE),
        (RuntimeError(), LifecycleFailureCode.INTERNAL_FAILURE),
    ],
)
def test_internal_failures_map_to_one_bounded_public_code(
    error: Exception,
    expected: LifecycleFailureCode,
) -> None:
    assert _service_error(error).code is expected


def test_validation_failures_map_to_invalid_transition() -> None:
    with pytest.raises(ValidationError) as invalid:
        MoonrakerEndpoint(url="not-an-origin")
    assert _service_error(invalid.value).code is LifecycleFailureCode.INVALID_TRANSITION


def test_private_transition_helpers_fail_closed_on_missing_or_invalid_records() -> None:
    with pytest.raises(LifecycleServiceError) as missing:
        _required_reference(None)
    assert missing.value.code is LifecycleFailureCode.STORAGE_UNAVAILABLE
    with pytest.raises(LifecycleServiceError) as invalid:
        _updated_printer(printer(), {"revision": 0})
    assert invalid.value.code is LifecycleFailureCode.INVALID_TRANSITION


@pytest.mark.parametrize(
    "updates",
    [
        {"operation": RegistryOperationKind.UPDATE},
        {"printer_uuid": OTHER_PRINTER_UUID},
        {"request_fingerprint": "b" * 64},
        {"actor": "owner:other"},
        {"request_origin": "https://other.example.test"},
        {"expected_revision": 1},
    ],
)
def test_exact_duplicate_comparison_binds_every_request_field(
    updates: dict[str, object],
) -> None:
    request = create_request()
    existing = operation(**updates)
    assert not _same_request(
        existing,
        RegistryOperationKind.CREATE,
        request,
        "a" * 64,
    )


def test_exact_duplicate_comparison_accepts_only_the_complete_match() -> None:
    assert _same_request(
        operation(),
        RegistryOperationKind.CREATE,
        create_request(),
        "a" * 64,
    )


def test_unix_clock_returns_a_nonnegative_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time_ns", lambda: 1_234_567_890)
    assert onboarding_module._unix_time_ms() == 1_234
