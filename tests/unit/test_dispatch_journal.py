from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

import pytest

from klove.domain.artifacts import PlateSelection, target_for_safety_profile
from klove.domain.dispatch import (
    DispatchFailure,
    DispatchFailureCode,
    DispatchGrant,
    DispatchJournalRecord,
    DispatchRequest,
    DispatchState,
)
from klove.errors import JournalError
from klove.persistence import dispatch_journal
from klove.persistence.dispatch_journal import (
    DispatchJournal,
    DispatchJournalCapacityError,
    DispatchJournalConflictError,
    DispatchJournalFenceError,
)

from ..start_helpers import OPERATION_ID, safety_profile

INSTALLATION_ID = "99999999-9999-4999-8999-999999999999"


def request(**updates: object) -> DispatchRequest:
    target = target_for_safety_profile(safety_profile())
    values: dict[str, object] = {
        "operation_id": OPERATION_ID,
        "idempotency_key": "33333333-3333-4333-8333-333333333333",
        "printer_uuid": target.printer_uuid,
        "slicer_profile_id": target.slicer_profile_id,
        "safety_profile_generation": target.safety_profile_generation,
        "safety_profile_fingerprint": target.safety_profile_fingerprint,
        "selected_plate": PlateSelection(plate_id="1", archive_path="Metadata/plate_1.gcode"),
        "archive_sha256": "sha256:" + hashlib.sha256(b"archive").hexdigest(),
        "archive_size_bytes": len(b"archive"),
    }
    values.update(updates)
    return DispatchRequest.model_validate(values)


def record(**updates: object) -> DispatchJournalRecord:
    submitted = request()
    values: dict[str, object] = {
        "grant": DispatchGrant(principal_id="actor-1", printer_uuid=submitted.printer_uuid),
        "request": submitted,
        "state": DispatchState.RECEIVING,
    }
    values.update(updates)
    return DispatchJournalRecord.model_validate(values)


def journal(tmp_path: Path) -> DispatchJournal:
    tmp_path.chmod(0o700)
    value = DispatchJournal(tmp_path / "dispatch.sqlite3", INSTALLATION_ID)
    value.initialize()
    return value


def failed(value: DispatchJournalRecord) -> DispatchJournalRecord:
    return DispatchJournalRecord.model_validate(
        value.model_dump()
        | {
            "state": DispatchState.FAILED,
            "failure": DispatchFailure(code=DispatchFailureCode.SOURCE_INVALID),
        }
    )


def test_journal_reserves_replaces_and_reloads_exact_durable_record(tmp_path: Path) -> None:
    value = journal(tmp_path)
    initial = record()

    stored, created = value.reserve(initial, capacity=2)
    duplicate, duplicate_created = value.reserve(initial, capacity=2)
    terminal = value.replace(stored, failed(stored))

    assert created is True
    assert duplicate_created is False
    assert duplicate == stored == initial
    assert terminal.state is DispatchState.FAILED
    assert value.lookup(initial.request.operation_id, initial.request.idempotency_key) == terminal
    assert (
        value.lookup(initial.request.operation_id, "55555555-5555-4555-8555-555555555555") is None
    )
    assert (
        value.lookup("55555555-5555-4555-8555-555555555555", initial.request.idempotency_key)
        is None
    )
    assert value.records() == (terminal,)
    restarted = DispatchJournal(tmp_path / "dispatch.sqlite3", INSTALLATION_ID)
    restarted.initialize()
    assert restarted.records() == (terminal,)


def test_identity_and_exact_operation_state_lookup(tmp_path: Path) -> None:
    value = DispatchJournal(tmp_path / "uninitialized.sqlite3", INSTALLATION_ID)
    with pytest.raises(JournalError):
        _ = value.identity
    value = journal(tmp_path)
    initial = record()
    assert value.lookup_operation_state(initial.request.operation_id) is None
    value.reserve(initial, capacity=2)
    identity = value.identity
    assert identity.installation_id == INSTALLATION_ID
    assert identity.store_id != identity.installation_id
    assert identity.schema_version == 2
    assert value.lookup_operation(initial.request.operation_id) == initial
    assert value.lookup_operation_state(initial.request.operation_id) is DispatchState.RECEIVING
    assert value.lookup_operation_state("55555555-5555-4555-8555-555555555555") is None
    with pytest.raises(JournalError):
        value.lookup_operation_state("not-an-operation")
    restarted = DispatchJournal(tmp_path / "dispatch.sqlite3", INSTALLATION_ID)
    restarted.initialize()
    assert restarted.identity == identity


