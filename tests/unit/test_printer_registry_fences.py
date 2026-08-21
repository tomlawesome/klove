from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from klove.domain.fence import (
    ActiveFenceReference,
    FenceKind,
    FenceOwnerStore,
    FencePageCursor,
    FenceResolutionCode,
    FenceState,
    FenceStoreMetadata,
    FenceTransition,
    PreparedFenceReference,
    ResolvedFenceReference,
)
from klove.domain.onboarding import RegisteredPrinter, RegistryOperationKind
from klove.persistence.printer_registry import (
    PrinterStore,
    RegistryBusyError,
    RegistryConflictError,
    RegistryStoreError,
    RegistryTransitionError,
)
from klove.persistence.printer_registry_fences import (
    _decode_reference,
    _decode_transition,
    _validate_page,
    _validate_printer_uuid,
)
from klove.persistence.secret_store import SecretStore

from ..onboarding_helpers import make_stores, operation, printer, write_printer_secrets


def _uuid(index: int) -> str:
    return f"{index:08x}-0000-4000-8000-000000000000"


def _metadata(store: PrinterStore, owner: FenceOwnerStore, index: int) -> FenceStoreMetadata:
    registry = store.registry_store_metadata()
    return FenceStoreMetadata(
        owner_store=owner,
        installation_uuid=registry.installation_uuid,
        store_id=_uuid(index),
        schema_version=1,
    )


def _create(tmp_path: Path) -> tuple[SecretStore, PrinterStore, RegisteredPrinter]:
    secrets, store = make_stores(tmp_path)
    pending = operation()
    store.reserve(pending)
    write_printer_secrets(secrets)
    current = store.commit(pending, printer(), committed_at_unix_ms=1_000).result
    assert current is not None
    return secrets, store, current


def test_prepare_is_an_atomic_exact_printer_conflict_check_and_pages_stably(
    tmp_path: Path,
) -> None:
    _secrets, store, current = _create(tmp_path)
    control = _metadata(store, FenceOwnerStore.CONTROL_JOURNAL, 1)
    start = _metadata(store, FenceOwnerStore.START_JOURNAL, 2)
    store.register_fence_store(control)
    store.register_fence_store(control)
    store.register_fence_store(start)
    catalogue = store.fence_catalogue

    prepared = catalogue.prepare(
        printer_uuid=current.printer_uuid,
        fence_kind=FenceKind.CONTROL,
        owner_store=FenceOwnerStore.CONTROL_JOURNAL,
        store_id=control.store_id,
        owner_schema_version=1,
        operation_id=_uuid(10),
        created_at_unix_ms=1_100,
        fence_reference_id=_uuid(11),
    )
    with pytest.raises(RegistryBusyError):
        catalogue.prepare(
            printer_uuid=current.printer_uuid,
            fence_kind=FenceKind.PRINT_START,
            owner_store=FenceOwnerStore.START_JOURNAL,
            store_id=start.store_id,
            owner_schema_version=1,
            operation_id=_uuid(12),
            created_at_unix_ms=1_101,
            fence_reference_id=_uuid(13),
        )
    assert [item.fence_reference_id for item in store.fence_page(current.printer_uuid)] == [
        prepared.fence_reference_id
    ]


def test_composed_prepare_allows_only_the_exact_live_reference_set(tmp_path: Path) -> None:
    _secrets, store, current = _create(tmp_path)
    control = _metadata(store, FenceOwnerStore.CONTROL_JOURNAL, 21)
    start = _metadata(store, FenceOwnerStore.START_JOURNAL, 22)
    store.register_fence_store(control)
    store.register_fence_store(start)
    catalogue = store.fence_catalogue
    first = catalogue.prepare(
        printer_uuid=current.printer_uuid,
        fence_kind=FenceKind.CONTROL,
        owner_store=FenceOwnerStore.CONTROL_JOURNAL,
        store_id=control.store_id,
        owner_schema_version=1,
        operation_id=_uuid(23),
        created_at_unix_ms=1_100,
        fence_reference_id=_uuid(24),
    )
    second = catalogue.prepare(
        printer_uuid=current.printer_uuid,
        fence_kind=FenceKind.PRINT_START,
        owner_store=FenceOwnerStore.START_JOURNAL,
        store_id=start.store_id,
        owner_schema_version=1,
        operation_id=_uuid(25),
        created_at_unix_ms=1_101,
        fence_reference_id=_uuid(26),
        permitted_reference_ids=frozenset({first.fence_reference_id}),
    )
    with pytest.raises(RegistryTransitionError):
        catalogue.prepare(
            printer_uuid=current.printer_uuid,
            fence_kind=FenceKind.PRINT_START,
            owner_store=FenceOwnerStore.START_JOURNAL,
            store_id=start.store_id,
            owner_schema_version=1,
            operation_id=_uuid(27),
            created_at_unix_ms=1_102,
            fence_reference_id=_uuid(28),
            permitted_reference_ids=frozenset({first.fence_reference_id, _uuid(29)}),
        )
    assert second.fence_reference_id != first.fence_reference_id


