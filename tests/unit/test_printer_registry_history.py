from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from klove.domain.artifacts import safety_profile_fingerprint
from klove.domain.onboarding import (
    RegisteredPrinter,
    RegistryOperationKind,
    RegistryOperationRecord,
)
from klove.domain.registry_history import (
    MappingHistoryCursor,
    ProfileHistoryCursor,
    ProfileHistoryEvent,
    canonical_identity_json,
    canonical_profile_json,
    mapping_fingerprint,
)
from klove.persistence.printer_registry import PrinterStore, RegistryStoreError
from klove.persistence.printer_registry_history import (
    SqliteRegistryHistory,
    _decode_mapping,
    _decode_profile,
)
from klove.persistence.printer_registry_schema import _SCHEMA_VERSION

from ..onboarding_helpers import (
    OTHER_IDEMPOTENCY_KEY,
    OTHER_PRINTER_UUID,
    PRINTER_UUID,
    identity,
    make_stores,
    operation,
    printer,
    safety_profile,
    write_printer_secrets,
)


def _create(tmp_path: Path) -> tuple[PrinterStore, RegisteredPrinter]:
    secrets, store = make_stores(tmp_path)
    pending = operation()
    store.reserve(pending)
    write_printer_secrets(secrets)
    committed = store.commit(pending, printer(), committed_at_unix_ms=1_000)
    assert committed.result is not None
    return store, committed.result


def _update_operation(
    revision: int, *, key: str = OTHER_IDEMPOTENCY_KEY
) -> RegistryOperationRecord:
    return operation(
        idempotency_key=key,
        operation=RegistryOperationKind.UPDATE,
        expected_revision=revision,
        new_credential_refs=(),
        started_at_unix_ms=1_100 + revision,
    )


def test_empty_store_is_v3_and_create_appends_typed_history(tmp_path: Path) -> None:
    store, current = _create(tmp_path)

    connection = store._connect()
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (_SCHEMA_VERSION,)
    finally:
        connection.close()
    mappings = store.mapping_history(PRINTER_UUID)
    profiles = store.profile_history(PRINTER_UUID)

    assert len(mappings) == 1
    assert mappings[0].registry_revision == 1
    assert mappings[0].identity == current.identity
    assert len(profiles) == 1
    assert profiles[0].event is ProfileHistoryEvent.BOUND
    assert profiles[0].profile == current.safety_profiles[0]
    assert store.mapping_history(OTHER_PRINTER_UUID) == ()
    assert store.profile_history(OTHER_PRINTER_UUID) == ()


def test_fresher_equal_mapping_appends_no_duplicate(tmp_path: Path) -> None:
    store, current = _create(tmp_path)
    pending = _update_operation(current.revision)
    store.reserve(pending)
    changed = current.model_copy(
        update={
            "identity": current.identity.model_copy(update={"observed_at_unix_ms": 901}),
            "revision": 2,
            "updated_at_unix_ms": 1_400,
        }
    )

    store.commit(pending, changed, committed_at_unix_ms=1_400)
    store.initialize()

    assert len(store.mapping_history(PRINTER_UUID)) == 1
    assert len(store.profile_history(PRINTER_UUID)) == 1


def test_changed_mapping_and_profile_append_once_in_stable_pages(tmp_path: Path) -> None:
    store, current = _create(tmp_path)
    replacement = safety_profile().model_copy(
        update={
            "generation": 2,
            "compatibility": safety_profile().compatibility.model_copy(
                update={"build_plate_id": "glass"}
            ),
        }
    )
    changed = current.model_copy(
        update={
            "identity": identity().model_copy(
                update={"server_hostname": "moonraker-2", "observed_at_unix_ms": 901}
            ),
            "safety_profiles": (replacement,),
            "revision": 2,
            "updated_at_unix_ms": 1_400,
        }
    )
    pending = _update_operation(current.revision)
    store.reserve(pending)
    store.commit(pending, changed, committed_at_unix_ms=1_400)

    first_mapping = store.mapping_history(PRINTER_UUID, limit=1)
    second_mapping = store.mapping_history(
        PRINTER_UUID,
        cursor=MappingHistoryCursor(first_mapping[-1].registry_revision),
        limit=1,
    )
    assert [row.registry_revision for row in (*first_mapping, *second_mapping)] == [1, 2]

    first_profiles = store.profile_history(PRINTER_UUID, limit=2)
    cursor = ProfileHistoryCursor(
        first_profiles[-1].registry_revision,
        first_profiles[-1].slicer_profile_id,
        first_profiles[-1].event,
    )
    remaining_profiles = store.profile_history(PRINTER_UUID, cursor=cursor, limit=2)
    assert [
        (row.registry_revision, row.event) for row in (*first_profiles, *remaining_profiles)
    ] == [
        (1, ProfileHistoryEvent.BOUND),
        (2, ProfileHistoryEvent.BOUND),
        (2, ProfileHistoryEvent.RETIRED),
    ]