def test_v1_migration_adds_metadata_without_changing_operation_rows(tmp_path: Path) -> None:
    path = tmp_path / "dispatch.sqlite3"
    initial = record()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE dispatch_operations (
                operation_id TEXT PRIMARY KEY NOT NULL,
                idempotency_key TEXT UNIQUE NOT NULL,
                printer_uuid TEXT NOT NULL,
                state TEXT NOT NULL,
                record_json TEXT NOT NULL
            ) STRICT
            """
        )
        connection.execute(
            """
            INSERT INTO dispatch_operations (
                operation_id, idempotency_key, printer_uuid, state, record_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                initial.request.operation_id,
                initial.request.idempotency_key,
                initial.request.printer_uuid,
                initial.state.value,
                initial.model_dump_json(),
            ),
        )
        connection.execute("PRAGMA user_version = 1")
    path.chmod(0o600)

    value = DispatchJournal(path, INSTALLATION_ID)
    value.initialize()
    assert value.lookup_operation_state(initial.request.operation_id) is DispatchState.RECEIVING
    assert value.identity.installation_id == INSTALLATION_ID
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM dispatch_operations").fetchone() == (1,)


def test_identity_metadata_mismatch_and_extra_rows_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "dispatch.sqlite3"
    value = journal(tmp_path)
    value.initialize()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TRIGGER dispatch_metadata_no_update")
        connection.execute(
            "UPDATE dispatch_metadata SET value = ? WHERE key = 'installation_id'",
            ("88888888-8888-4888-8888-888888888888",),
        )
    with pytest.raises(JournalError):
        value.initialize()

    clean_path = tmp_path / "extra.sqlite3"
    clean = DispatchJournal(clean_path, INSTALLATION_ID)
    clean.initialize()
    with closing(sqlite3.connect(clean_path)) as connection, connection:
        connection.execute("INSERT INTO dispatch_metadata (key, value) VALUES ('future', 'x')")
    with pytest.raises(JournalError):
        clean.initialize()


def test_dispatch_schema_validation_rejects_each_metadata_structure_weakness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dispatch_journal, "_validate_operation_schema", lambda _connection: None)

    class Schema:
        def __init__(self, mode: str) -> None:
            self.mode = mode
            self.query = ""

        def execute(self, query: str, _params: object = None) -> Schema:
            self.query = query
            return self

        def fetchall(self) -> list[tuple[object, ...]]:  # noqa: PLR0911
            if "type = 'table'" in self.query:
                return [("dispatch_metadata",), ("dispatch_operations",)]
            if self.query.startswith("PRAGMA table_info"):
                if self.mode == "short_columns":
                    return [("short",)]
                if self.mode == "bad_columns":
                    return [
                        (0, "key", "BLOB", 1, None, 1),
                        (1, "value", "TEXT", 1, None, 0),
                    ]
                return [
                    (0, "key", "TEXT", 1, None, 1),
                    (1, "value", "TEXT", 1, None, 0),
                ]
            if self.query.startswith("PRAGMA index_list"):
                if self.mode == "bad_index":
                    return [("short",)]
                return [(0, "index", 1, "c", 0)]
            if self.query.startswith("SELECT name FROM pragma_index_info"):
                return [] if self.mode == "bad_index_columns" else [("key",)]
            if "type = 'trigger'" in self.query:
                return [] if self.mode == "bad_triggers" else [
                    ("dispatch_metadata_no_delete",),
                    ("dispatch_metadata_no_update",),
                ]
            return []

    for mode in (
        "short_columns",
        "bad_columns",
        "bad_index",
        "bad_index_columns",
        "bad_triggers",
    ):
        with pytest.raises(JournalError):
            dispatch_journal._validate_schema(Schema(mode))  # type: ignore[arg-type]


