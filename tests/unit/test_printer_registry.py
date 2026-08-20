from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from pydantic import ValidationError

import klove.persistence.printer_registry as registry_module
from klove.domain.onboarding import (
    PrinterLifecycle,
    RegisteredPrinter,
    RegistryOperationKind,
    RegistryOperationRecord,
    RegistryOperationState,
)
from klove.persistence.printer_registry import (
    _METADATA_SCHEMA,
    PrinterStore,
    RegistryBusyError,
    RegistryConflictError,
    RegistryStoreError,
    RegistryTransitionError,
    _commit_transaction,
    _decode_operation,
    _decode_printer,
    _operation_row,
    _pragma_integer,
    _printer_row,
    _required_reference,
    _validate_schema,
    _validate_table,
    _validate_transition,
)
from klove.persistence.secret_store import SecretStore, SecretStoreError

from ..onboarding_helpers import (
    COMPATIBILITY_REF,
    IDEMPOTENCY_KEY,
    MOONRAKER_REF,
    NEW_COMPATIBILITY_REF,
    NEW_MOONRAKER_REF,
    OTHER_COMPATIBILITY_REF,
    OTHER_IDEMPOTENCY_KEY,
    OTHER_MOONRAKER_REF,
    OTHER_PRINTER_UUID,
    PRINTER_UUID,
    identity,
    make_stores,
    operation,
    printer,
    safety_profile,
    write_printer_secrets,
)


def key(index: int) -> str:
    return f"{index:08x}-0000-4000-8000-000000000000"


def test_public_facade_wildcard_exports_remain_compatible() -> None:
    assert set(registry_module.__all__) >= {
        "PrinterStore",
        "RegisteredPrinter",
        "RegistryOperationRecord",
        "RegistryOperationState",
    }


def create_printer(secrets: SecretStore, store: PrinterStore) -> RegisteredPrinter:
    pending = operation()
    assert store.reserve(pending) == (pending, True)
    write_printer_secrets(secrets)
    committed = store.commit(pending, printer(), committed_at_unix_ms=1_000)
    assert committed.state is RegistryOperationState.COMMITTED
    assert committed.result is not None
    return committed.result


def next_operation(
    current: RegisteredPrinter,
    kind: RegistryOperationKind,
    index: int,
    **updates: object,
) -> RegistryOperationRecord:
    values: dict[str, object] = {
        "idempotency_key": key(index),
        "operation": kind,
        "expected_revision": current.revision,
        "new_credential_refs": (),
        "retired_credential_refs": (),
        "started_at_unix_ms": 1_000 + index * 100,
    }
    values.update(updates)
    return operation(**values)