def test_transitions_are_optimistic_terminal_and_immutable(tmp_path: Path) -> None:
    _secrets, store, current = _create(tmp_path)
    metadata = _metadata(store, FenceOwnerStore.CONTROL_JOURNAL, 31)
    store.register_fence_store(metadata)
    catalogue = store.fence_catalogue
    prepared = catalogue.prepare(
        printer_uuid=current.printer_uuid,
        fence_kind=FenceKind.CONTROL,
        owner_store=metadata.owner_store,
        store_id=metadata.store_id,
        owner_schema_version=1,
        operation_id=_uuid(32),
        created_at_unix_ms=1_100,
        fence_reference_id=_uuid(33),
    )
    active = catalogue.activate(prepared, transitioned_at_unix_ms=1_101)
    resolved = catalogue.resolve(
        active,
        resolution_code=FenceResolutionCode.CONFIRMED,
        transitioned_at_unix_ms=1_102,
    )
    with pytest.raises(RegistryTransitionError):
        catalogue.resolve(
            active,
            resolution_code=FenceResolutionCode.FAILED,
            transitioned_at_unix_ms=1_103,
        )
    assert resolved.resolution_code is FenceResolutionCode.CONFIRMED
    assert [
        item.to_state.value for item in store.fence_transitions(resolved.fence_reference_id)
    ] == [
        "prepared",
        "active",
        "resolved",
    ]
    with pytest.raises(ValidationError):
        FencePageCursor(fence_reference_id="not-a-uuid")


def test_store_metadata_accepts_exact_duplicate_and_rejects_substitution(tmp_path: Path) -> None:
    _secrets, store, _current = _create(tmp_path)
    metadata = _metadata(store, FenceOwnerStore.CONTROL_JOURNAL, 41)
    store.register_fence_store(metadata)
    store.register_fence_store(metadata)
    with pytest.raises(RegistryConflictError):
        store.register_fence_store(metadata.model_copy(update={"store_id": _uuid(42)}))
    with pytest.raises(RegistryConflictError):
        store.register_fence_store(metadata.model_copy(update={"installation_uuid": _uuid(43)}))
    with pytest.raises(RegistryConflictError):
        store.register_fence_store(metadata.model_copy(update={"schema_version": 2}))


def test_existing_printer_commit_is_fenced_inside_its_write_transaction(tmp_path: Path) -> None:
    _secrets, store, current = _create(tmp_path)
    metadata = _metadata(store, FenceOwnerStore.CONTROL_JOURNAL, 51)
    store.register_fence_store(metadata)
    store.fence_catalogue.prepare(
        printer_uuid=current.printer_uuid,
        fence_kind=FenceKind.CONTROL,
        owner_store=metadata.owner_store,
        store_id=metadata.store_id,
        owner_schema_version=1,
        operation_id=_uuid(52),
        created_at_unix_ms=1_100,
        fence_reference_id=_uuid(53),
    )
    pending = operation(
        idempotency_key=_uuid(54),
        operation=RegistryOperationKind.UPDATE,
        expected_revision=current.revision,
        new_credential_refs=(),
        started_at_unix_ms=1_101,
    )
    store.reserve(pending)
    with pytest.raises(RegistryBusyError):
        store.commit(
            pending,
            current.model_copy(update={"revision": 2, "updated_at_unix_ms": 1_200}),
            committed_at_unix_ms=1_200,
            require_fence_clear=True,
        )
    assert store.get(current.printer_uuid) == current


def test_typed_references_and_transitions_reject_invalid_closed_states() -> None:
    base = {
        "fence_reference_id": _uuid(61),
        "printer_uuid": _uuid(62),
        "fence_kind": FenceKind.CONTROL,
        "owner_store": FenceOwnerStore.CONTROL_JOURNAL,
        "store_id": _uuid(63),
        "owner_schema_version": 1,
        "operation_id": _uuid(64),
        "created_at_unix_ms": 10,
        "transitioned_at_unix_ms": 10,
    }
    invalid_references = (
        {**base, "owner_store": FenceOwnerStore.START_JOURNAL},
        {**base, "transitioned_at_unix_ms": 9},
        {**base, "state": FenceState.RESOLVED},
        {**base, "state": FenceState.ACTIVE, "resolution_code": FenceResolutionCode.FAILED},
    )
    for value in invalid_references:
        with pytest.raises(ValidationError):
            ActiveFenceReference.model_validate(value)
    with pytest.raises(ValidationError):
        ResolvedFenceReference.model_validate(base)
    with pytest.raises(ValidationError):
        PreparedFenceReference.model_validate({**base, "state": FenceState.ACTIVE})

    transition_base = {
        "fence_reference_id": _uuid(65),
        "transitioned_at_unix_ms": 10,
        "resolution_code": None,
    }
    invalid_transitions = (
        {
            **transition_base,
            "sequence": 1,
            "from_state": FenceState.PREPARED,
            "to_state": FenceState.ACTIVE,
        },
        {**transition_base, "sequence": 2, "from_state": None, "to_state": FenceState.ACTIVE},
        {
            **transition_base,
            "sequence": 2,
            "from_state": FenceState.PREPARED,
            "to_state": FenceState.PREPARED,
        },
        {
            **transition_base,
            "sequence": 2,
            "from_state": FenceState.ACTIVE,
            "to_state": FenceState.ACTIVE,
        },
        {
            **transition_base,
            "sequence": 2,
            "from_state": FenceState.PREPARED,
            "to_state": FenceState.RESOLVED,
        },
        {
            **transition_base,
            "sequence": 1,
            "from_state": None,
            "to_state": FenceState.RESOLVED,
        },
    )
    for value in invalid_transitions:
        with pytest.raises(ValidationError):
            FenceTransition.model_validate(value)


def test_fence_decoders_and_bounds_fail_closed() -> None:
    with pytest.raises(RegistryStoreError):
        _decode_reference(())
    with pytest.raises(RegistryStoreError):
        _decode_transition(())
    with pytest.raises(RegistryStoreError):
        _validate_page(0)
    with pytest.raises(RegistryStoreError):
        _validate_page(True)
    with pytest.raises(RegistryStoreError):
        _validate_printer_uuid("not-a-uuid")