def test_dispatch_identity_and_operation_schema_helpers_reject_corruption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Rows:
        def __init__(self, values: list[tuple[object, ...]]) -> None:
            self.values = values

        def execute(self, _query: str, _params: object = None) -> Rows:
            return self

        def fetchall(self) -> list[tuple[object, ...]]:
            return self.values

    valid_store = "88888888-8888-4888-8888-888888888888"
    for rows in (
        [("short",)],
        [("future", "x"), ("store_id", valid_store)],
        [
            ("installation_id", "77777777-7777-4777-8777-777777777777"),
            ("store_id", valid_store),
        ],
    ):
        with pytest.raises(JournalError):
            dispatch_journal._read_store_id(Rows(rows), INSTALLATION_ID)  # type: ignore[arg-type]

    class OperationSchema:
        def __init__(self, mode: str) -> None:
            self.mode = mode
            self.query = ""

        def execute(self, query: str, _params: object = None) -> OperationSchema:
            self.query = query
            return self

        def fetchall(self) -> list[tuple[object, ...]]:  # noqa: PLR0911
            if self.query.startswith("PRAGMA table_info"):
                if self.mode == "short_columns":
                    return [("short",)]
                columns = [
                    (0, "operation_id", "TEXT", 1, None, 1),
                    (1, "idempotency_key", "TEXT", 1, None, 0),
                    (2, "printer_uuid", "TEXT", 1, None, 0),
                    (3, "state", "TEXT", 1, None, 0),
                    (4, "record_json", "TEXT", 1, None, 0),
                ]
                if self.mode == "bad_columns":
                    columns[1] = (1, "idempotency_key", "BLOB", 1, None, 0)
                return columns
            if self.query.startswith("PRAGMA index_list"):
                if self.mode == "bad_index":
                    return [("short",)]
                return [
                    (0, "operation_index", 1, "c", 0),
                    (1, "key_index", 1, "c", 0),
                ]
            if self.mode == "bad_index_name_type":
                return [(1,)]
            if self.mode == "bad_index_set":
                return [("wrong",)]
            return (
                [("operation_id",)]
                if "operation_index" in self.query
                else [("idempotency_key",)]
            )

    for mode in (
        "short_columns",
        "bad_columns",
        "bad_index",
        "bad_index_name_type",
        "bad_index_set",
    ):
        with pytest.raises(JournalError):
            dispatch_journal._validate_operation_schema(OperationSchema(mode))  # type: ignore[arg-type]

    with pytest.raises(JournalError):
        dispatch_journal._validate_v1_schema(Rows([]))  # type: ignore[arg-type]

    class FailedConnection:
        def execute(self, _query: str, _params: object = None) -> object:
            raise sqlite3.OperationalError

        def close(self) -> None:
            return None

    store = DispatchJournal(tmp_path / "lookup.sqlite3", INSTALLATION_ID)

    def failed_connect() -> FailedConnection:
        return FailedConnection()

    monkeypatch.setattr(store, "_connect", failed_connect)
    with pytest.raises(JournalError):
        store.lookup_operation("99999999-9999-4999-8999-999999999999")


def test_dispatch_v1_migration_rolls_back_on_invalid_schema(tmp_path: Path) -> None:
    path = tmp_path / "invalid-v1.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE dispatch_operations (operation_id TEXT PRIMARY KEY NOT NULL) STRICT"
        )
        connection.execute("CREATE TABLE extra_table (value TEXT) STRICT")
        connection.execute("PRAGMA user_version = 1")
    path.chmod(0o600)
    with pytest.raises(JournalError):
        DispatchJournal(path, INSTALLATION_ID).initialize()
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'dispatch_metadata'"
        ).fetchone() is None


def test_journal_rejects_fence_capacity_collision_and_stale_replace(tmp_path: Path) -> None:
    value = journal(tmp_path)
    initial = record()
    value.reserve(initial, capacity=1)
    other = record(
        request=request(
            operation_id="55555555-5555-4555-8555-555555555555",
            idempotency_key="66666666-6666-4666-8666-666666666666",
        )
    )

    with pytest.raises(DispatchJournalFenceError):
        value.reserve(other, capacity=2)
    with pytest.raises(DispatchJournalCapacityError):
        value.reserve(other, capacity=1)
    with pytest.raises(DispatchJournalConflictError):
        value.replace(failed(initial), failed(initial))
    conflict = record(request=request(idempotency_key="77777777-7777-4777-8777-777777777777"))
    returned, created = value.reserve(conflict, capacity=2)
    assert created is False
    assert returned == initial


def test_journal_rejects_corrupt_rows_schema_and_private_path(tmp_path: Path) -> None:
    path = tmp_path / "dispatch.sqlite3"
    value = journal(tmp_path)
    initial = record()
    value.reserve(initial, capacity=2)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE dispatch_operations SET record_json = '{}' ")
    with pytest.raises(JournalError):
        value.records()
    path.chmod(0o644)
    with pytest.raises(JournalError):
        value.initialize()