def test_registry_executes_exact_full_lifecycle_and_survives_restart(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    secrets, store = make_stores(tmp_path)
    assert store.get(PRINTER_UUID) is None
    assert store.list() == ()
    assert store.lookup_operation(IDEMPOTENCY_KEY) is None
    assert store.operations(PRINTER_UUID) == ()

    current = create_printer(secrets, store)
    assert store.get(PRINTER_UUID) == current
    assert store.list() == (current,)
    duplicate, created = store.reserve(operation())
    assert created is False
    assert duplicate.state is RegistryOperationState.COMMITTED

    update = next_operation(current, RegistryOperationKind.UPDATE, 4)
    assert store.reserve(update) == (update, True)
    current = printer(
        display_name="Renamed Voron",
        revision=2,
        updated_at_unix_ms=1_400,
    )
    store.commit(update, current, committed_at_unix_ms=1_400)

    rotate_moonraker = next_operation(
        current,
        RegistryOperationKind.ROTATE_MOONRAKER,
        5,
        new_credential_refs=(NEW_MOONRAKER_REF,),
        retired_credential_refs=(MOONRAKER_REF,),
    )
    store.reserve(rotate_moonraker)
    secrets.write(NEW_MOONRAKER_REF, "n" * 32, minimum_length=32)
    current = printer(
        display_name="Renamed Voron",
        moonraker_credential_ref=NEW_MOONRAKER_REF,
        identity=identity().model_copy(update={"server_hostname": "192.0.2.10"}),
        revision=3,
        updated_at_unix_ms=1_500,
    )
    rotated = store.commit(rotate_moonraker, current, committed_at_unix_ms=1_500)
    assert rotated.retired_credential_refs == ()
    assert current.identity.server_hostname == "192.0.2.10"
    assert not secrets.contains(MOONRAKER_REF)

    rotate_compatibility = next_operation(
        current,
        RegistryOperationKind.ROTATE_COMPATIBILITY,
        6,
        new_credential_refs=(NEW_COMPATIBILITY_REF,),
        retired_credential_refs=(COMPATIBILITY_REF,),
    )
    store.reserve(rotate_compatibility)
    secrets.write(NEW_COMPATIBILITY_REF, "d" * 20, minimum_length=20)
    current = printer(
        display_name="Renamed Voron",
        identity=current.identity,
        moonraker_credential_ref=NEW_MOONRAKER_REF,
        compatibility_credential_ref=NEW_COMPATIBILITY_REF,
        revision=4,
        updated_at_unix_ms=1_600,
    )
    store.commit(rotate_compatibility, current, committed_at_unix_ms=1_600)
    assert not secrets.contains(COMPATIBILITY_REF)

    disable = next_operation(current, RegistryOperationKind.DISABLE, 7)
    store.reserve(disable)
    current = printer(
        display_name="Renamed Voron",
        identity=current.identity,
        moonraker_credential_ref=NEW_MOONRAKER_REF,
        compatibility_credential_ref=NEW_COMPATIBILITY_REF,
        lifecycle=PrinterLifecycle.DISABLED,
        control_enabled=False,
        dispatch_enabled=False,
        revision=5,
        updated_at_unix_ms=1_700,
    )
    store.commit(disable, current, committed_at_unix_ms=1_700)

    reenable = next_operation(current, RegistryOperationKind.UPDATE, 8)
    store.reserve(reenable)
    current = printer(
        display_name="Renamed Voron",
        identity=current.identity,
        moonraker_credential_ref=NEW_MOONRAKER_REF,
        compatibility_credential_ref=NEW_COMPATIBILITY_REF,
        revision=6,
        updated_at_unix_ms=1_800,
    )
    store.commit(reenable, current, committed_at_unix_ms=1_800)

    disable_again = next_operation(current, RegistryOperationKind.DISABLE, 9)
    store.reserve(disable_again)
    current = current.model_copy(
        update={
            "lifecycle": PrinterLifecycle.DISABLED,
            "control_enabled": False,
            "dispatch_enabled": False,
            "revision": 7,
            "updated_at_unix_ms": 1_900,
        }
    )
    store.commit(disable_again, current, committed_at_unix_ms=1_900)

    remove = next_operation(
        current,
        RegistryOperationKind.REMOVE,
        10,
        retired_credential_refs=(NEW_MOONRAKER_REF, NEW_COMPATIBILITY_REF),
    )
    store.reserve(remove)
    current = current.model_copy(
        update={
            "lifecycle": PrinterLifecycle.REMOVED,
            "moonraker_credential_ref": None,
            "compatibility_credential_ref": None,
            "revision": 8,
            "updated_at_unix_ms": 2_000,
        }
    )
    store.commit(remove, current, committed_at_unix_ms=2_000)

    assert store.get(PRINTER_UUID) == current
    assert store.list() == ()
    assert store.list(include_removed=True) == (current,)
    assert secrets.references() == frozenset()
    audit = store.operations(PRINTER_UUID)
    assert len(audit) == 8
    assert all(record.state is RegistryOperationState.COMMITTED for record in audit)
    assert all(not record.retired_credential_refs for record in audit)

    restarted = PrinterStore(tmp_path / "registry.sqlite3", secrets)
    restarted.initialize()
    assert restarted.get(PRINTER_UUID) == current


def test_bootstrap_import_uses_the_same_exact_create_contract(tmp_path: Path) -> None:
    secrets, store = make_stores(tmp_path)
    pending = operation(operation=RegistryOperationKind.BOOTSTRAP_IMPORT)
    store.reserve(pending)
    write_printer_secrets(secrets)

    result = store.commit(pending, printer(), committed_at_unix_ms=1_000)
    assert result.operation is RegistryOperationKind.BOOTSTRAP_IMPORT


def test_registry_database_contains_references_but_never_secret_values(tmp_path: Path) -> None:
    secrets, store = make_stores(tmp_path)
    create_printer(secrets, store)

    with closing(sqlite3.connect(tmp_path / "registry.sqlite3")) as connection:
        dump = "\n".join(connection.iterdump())
    assert MOONRAKER_REF in dump
    assert COMPATIBILITY_REF in dump
    assert "m" * 32 not in dump
    assert "c" * 20 not in dump


def test_abort_retry_is_idempotent_and_never_bypasses_printer_serialization(
    tmp_path: Path,
) -> None:
    secrets, store = make_stores(tmp_path)
    first = operation()
    store.reserve(first)
    write_printer_secrets(secrets)
    aborted = store.abort(first)
    assert aborted.state is RegistryOperationState.ABORTED
    assert secrets.references() == frozenset()

    replacement = operation(
        new_credential_refs=(NEW_MOONRAKER_REF, NEW_COMPATIBILITY_REF),
    )
    retried, created = store.reserve(replacement)
    assert created is True
    assert retried.attempt == 2
    assert retried.new_credential_refs == replacement.new_credential_refs
    store.abort(retried)

    blocker = operation(idempotency_key=OTHER_IDEMPOTENCY_KEY)
    store.reserve(blocker)
    with pytest.raises(RegistryBusyError):
        store.reserve(replacement)

    other_printer = operation(
        idempotency_key=key(12),
        printer_uuid=OTHER_PRINTER_UUID,
        new_credential_refs=(OTHER_MOONRAKER_REF, OTHER_COMPATIBILITY_REF),
    )
    assert store.reserve(other_printer) == (other_printer, True)


@pytest.mark.parametrize(
    "updates",
    [
        {"operation": RegistryOperationKind.BOOTSTRAP_IMPORT},
        {"printer_uuid": OTHER_PRINTER_UUID},
        {"request_fingerprint": "b" * 64},
        {"actor": "owner:other"},
        {"request_origin": "https://other.example.test"},
        {"expected_revision": 1},
    ],
)
def test_idempotency_key_rejects_every_request_identity_collision(
    tmp_path: Path,
    updates: dict[str, object],
) -> None:
    _secrets, store = make_stores(tmp_path)
    store.reserve(operation())
    with pytest.raises(RegistryConflictError):
        store.reserve(operation(**updates))


def test_public_transitions_require_their_exact_source_state(tmp_path: Path) -> None:
    secrets, store = make_stores(tmp_path)
    committed = operation(
        state=RegistryOperationState.COMMITTED,
        result=printer(),
        committed_at_unix_ms=1_000,
    )
    aborted = operation(state=RegistryOperationState.ABORTED, error_code="interrupted")

    with pytest.raises(RegistryTransitionError):
        store.reserve(committed)
    with pytest.raises(RegistryTransitionError):
        store.commit(committed, printer(), committed_at_unix_ms=1_000)
    with pytest.raises(RegistryTransitionError):
        store.abort(aborted)
    with pytest.raises(RegistryTransitionError):
        store.finalize(aborted)

    store.reserve(operation())
    with pytest.raises(RegistryTransitionError):
        store.commit(operation(), printer(), committed_at_unix_ms=1_000)
    assert secrets.references() == frozenset()


def test_commit_rejects_invalid_commit_time_and_secret_failures(tmp_path: Path) -> None:
    secrets, store = make_stores(tmp_path)
    pending = operation()
    store.reserve(pending)
    write_printer_secrets(secrets)
    with pytest.raises(RegistryTransitionError):
        store.commit(pending, printer(), committed_at_unix_ms=949)

    (tmp_path / "registry-secrets" / MOONRAKER_REF).chmod(0o644)
    with pytest.raises(RegistryStoreError):
        store.commit(pending, printer(), committed_at_unix_ms=1_000)


@pytest.mark.parametrize("compatibility_secret", ["c" * 21, "!" * 20])
def test_commit_requires_exact_twenty_character_base64url_compatibility_secret(
    tmp_path: Path,
    compatibility_secret: str,
) -> None:
    secrets, store = make_stores(tmp_path)
    pending = operation()
    store.reserve(pending)
    secrets.write(MOONRAKER_REF, "m" * 32, minimum_length=32)
    secrets.write(COMPATIBILITY_REF, compatibility_secret, minimum_length=20)

    with pytest.raises(RegistryTransitionError):
        store.commit(pending, printer(), committed_at_unix_ms=1_000)


def test_query_methods_wrap_corrupt_rows_at_the_storage_boundary(tmp_path: Path) -> None:
    secrets, store = make_stores(tmp_path)
    current = create_printer(secrets, store)
    path = tmp_path / "registry.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE registry_printers SET record_json = ?", ("not-json",))
    with pytest.raises(RegistryStoreError):
        store.get(PRINTER_UUID)
    with pytest.raises(RegistryStoreError):
        store.list()

    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE registry_printers SET record_json = ?",
            (_printer_row(current)[-1],),
        )
        connection.execute("UPDATE registry_operations SET record_json = ?", ("not-json",))
    with pytest.raises(RegistryStoreError):
        store.lookup_operation(IDEMPOTENCY_KEY)
    with pytest.raises(RegistryStoreError):
        store.operations(PRINTER_UUID)
    with pytest.raises(RegistryStoreError):
        store._all_operations()