@pytest.mark.parametrize("limit", [0, 1_001, True, "1"])
def test_history_pages_reject_invalid_limits(tmp_path: Path, limit: object) -> None:
    store, _current = _create(tmp_path)

    with pytest.raises(RegistryStoreError):
        store.mapping_history(PRINTER_UUID, limit=limit)  # type: ignore[arg-type]
    with pytest.raises(RegistryStoreError):
        store.profile_history(PRINTER_UUID, limit=limit)  # type: ignore[arg-type]


def test_history_pages_reject_invalid_uuid_and_cursors(tmp_path: Path) -> None:
    store, _current = _create(tmp_path)

    for invalid_uuid in (
        "not-a-uuid",
        "AAAAAAAA-AAAA-4AAA-AAAA-AAAAAAAAAAAA",
        "11111111-1111-1111-8111-111111111111",
    ):
        with pytest.raises(RegistryStoreError):
            store.mapping_history(invalid_uuid)
    with pytest.raises(RegistryStoreError):
        store.mapping_history(PRINTER_UUID, cursor=MappingHistoryCursor(0))
    with pytest.raises(RegistryStoreError):
        store.profile_history(
            PRINTER_UUID,
            cursor=ProfileHistoryCursor(0, "profile", ProfileHistoryEvent.BOUND),
        )
    with pytest.raises(RegistryStoreError):
        store.profile_history(
            PRINTER_UUID,
            cursor=ProfileHistoryCursor(1, "", ProfileHistoryEvent.BOUND),
        )
    with pytest.raises(RegistryStoreError):
        store.mapping_history(PRINTER_UUID, cursor=object())  # type: ignore[arg-type]
    with pytest.raises(RegistryStoreError):
        store.profile_history(PRINTER_UUID, cursor=object())  # type: ignore[arg-type]
    with pytest.raises(RegistryStoreError):
        store.mapping_history(1)  # type: ignore[arg-type]


def test_generation_reuse_rolls_back_current_record_and_operation(tmp_path: Path) -> None:
    store, current = _create(tmp_path)
    remove_profile = current.model_copy(
        update={
            "safety_profiles": (),
            "dispatch_enabled": False,
            "revision": 2,
            "updated_at_unix_ms": 1_400,
        }
    )
    first = _update_operation(1)
    store.reserve(first)
    store.commit(first, remove_profile, committed_at_unix_ms=1_400)

    rebound = remove_profile.model_copy(
        update={"safety_profiles": (safety_profile(),), "revision": 3, "updated_at_unix_ms": 1_500}
    )
    second = _update_operation(2, key="55555555-5555-4555-8555-555555555555")
    store.reserve(second)

    with pytest.raises(RegistryStoreError):
        store.commit(second, rebound, committed_at_unix_ms=1_500)

    assert store.get(PRINTER_UUID) == remove_profile
    assert store.lookup_operation(second.idempotency_key) == second


def test_history_read_wraps_sqlite_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, _current = _create(tmp_path)

    class BrokenConnection:
        def execute(self, *_args: object) -> None:
            raise sqlite3.OperationalError

        def close(self) -> None:
            pass

    monkeypatch.setattr(store, "_connect", BrokenConnection)
    with pytest.raises(RegistryStoreError):
        store.mapping_history(PRINTER_UUID)
    with pytest.raises(RegistryStoreError):
        store.profile_history(PRINTER_UUID)


class _Rows:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows

    def execute(self, *_args: object) -> _Rows:
        return self

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows


@pytest.mark.parametrize("profile_ids", [("profile", "profile"), ("",), (1,)])
def test_retained_generation_query_rejects_invalid_profile_ids(profile_ids: object) -> None:
    with pytest.raises(RegistryStoreError):
        SqliteRegistryHistory().retained_profile_generations(
            _Rows([]),  # type: ignore[arg-type]
            PRINTER_UUID,
            profile_ids,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "rows",
    [
        [("profile",)],
        [(1, 1)],
        [("profile", "1")],
        [("profile", 0)],
    ],
)
def test_retained_generation_query_rejects_corrupt_rows(rows: list[tuple[object, ...]]) -> None:
    with pytest.raises(RegistryStoreError):
        SqliteRegistryHistory().retained_profile_generations(
            _Rows(rows),  # type: ignore[arg-type]
            PRINTER_UUID,
            ("profile",),
        )
    assert (
        SqliteRegistryHistory().retained_profile_generations(
            _Rows([("other", 2)]),  # type: ignore[arg-type]
            PRINTER_UUID,
            ("profile",),
        )
        == {}
    )


@pytest.mark.parametrize(
    "row",
    [
        (),
        (OTHER_PRINTER_UUID, 1, 900, "a" * 64, "{}"),
        (PRINTER_UUID, "1", 900, "a" * 64, "{}"),
        (PRINTER_UUID, 1, "900", "a" * 64, "{}"),
        (PRINTER_UUID, 1, 900, 1, "{}"),
        (PRINTER_UUID, 1, 900, "a" * 64, 1),
    ],
)
def test_mapping_decoder_rejects_structural_corruption(row: tuple[object, ...]) -> None:
    with pytest.raises(RegistryStoreError):
        _decode_mapping(row, PRINTER_UUID)


@pytest.mark.parametrize("kind", ["json", "canonical", "observed", "fingerprint"])
def test_mapping_decoder_rejects_payload_disagreement(kind: str) -> None:
    evidence = identity()
    document = canonical_identity_json(evidence)
    if kind == "json":
        document = "{"
    elif kind == "canonical":
        document = document + " "
    observed = 901 if kind == "observed" else 900
    fingerprint = "a" * 64 if kind == "fingerprint" else mapping_fingerprint(evidence)
    with pytest.raises(RegistryStoreError):
        _decode_mapping((PRINTER_UUID, 1, observed, fingerprint, document), PRINTER_UUID)


@pytest.mark.parametrize(
    "row",
    [
        (),
        (OTHER_PRINTER_UUID, 1, "profile", 1, "a" * 64, "bound", "{}"),
        (PRINTER_UUID, "1", "profile", 1, "a" * 64, "bound", "{}"),
        (PRINTER_UUID, 1, 1, 1, "a" * 64, "bound", "{}"),
        (PRINTER_UUID, 1, "profile", "1", "a" * 64, "bound", "{}"),
        (PRINTER_UUID, 1, "profile", 1, 1, "bound", "{}"),
        (PRINTER_UUID, 1, "profile", 1, "a" * 64, 1, "{}"),
        (PRINTER_UUID, 1, "profile", 1, "a" * 64, "bound", 1),
    ],
)
def test_profile_decoder_rejects_structural_corruption(row: tuple[object, ...]) -> None:
    with pytest.raises(RegistryStoreError):
        _decode_profile(row, PRINTER_UUID)


@pytest.mark.parametrize(
    "kind",
    ["event", "json", "canonical", "uuid", "id", "generation", "fingerprint"],
)
def test_profile_decoder_rejects_payload_disagreement(kind: str) -> None:
    profile = safety_profile()
    document = canonical_profile_json(profile)
    if kind == "json":
        document = "{"
    elif kind == "canonical":
        document = document + " "
    row_uuid = OTHER_PRINTER_UUID if kind == "uuid" else PRINTER_UUID
    profile_id = "other" if kind == "id" else profile.slicer_profile_id
    generation = 2 if kind == "generation" else profile.generation
    fingerprint = "a" * 64 if kind == "fingerprint" else safety_profile_fingerprint(profile)
    event = "unknown" if kind == "event" else "bound"
    with pytest.raises(RegistryStoreError):
        _decode_profile(
            (row_uuid, 1, profile_id, generation, fingerprint, event, document),
            row_uuid,
        )