def test_journal_rejects_persisted_pre_reservation_or_impossible_failure_codes(
    tmp_path: Path,
) -> None:
    value = journal(tmp_path)
    initial = record()
    value.reserve(initial, capacity=2)
    invalid = initial.model_copy(
        update={
            "state": DispatchState.FAILED,
            "failure": DispatchFailure(code=DispatchFailureCode.ACCESS_DENIED),
        }
    )
    with closing(sqlite3.connect(tmp_path / "dispatch.sqlite3")) as connection, connection:
        connection.execute(
            "UPDATE dispatch_operations SET state = ?, record_json = ?",
            (DispatchState.FAILED.value, invalid.model_dump_json()),
        )

    with pytest.raises(JournalError):
        value.records()


def test_journal_wraps_corrupt_lookup_and_schema_creation_failures(tmp_path: Path) -> None:
    value = journal(tmp_path)
    initial = record()
    value.reserve(initial, capacity=2)
    with closing(sqlite3.connect(tmp_path / "dispatch.sqlite3")) as connection, connection:
        connection.execute("UPDATE dispatch_operations SET record_json = '{}' ")
    with pytest.raises(JournalError):
        value.lookup(initial.request.operation_id, initial.request.idempotency_key)

    conflict_path = tmp_path / "conflict.sqlite3"
    conflict_path.touch(mode=0o600)
    conflict_path.chmod(0o600)
    with closing(sqlite3.connect(conflict_path)) as connection, connection:
        connection.execute("CREATE TABLE dispatch_operations (wrong TEXT) STRICT")
    with pytest.raises(JournalError):
        DispatchJournal(conflict_path, INSTALLATION_ID).initialize()


def test_journal_rejects_invalid_inputs_versions_and_substituted_rows(tmp_path: Path) -> None:
    value = journal(tmp_path)
    initial = record()

    with pytest.raises(JournalError):
        value.reserve(failed(initial), capacity=2)
    with pytest.raises(JournalError):
        value.reserve(initial, capacity=0)
    with pytest.raises(DispatchJournalConflictError):
        value.replace(
            initial,
            record(
                request=request(
                    operation_id="55555555-5555-4555-8555-555555555555",
                    idempotency_key="66666666-6666-4666-8666-666666666666",
                )
            ),
        )
    with pytest.raises(DispatchJournalConflictError):
        dispatch_journal._one([("one",), ("two",)])
    with pytest.raises(JournalError):
        dispatch_journal._decode(("only",))
    with closing(sqlite3.connect(tmp_path / "dispatch.sqlite3")) as connection, connection:
        connection.execute("PRAGMA user_version = 3")
    with pytest.raises(JournalError):
        value.initialize()


def test_journal_schema_helpers_reject_bad_pragmas_and_index_layouts() -> None:
    class BrokenPragma:
        def execute(self, _query: str) -> BrokenPragma:
            return self

        def fetchone(self) -> tuple[object, ...]:
            return ("not-an-int",)

    class BrokenSchema:
        def execute(self, query: str, _params: object = None) -> BrokenSchema:
            self.query = query
            return self

        def fetchall(self) -> list[tuple[object, ...]]:
            if self.query.startswith("PRAGMA table_info"):
                return [("too", "short")]
            return []

    with pytest.raises(JournalError):
        dispatch_journal._pragma_integer(BrokenPragma(), "user_version")  # type: ignore[arg-type]
    with pytest.raises(JournalError):
        dispatch_journal._validate_operation_schema(BrokenSchema())  # type: ignore[arg-type]


def test_journal_helpers_reject_mismatched_row_and_non_wal_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = record()
    row = list(dispatch_journal._row(initial))
    row[0] = "55555555-5555-4555-8555-555555555555"
    with pytest.raises(JournalError):
        dispatch_journal._decode(tuple(row))

    class NonWalConnection:
        def execute(self, _query: str) -> NonWalConnection:
            return self

        def fetchone(self) -> tuple[str]:
            return ("delete",)

        def close(self) -> None:
            return None

    value = journal(tmp_path)
    monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: NonWalConnection())
    with pytest.raises(JournalError):
        value._connect()

    monkeypatch.setattr(
        dispatch_journal,
        "require_private_file",
        lambda _path: (_ for _ in ()).throw(JournalError()),
    )
    with pytest.raises(JournalError):
        value._connect()
    monkeypatch.undo()

    closed = False

    class PartiallyOpenedConnection:
        def execute(self, _query: str) -> object:
            raise sqlite3.OperationalError

        def close(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: PartiallyOpenedConnection())
    with pytest.raises(JournalError):
        value._connect()
    assert closed is True