def test_initialize_rolls_back_schema_creation_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _secrets, store = make_stores(tmp_path)
    calls: list[str] = []

    class Cursor:
        def fetchone(self) -> tuple[int]:
            return (0,)

    class Connection:
        def execute(self, query: str, _params: object = None) -> Cursor:
            operation_name = query.strip().split(maxsplit=1)[0]
            calls.append(operation_name)
            if operation_name == "CREATE":
                raise sqlite3.OperationalError
            return Cursor()

        def close(self) -> None:
            calls.append("CLOSE")

    monkeypatch.setattr(store, "_connect", Connection)
    with pytest.raises(RegistryStoreError):
        store.initialize()
    assert "ROLLBACK" in calls
    assert calls[-1] == "CLOSE"


def test_reserve_wraps_begin_failures_and_closes_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _secrets, store = make_stores(tmp_path)
    closed = False

    class Connection:
        def execute(self, _query: str, _params: object = None) -> None:
            raise sqlite3.OperationalError

        def close(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(store, "_connect", Connection)
    with pytest.raises(RegistryStoreError):
        store.reserve(operation())
    assert closed


def test_commit_maps_unique_collisions_and_database_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets, store = make_stores(tmp_path)
    create_printer(secrets, store)
    collision = operation(
        idempotency_key=OTHER_IDEMPOTENCY_KEY,
        printer_uuid=OTHER_PRINTER_UUID,
        new_credential_refs=(OTHER_MOONRAKER_REF, OTHER_COMPATIBILITY_REF),
    )
    store.reserve(collision)
    write_printer_secrets(
        secrets,
        moonraker_ref=OTHER_MOONRAKER_REF,
        compatibility_ref=OTHER_COMPATIBILITY_REF,
    )
    conflicting_result = printer(
        printer_uuid=OTHER_PRINTER_UUID,
        moonraker_credential_ref=OTHER_MOONRAKER_REF,
        compatibility_credential_ref=OTHER_COMPATIBILITY_REF,
        safety_profiles=(safety_profile(printer_uuid=OTHER_PRINTER_UUID),),
    )
    with pytest.raises(RegistryConflictError):
        store.commit(collision, conflicting_result, committed_at_unix_ms=1_000)

    monkeypatch.setattr(
        registry_module,
        "_commit_transaction",
        lambda *_args: (_ for _ in ()).throw(sqlite3.OperationalError),
    )
    with pytest.raises(RegistryStoreError):
        store.commit(collision, conflicting_result, committed_at_unix_ms=1_000)


def test_abort_and_finalize_wrap_secret_deletion_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets, store = make_stores(tmp_path)
    pending = operation()
    store.reserve(pending)
    monkeypatch.setattr(
        secrets,
        "delete",
        lambda _reference: (_ for _ in ()).throw(SecretStoreError),
    )
    with pytest.raises(RegistryStoreError):
        store.abort(pending)

    finalize_secrets, finalize_store = make_stores(tmp_path / "finalize")
    current = create_printer(finalize_secrets, finalize_store)
    rotation = next_operation(
        current,
        RegistryOperationKind.ROTATE_MOONRAKER,
        60,
        new_credential_refs=(NEW_MOONRAKER_REF,),
        retired_credential_refs=(MOONRAKER_REF,),
    )
    finalize_store.reserve(rotation)
    finalize_secrets.write(NEW_MOONRAKER_REF, "n" * 32, minimum_length=32)
    result = current.model_copy(
        update={
            "moonraker_credential_ref": NEW_MOONRAKER_REF,
            "revision": 2,
            "updated_at_unix_ms": 7_000,
        }
    )
    monkeypatch.setattr(
        finalize_secrets,
        "delete",
        lambda _reference: (_ for _ in ()).throw(SecretStoreError),
    )
    with pytest.raises(RegistryStoreError):
        finalize_store.commit(rotation, result, committed_at_unix_ms=7_000)


def test_reconcile_never_deletes_a_committed_active_reference(tmp_path: Path) -> None:
    secrets, store = make_stores(tmp_path)
    current = create_printer(secrets, store)
    unsafe = operation(
        idempotency_key=key(61),
        operation=RegistryOperationKind.UPDATE,
        expected_revision=1,
        new_credential_refs=(),
        retired_credential_refs=(MOONRAKER_REF,),
        state=RegistryOperationState.COMMITTED,
        result=current,
        committed_at_unix_ms=1_000,
    )
    with closing(sqlite3.connect(tmp_path / "registry.sqlite3")) as connection, connection:
        connection.execute(
            """
            INSERT INTO registry_operations (
                idempotency_key, operation, printer_uuid,
                request_fingerprint, state, record_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            _operation_row(unsafe),
        )

    with pytest.raises(RegistryStoreError):
        store.finalize(unsafe)
    with pytest.raises(RegistryStoreError):
        store.reconcile()
    assert secrets.contains(MOONRAKER_REF)


def test_backup_closes_source_when_target_connection_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _secrets, store = make_stores(tmp_path)
    real_connect = sqlite3.connect
    calls = 0

    def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal calls
        del kwargs
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError
        return real_connect(str(args[0]))

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(RegistryStoreError):
        store.backup(tmp_path / "failed-backup.sqlite3")


def test_cleanup_operation_detects_stale_rows_and_wraps_commit_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _secrets, store = make_stores(tmp_path)
    previous = operation()
    changed = operation(state=RegistryOperationState.ABORTED, error_code="interrupted")
    with pytest.raises(RegistryConflictError):
        store._cleanup_operation(previous, changed, ())

    calls: list[str] = []

    class Cursor:
        def __init__(self, rowcount: int = 1) -> None:
            self.rowcount = rowcount

        def fetchone(self) -> tuple[object, ...]:
            return _operation_row(previous)

        def fetchall(self) -> list[tuple[object, ...]]:
            return []

    class Connection:
        def __init__(self, *, commit_error: bool = False, update_rowcount: int = 1) -> None:
            self._commit_error = commit_error
            self._update_rowcount = update_rowcount

        def execute(self, query: str, _params: object = None) -> Cursor:
            operation_name = query.strip().split(maxsplit=1)[0]
            calls.append(operation_name)
            if operation_name == "COMMIT" and self._commit_error:
                raise sqlite3.OperationalError
            return Cursor(self._update_rowcount if operation_name == "UPDATE" else 1)

        def close(self) -> None:
            calls.append("CLOSE")

    monkeypatch.setattr(store, "_connect", lambda: Connection(update_rowcount=0))
    with pytest.raises(RegistryConflictError):
        store._cleanup_operation(previous, changed, ())
    assert calls[-2:] == ["ROLLBACK", "CLOSE"]

    calls.clear()
    monkeypatch.setattr(store, "_connect", lambda: Connection(commit_error=True))
    with pytest.raises(RegistryStoreError):
        store._cleanup_operation(previous, changed, ())
    assert calls[-2:] == ["ROLLBACK", "CLOSE"]


@pytest.mark.parametrize("failure", ["journal_mode", "pragma", "connect"])
def test_connect_closes_every_failed_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    path = tmp_path / "registry.sqlite3"
    path.touch(mode=0o600)
    closed = False

    class Cursor:
        def fetchone(self) -> tuple[str]:
            return ("delete",)

    class Connection:
        def execute(self, query: str) -> Cursor:
            if failure == "pragma" and "busy_timeout" in query:
                raise sqlite3.OperationalError
            return Cursor()

        def close(self) -> None:
            nonlocal closed
            closed = True

    def connect(*_args: object, **_kwargs: object) -> Connection:
        if failure == "connect":
            raise sqlite3.OperationalError
        return Connection()

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(RegistryStoreError):
        PrinterStore(path, SecretStore(tmp_path))._connect()
    assert closed is (failure != "connect")


@pytest.mark.parametrize("failure", ["stored", "printer_update", "operation_update"])
def test_commit_transaction_rolls_back_every_optimistic_conflict(failure: str) -> None:
    current = printer()
    pending = next_operation(current, RegistryOperationKind.UPDATE, 70)
    result = current.model_copy(update={"revision": 2, "updated_at_unix_ms": 8_000})
    committed = RegistryOperationRecord.model_validate(
        {
            **pending.model_dump(mode="python"),
            "state": RegistryOperationState.COMMITTED,
            "result": result,
            "committed_at_unix_ms": 8_000,
        }
    )
    calls: list[str] = []

    class Cursor:
        def __init__(self, row: tuple[object, ...] | None = None, rowcount: int = 1) -> None:
            self._row = row
            self.rowcount = rowcount

        def fetchone(self) -> tuple[object, ...] | None:
            return self._row

    class Connection:
        def execute(self, query: str, _params: object = None) -> Cursor:
            normalized = " ".join(query.split())
            calls.append(normalized.split(maxsplit=1)[0])
            if normalized.startswith("SELECT idempotency_key"):
                return Cursor(None if failure == "stored" else _operation_row(pending))
            if normalized.startswith("SELECT printer_uuid"):
                return Cursor(_printer_row(current))
            if normalized.startswith("UPDATE registry_printers"):
                return Cursor(rowcount=0 if failure == "printer_update" else 1)
            if normalized.startswith("UPDATE registry_operations"):
                return Cursor(rowcount=0 if failure == "operation_update" else 1)
            return Cursor()

    with pytest.raises(RegistryConflictError):
        _commit_transaction(Connection(), pending, result, committed)  # type: ignore[arg-type]
    assert calls[-1] == "ROLLBACK"


class _SchemaCursor:
    def __init__(
        self,
        *,
        rows: list[tuple[object, ...]] | None = None,
        row: tuple[object, ...] | None = None,
    ) -> None:
        self._rows = [] if rows is None else rows
        self._row = row

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row


class _SchemaConnection:
    def __init__(self, failure: str) -> None:
        self._failure = failure

    def execute(  # noqa: PLR0911
        self, query: str, _params: object = None
    ) -> _SchemaCursor:
        if query.startswith("PRAGMA table_info"):
            rows: list[tuple[object, ...]] = [
                (0, "key", "TEXT", 1, None, 1),
                (1, "value", "TEXT", 1, None, 0),
            ]
            if self._failure == "definition":
                rows[0] = (0, "wrong", "TEXT", 1, None, 1)
            return _SchemaCursor(rows=rows)
        if query.startswith("PRAGMA table_list"):
            rows = [("main", "registry_metadata", "table", 2, 0, 1)]
            if self._failure == "table":
                rows = []
            return _SchemaCursor(rows=rows)
        if query.startswith("SELECT sql"):
            if self._failure == "sql_row":
                return _SchemaCursor(row=None)
            sql = registry_module._METADATA_TABLE_SQL
            if self._failure == "sql":
                sql = "CREATE TABLE registry_metadata (key TEXT) STRICT"
            return _SchemaCursor(row=(sql,))
        if query.startswith("PRAGMA index_list"):
            rows = [(0, "sqlite_autoindex_registry_metadata_1", 1, "pk", 0)]
            if self._failure == "index_row":
                rows = [(0,)]
            elif self._failure == "indexes":
                rows = []
            return _SchemaCursor(rows=rows)
        if self._failure == "index_info":
            return _SchemaCursor(rows=[])
        return _SchemaCursor(rows=[("key",)])


@pytest.mark.parametrize(
    "failure",
    ["definition", "table", "sql_row", "sql", "index_row", "index_info", "indexes"],
)
def test_table_schema_validation_rejects_each_structural_weakness(failure: str) -> None:
    with pytest.raises(RegistryStoreError):
        _validate_table(_SchemaConnection(failure), _METADATA_SCHEMA)  # type: ignore[arg-type]


def test_schema_validation_rejects_failed_integrity_check() -> None:
    class Cursor:
        def fetchall(self) -> list[tuple[str]]:
            return [("corrupt",)]

    class Connection:
        def execute(self, _query: str) -> Cursor:
            return Cursor()

    with pytest.raises(RegistryStoreError):
        _validate_schema(Connection())  # type: ignore[arg-type]


def test_backup_is_private_consistent_and_requires_the_matching_secret_set(
    tmp_path: Path,
) -> None:
    secrets, store = make_stores(tmp_path)
    expected = create_printer(secrets, store)
    backup = tmp_path / "backup.sqlite3"
    store.backup(backup)
    assert backup.stat().st_mode & 0o777 == 0o600

    restored = PrinterStore(backup, SecretStore(tmp_path / "registry-secrets"))
    restored.initialize()
    assert restored.get(PRINTER_UUID) == expected

    with pytest.raises(RegistryStoreError):
        store.backup(backup)
    with pytest.raises(RegistryStoreError):
        store.backup(tmp_path / "registry.sqlite3")

    broken_link = tmp_path / "broken-backup"
    broken_link.symlink_to(tmp_path / "absent")
    with pytest.raises(RegistryStoreError):
        store.backup(broken_link)


def test_initialize_rejects_future_schema_extra_tables_and_key_mismatch(tmp_path: Path) -> None:
    future_directory = tmp_path / "future-secrets"
    future_directory.mkdir(mode=0o700)
    future = tmp_path / "future.sqlite3"
    with closing(sqlite3.connect(future)) as connection, connection:
        connection.execute("PRAGMA user_version = 2")
    future.chmod(0o600)
    with pytest.raises(RegistryStoreError):
        PrinterStore(future, SecretStore(future_directory)).initialize()

    secrets, _store = make_stores(tmp_path / "extra")
    path = tmp_path / "extra" / "registry.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE unexpected (value TEXT) STRICT")
    with pytest.raises(RegistryStoreError):
        PrinterStore(path, secrets).initialize()

    secrets, _store = make_stores(tmp_path / "mismatch")
    path = tmp_path / "mismatch" / "registry.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE registry_metadata SET value = ?",
            ("0" * 64,),
        )
    with pytest.raises(RegistryStoreError):
        PrinterStore(path, secrets).initialize()


def test_initialize_recovers_preparing_and_committed_secret_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets, store = make_stores(tmp_path)
    current = create_printer(secrets, store)

    preparing = next_operation(
        current,
        RegistryOperationKind.ROTATE_MOONRAKER,
        20,
        new_credential_refs=(NEW_MOONRAKER_REF,),
        retired_credential_refs=(MOONRAKER_REF,),
    )
    store.reserve(preparing)
    secrets.write(NEW_MOONRAKER_REF, "n" * 32, minimum_length=32)
    PrinterStore(tmp_path / "registry.sqlite3", secrets).initialize()
    recovered = store.lookup_operation(preparing.idempotency_key)
    assert recovered is not None and recovered.state is RegistryOperationState.ABORTED
    assert not secrets.contains(NEW_MOONRAKER_REF)

    rotation = next_operation(
        current,
        RegistryOperationKind.ROTATE_MOONRAKER,
        21,
        new_credential_refs=(NEW_MOONRAKER_REF,),
        retired_credential_refs=(MOONRAKER_REF,),
    )
    store.reserve(rotation)
    secrets.write(NEW_MOONRAKER_REF, "n" * 32, minimum_length=32)
    result = current.model_copy(
        update={
            "moonraker_credential_ref": NEW_MOONRAKER_REF,
            "revision": 2,
            "updated_at_unix_ms": 3_100,
        }
    )
    original_finalize = store.finalize
    monkeypatch.setattr(
        store,
        "finalize",
        lambda _operation: (_ for _ in ()).throw(RegistryStoreError),
    )
    with pytest.raises(RegistryStoreError):
        store.commit(rotation, result, committed_at_unix_ms=3_100)
    monkeypatch.setattr(store, "finalize", original_finalize)

    PrinterStore(tmp_path / "registry.sqlite3", secrets).initialize()
    recovered = store.lookup_operation(rotation.idempotency_key)
    assert recovered is not None and recovered.retired_credential_refs == ()
    assert not secrets.contains(MOONRAKER_REF)


def test_reconcile_rejects_orphans_missing_secrets_and_unsafe_cleanup_overlap(
    tmp_path: Path,
) -> None:
    secrets, store = make_stores(tmp_path)
    current = create_printer(secrets, store)

    secrets.write(NEW_MOONRAKER_REF, "n" * 32, minimum_length=32)
    with pytest.raises(RegistryStoreError):
        store.reconcile()
    secrets.delete(NEW_MOONRAKER_REF)

    secrets.delete(MOONRAKER_REF)
    with pytest.raises(RegistryStoreError):
        store.reconcile()
    secrets.write(MOONRAKER_REF, "m" * 32, minimum_length=32)

    unsafe = next_operation(
        current,
        RegistryOperationKind.UPDATE,
        30,
        new_credential_refs=(MOONRAKER_REF,),
    )
    store.reserve(unsafe)
    with pytest.raises(RegistryStoreError):
        store.abort(unsafe)
    with pytest.raises(RegistryStoreError):
        store.reconcile()
    assert secrets.contains(MOONRAKER_REF)


def test_row_decoders_reject_malformed_noncanonical_and_contradictory_records() -> None:
    registered = printer()
    pending = operation()
    printer_row = _printer_row(registered)
    operation_row = _operation_row(pending)
    assert _decode_printer(printer_row) == registered
    assert _decode_operation(operation_row) == pending

    malformed_printer_rows = [
        (),
        (1, *printer_row[1:]),
        (*printer_row[:3], "1", *printer_row[4:]),
        (*printer_row[:1], "https://different.example", *printer_row[2:]),
        (*printer_row[:-1], "not-json"),
        (*printer_row[:-1], json.dumps(registered.model_dump(mode="json"), sort_keys=True)),
    ]
    for row in malformed_printer_rows:
        with pytest.raises((RegistryStoreError, ValidationError)):
            _decode_printer(row)

    malformed_operation_rows = [
        (),
        (1, *operation_row[1:]),
        (*operation_row[:1], "update", *operation_row[2:]),
        (*operation_row[:-1], "not-json"),
        (*operation_row[:-1], json.dumps(pending.model_dump(mode="json"), sort_keys=True)),
    ]
    for row in malformed_operation_rows:
        with pytest.raises((RegistryStoreError, ValidationError)):
            _decode_operation(row)


def test_low_level_pragma_and_required_reference_decoders_are_strict() -> None:
    class Cursor:
        def __init__(self, row: tuple[object, ...] | None) -> None:
            self._row = row

        def fetchone(self) -> tuple[object, ...] | None:
            return self._row

    class Connection:
        def __init__(self, row: tuple[object, ...] | None) -> None:
            self._row = row

        def execute(self, _query: str) -> Cursor:
            return Cursor(self._row)

    for value in (None, (), (True,), ("1",), (1, 2)):
        with pytest.raises(RegistryStoreError):
            _pragma_integer(Connection(value), "user_version")  # type: ignore[arg-type]
    assert _required_reference(MOONRAKER_REF) == MOONRAKER_REF
    with pytest.raises(RegistryTransitionError):
        _required_reference(None)


def test_schema_decoder_rejects_malformed_pragma_rows() -> None:
    class Cursor:
        def __init__(self, rows: list[tuple[object, ...]]) -> None:
            self._rows = rows

        def fetchall(self) -> list[tuple[object, ...]]:
            return self._rows

    class Connection:
        def execute(self, query: str, _params: object = None) -> Cursor:
            if "quick_check" in query:
                return Cursor([("ok",)])
            if "sqlite_master" in query and "type = 'table'" in query:
                return Cursor(
                    [
                        ("registry_metadata",),
                        ("registry_operations",),
                        ("registry_printers",),
                    ]
                )
            return Cursor([("short",)])

    with pytest.raises(RegistryStoreError):
        _validate_schema(Connection())  # type: ignore[arg-type]


def test_transition_validator_rejects_wrong_create_and_revision_evidence() -> None:
    create = operation()
    current = printer()
    invalid = (
        (
            create,
            None,
            printer(
                printer_uuid=OTHER_PRINTER_UUID,
                safety_profiles=(safety_profile(printer_uuid=OTHER_PRINTER_UUID),),
            ),
        ),
        (create, current, printer()),
        (operation(expected_revision=1), None, printer()),
        (create, None, printer(revision=2)),
        (
            create,
            None,
            printer(
                lifecycle=PrinterLifecycle.DISABLED,
                control_enabled=False,
                dispatch_enabled=False,
            ),
        ),
        (create, None, printer(updated_at_unix_ms=1_001)),
        (operation(new_credential_refs=(MOONRAKER_REF,)), None, printer()),
        (operation(retired_credential_refs=(NEW_MOONRAKER_REF,)), None, printer()),
        (next_operation(current, RegistryOperationKind.UPDATE, 40), None, current),
        (
            next_operation(current, RegistryOperationKind.UPDATE, 41, expected_revision=2),
            current,
            current.model_copy(update={"revision": 2, "updated_at_unix_ms": 5_100}),
        ),
        (
            next_operation(current, RegistryOperationKind.UPDATE, 42),
            current,
            current.model_copy(update={"revision": 3, "updated_at_unix_ms": 5_200}),
        ),
        (
            next_operation(current, RegistryOperationKind.UPDATE, 43),
            current,
            current.model_copy(
                update={"revision": 2, "created_at_unix_ms": 999, "updated_at_unix_ms": 5_300}
            ),
        ),
        (
            next_operation(current, RegistryOperationKind.UPDATE, 44),
            current,
            current.model_copy(update={"revision": 2, "updated_at_unix_ms": 999}),
        ),
    )
    for pending, existing, result in invalid:
        with pytest.raises(RegistryTransitionError):
            _validate_transition(pending, existing, result)


def test_transition_validator_rejects_semantically_widened_mutations() -> None:
    current = printer()
    changed_ref = current.model_copy(
        update={
            "moonraker_credential_ref": NEW_MOONRAKER_REF,
            "revision": 2,
            "updated_at_unix_ms": 2_000,
        }
    )
    disabled = current.model_copy(
        update={
            "lifecycle": PrinterLifecycle.DISABLED,
            "control_enabled": False,
            "dispatch_enabled": False,
            "revision": 2,
            "updated_at_unix_ms": 2_000,
        }
    )
    removed = disabled.model_copy(
        update={
            "lifecycle": PrinterLifecycle.REMOVED,
            "moonraker_credential_ref": None,
            "compatibility_credential_ref": None,
            "revision": 3,
            "updated_at_unix_ms": 3_000,
        }
    )
    cases = (
        (
            next_operation(current, RegistryOperationKind.UPDATE, 50),
            current,
            disabled,
        ),
        (
            next_operation(current, RegistryOperationKind.UPDATE, 51),
            current,
            changed_ref,
        ),
        (
            next_operation(
                current,
                RegistryOperationKind.ROTATE_MOONRAKER,
                52,
                new_credential_refs=(NEW_MOONRAKER_REF,),
                retired_credential_refs=(MOONRAKER_REF,),
            ),
            current,
            changed_ref.model_copy(update={"display_name": "Unexpected"}),
        ),
        (
            next_operation(current, RegistryOperationKind.DISABLE, 53),
            current,
            disabled.model_copy(update={"display_name": "Unexpected"}),
        ),
        (
            next_operation(
                current,
                RegistryOperationKind.REMOVE,
                54,
                retired_credential_refs=(MOONRAKER_REF, COMPATIBILITY_REF),
            ),
            current,
            removed.model_copy(update={"revision": 2}),
        ),
        (
            next_operation(
                disabled,
                RegistryOperationKind.REMOVE,
                55,
                retired_credential_refs=(MOONRAKER_REF, COMPATIBILITY_REF),
            ),
            disabled,
            removed.model_copy(update={"display_name": "Unexpected"}),
        ),
    )
    for pending, existing, result in cases:
        with pytest.raises(RegistryTransitionError):
            _validate_transition(pending, existing, result)


def test_store_wraps_secret_adapter_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets, store = make_stores(tmp_path)
    pending = operation()
    store.reserve(pending)
    monkeypatch.setattr(
        secrets, "contains", lambda _reference: (_ for _ in ()).throw(SecretStoreError)
    )
    with pytest.raises(RegistryStoreError):
        store.commit(pending, printer(), committed_at_unix_ms=1_000)