def test_journal_wraps_invalid_counts_sqlite_errors_and_stale_row_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = journal(tmp_path)
    initial = record()

    class Cursor:
        def __init__(self, rows: list[tuple[object, ...]] | None = None, rowcount: int = 1) -> None:
            self._rows = [] if rows is None else rows
            self.rowcount = rowcount

        def fetchall(self) -> list[tuple[object, ...]]:
            return self._rows

        def fetchone(self) -> tuple[object, ...] | None:
            return self._rows[0] if self._rows else None

    class InvalidCountConnection:
        def execute(self, query: str, _params: object = None) -> Cursor:
            if "WHERE operation_id = ? OR idempotency_key = ?" in query:
                return Cursor([])
            if "SELECT COUNT(*)" in query:
                return Cursor([("not-an-integer",)])
            return Cursor()

        def close(self) -> None:
            return None

    monkeypatch.setattr(value, "_connect", InvalidCountConnection)
    with pytest.raises(JournalError):
        value.reserve(initial, capacity=2)

    class SqliteFailureConnection:
        def execute(self, query: str, _params: object = None) -> Cursor:
            if "WHERE operation_id = ? OR idempotency_key = ?" in query:
                raise sqlite3.OperationalError
            return Cursor()

        def close(self) -> None:
            return None

    monkeypatch.setattr(value, "_connect", SqliteFailureConnection)
    with pytest.raises(JournalError):
        value.reserve(initial, capacity=2)

    class StaleUpdateConnection:
        def execute(self, query: str, _params: object = None) -> Cursor:
            if query.lstrip().startswith("SELECT"):
                return Cursor([dispatch_journal._row(initial)])
            if "UPDATE dispatch_operations" in query:
                return Cursor(rowcount=0)
            return Cursor()

        def close(self) -> None:
            return None

    monkeypatch.setattr(value, "_connect", StaleUpdateConnection)
    with pytest.raises(DispatchJournalConflictError):
        value.replace(initial, failed(initial))

    class ReplaceSqliteFailureConnection:
        def execute(self, query: str, _params: object = None) -> Cursor:
            if query.lstrip().startswith("SELECT"):
                raise sqlite3.OperationalError
            return Cursor()

        def close(self) -> None:
            return None

    monkeypatch.setattr(value, "_connect", ReplaceSqliteFailureConnection)
    with pytest.raises(JournalError):
        value.replace(initial, failed(initial))


def test_journal_schema_and_connection_helpers_reject_all_remaining_malformed_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected_columns = [
        (0, "operation_id", "TEXT", 1, None, 1),
        (1, "idempotency_key", "TEXT", 1, None, 0),
        (2, "printer_uuid", "TEXT", 1, None, 0),
        (3, "state", "TEXT", 1, None, 0),
        (4, "record_json", "TEXT", 1, None, 0),
    ]

    class Schema:
        def __init__(
            self,
            columns: Sequence[tuple[object, ...]],
            indexes: Sequence[tuple[object, ...]],
            names: Sequence[tuple[object, ...]],
        ) -> None:
            self.columns = columns
            self.indexes = indexes
            self.names = names
            self.query = ""

        def execute(self, query: str, _params: object = None) -> Schema:
            self.query = query
            return self

        def fetchall(self) -> list[tuple[object, ...]]:
            if self.query.startswith("PRAGMA table_info"):
                return list(self.columns)
            if self.query.startswith("PRAGMA index_list"):
                return list(self.indexes)
            return list(self.names)

    wrong_column = [*expected_columns]
    wrong_column[1] = (1, "idempotency_key", "BLOB", 1, None, 0)
    bad_layout = [(0, "unique_operation", 0, "c", 0)]
    good_indexes = [
        (0, "unique_operation", 1, "c", 0),
        (1, "unique_key", 1, "c", 0),
    ]
    for schema in (
        Schema(wrong_column, [], []),
        Schema(expected_columns, bad_layout, []),
        Schema(expected_columns, good_indexes, [(1,)]),
        Schema(expected_columns, [], []),
    ):
        with pytest.raises(JournalError):
            dispatch_journal._validate_schema(schema)  # type: ignore[arg-type]

    value = journal(tmp_path)
    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError()),
    )
    with pytest.raises(JournalError):
        value._connect()
