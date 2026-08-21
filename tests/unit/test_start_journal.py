from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import klove.persistence.start_journal as journal_module
from klove.domain.start import (
    StartBoundary,
    StartConfirmation,
    StartFailure,
    StartFailureCode,
    StartJournalRecord,
    StartJournalState,
)
from klove.errors import JournalError
from klove.persistence.start_journal import (
    JournalConflictError,
    JournalFenceError,
    StartJournal,
    _decode_row,
    _pragma_integer,
    _validate_schema,
)
from klove.persistence.store_identity import StoreIdentity, canonical_uuid4

from ..start_helpers import observation, preflight, verified_upload

INSTALLATION_ID = "99999999-9999-4999-8999-999999999999"


def journal_path(tmp_path: Path) -> Path:
    return tmp_path / "start-journal.sqlite3"


def make_journal(path: Path) -> StartJournal:
    return StartJournal(path, INSTALLATION_ID)


def record(**updates: object) -> StartJournalRecord:
    verified = verified_upload()
    values: dict[str, object] = {
        "operation_id": verified.qualification.operation_id,
        "idempotency_key": verified.qualification.idempotency_key,
        "printer_uuid": verified.qualification.target.printer_uuid,
        "verified": verified,
        "preflight": preflight(),
        "state": StartJournalState.DISPATCHING,
    }
    values.update(updates)
    return StartJournalRecord.model_validate(values)


def failure() -> StartFailure:
    return StartFailure(
        boundary=StartBoundary.TRANSPORT,
        code=StartFailureCode.TRANSPORT_AMBIGUOUS,
    )


def test_journal_reservation_and_terminal_evidence_survive_restart(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    journal = make_journal(path)
    journal.initialize()
    assert path.stat().st_mode & 0o777 == 0o600

    pending = record()
    reserved, created = journal.reserve(pending)
    duplicate, duplicate_created = journal.reserve(pending)
    assert created is True
    assert duplicate_created is False
    assert reserved == duplicate == pending
    assert journal.lookup(pending.verified) == pending
    assert journal.unresolved(pending.printer_uuid) == (pending,)

    unknown = journal.mark_unknown(pending, failure())
    assert unknown.state is StartJournalState.OUTCOME_UNKNOWN
    confirmed_evidence = observation().latest_job
    assert confirmed_evidence is not None
    confirmed = journal.mark_confirmed(
        unknown,
        StartConfirmation(
            path=pending.verified.path,
            eventtime=11,
            phase=observation().phase,
            file_position=100,
            job=confirmed_evidence,
        ),
    )
    assert confirmed.state is StartJournalState.CONFIRMED
    assert journal.unresolved(pending.printer_uuid) == ()

    restarted = make_journal(path)
    restarted.initialize()
    assert restarted.lookup(pending.verified) == confirmed
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute("PRAGMA synchronous").fetchone() == (2,)


def test_identity_and_exact_operation_state_lookup(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    store = make_journal(path)
    with pytest.raises(JournalError):
        _ = store.identity
    store.initialize()
    identity = store.identity
    assert identity.installation_id == INSTALLATION_ID
    assert identity.store_id != identity.installation_id
    assert identity.schema_version == 2
    pending = record()
    assert store.lookup_operation_state(pending.operation_id) is None
    store.reserve(pending)
    assert store.lookup_operation(pending.operation_id) == pending
    assert store.lookup_operation_state(pending.operation_id) is StartJournalState.DISPATCHING
    assert store.lookup_operation_state("55555555-5555-4555-8555-555555555555") is None
    with pytest.raises(JournalError):
        store.lookup_operation_state("not-an-operation")
    restarted = StartJournal(path, INSTALLATION_ID)
    restarted.initialize()
    assert restarted.identity == identity


def test_v1_migration_adds_metadata_without_changing_operation_rows(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    pending = record()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE start_operations (
                operation_id TEXT PRIMARY KEY NOT NULL,
                idempotency_key TEXT UNIQUE NOT NULL,
                printer_uuid TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('dispatching', 'confirmed', 'outcome_unknown')
                ),
                record_json TEXT NOT NULL
            ) STRICT
            """
        )
        connection.execute(
            """
            INSERT INTO start_operations (
                operation_id, idempotency_key, printer_uuid, state, record_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                pending.operation_id,
                pending.idempotency_key,
                pending.printer_uuid,
                pending.state.value,
                pending.model_dump_json(),
            ),
        )
        connection.execute("PRAGMA user_version = 1")
    path.chmod(0o600)

    store = make_journal(path)
    store.initialize()
    assert store.lookup_operation_state(pending.operation_id) is StartJournalState.DISPATCHING
    assert store.identity.installation_id == INSTALLATION_ID
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM start_operations").fetchone() == (1,)


def test_identity_metadata_mismatch_and_extra_rows_fail_closed(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    store = make_journal(path)
    store.initialize()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TRIGGER start_metadata_no_update")
        connection.execute(
            "UPDATE start_metadata SET value = ? WHERE key = 'installation_id'",
            ("88888888-8888-4888-8888-888888888888",),
        )
    with pytest.raises(JournalError):
        store.initialize()

    clean = make_journal(tmp_path / "extra.sqlite3")
    clean.initialize()
    with closing(sqlite3.connect(clean._path)) as connection, connection:
        connection.execute("INSERT INTO start_metadata (key, value) VALUES ('future', 'x')")
    with pytest.raises(JournalError):
        clean.initialize()


@pytest.mark.parametrize(
    "value",
    [
        None,
        "not-a-uuid",
        "99999999-9999-4999-7999-999999999999",
        "99999999-9999-4999-8999-99999999999A",
    ],
)
def test_store_identity_rejects_noncanonical_uuid4_values(value: object) -> None:
    with pytest.raises(JournalError):
        canonical_uuid4(value)  # type: ignore[arg-type]
    with pytest.raises(JournalError):
        StoreIdentity(value, "88888888-8888-4888-8888-888888888888", 2)  # type: ignore[arg-type]
    with pytest.raises(JournalError):
        StoreIdentity(
            "99999999-9999-4999-8999-999999999999",
            "88888888-8888-4888-8888-888888888888",
            value,  # type: ignore[arg-type]
        )


def test_start_schema_validation_rejects_each_metadata_structure_weakness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(journal_module, "_validate_operation_schema", lambda _connection: None)

    class Schema:
        def __init__(self, mode: str) -> None:
            self.mode = mode
            self.query = ""

        def execute(self, query: str, _params: object = None) -> Schema:
            self.query = query
            return self

        def fetchall(self) -> list[tuple[object, ...]]:  # noqa: PLR0911
            if "type = 'table'" in self.query:
                if self.mode == "wrong_tables":
                    return []
                return [("start_metadata",), ("start_operations",)]
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
                    ("start_metadata_no_delete",),
                    ("start_metadata_no_update",),
                ]
            return []

    for mode in (
        "wrong_tables",
        "short_columns",
        "bad_columns",
        "bad_index",
        "bad_index_columns",
        "bad_triggers",
    ):
        with pytest.raises(JournalError):
            _validate_schema(Schema(mode))  # type: ignore[arg-type]
    with pytest.raises(JournalError):
        journal_module._validate_v1_schema(Schema("wrong_tables"))  # type: ignore[arg-type]


def test_start_identity_and_lookup_helpers_reject_corrupt_reads(
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
            journal_module._read_store_id(Rows(rows), INSTALLATION_ID)  # type: ignore[arg-type]

    class FailedConnection:
        def execute(self, _query: str, _params: object = None) -> object:
            raise sqlite3.OperationalError

        def close(self) -> None:
            return None

    store = make_journal(journal_path(tmp_path))
    def failed_connect() -> FailedConnection:
        return FailedConnection()

    monkeypatch.setattr(store, "_connect", failed_connect)
    with pytest.raises(JournalError):
        store.lookup_operation("99999999-9999-4999-8999-999999999999")


@pytest.mark.parametrize("collision", ["operation", "key", "two_rows"])
def test_journal_rejects_every_operation_or_key_collision(tmp_path: Path, collision: str) -> None:
    journal = make_journal(journal_path(tmp_path))
    journal.initialize()
    first = record()
    journal.reserve(first)
    if collision == "operation":
        conflicting = verified_upload(idempotency_key="55555555-5555-4555-8555-555555555555")
    elif collision == "key":
        conflicting = verified_upload(operation_id="66666666-6666-4666-8666-666666666666")
    else:
        live = observation()
        assert live.latest_job is not None
        journal.mark_confirmed(
            first,
            StartConfirmation(
                path=first.verified.path,
                eventtime=live.eventtime,
                phase=live.phase,
                file_position=live.file_position,
                job=live.latest_job,
            ),
        )
        second = verified_upload(
            operation_id="66666666-6666-4666-8666-666666666666",
            idempotency_key="55555555-5555-4555-8555-555555555555",
        )
        journal.reserve(
            record(
                operation_id=second.qualification.operation_id,
                idempotency_key=second.qualification.idempotency_key,
                verified=second,
            )
        )
        conflicting = verified_upload(idempotency_key="55555555-5555-4555-8555-555555555555")

    with pytest.raises(JournalConflictError):
        journal.lookup(conflicting)
    with pytest.raises(JournalConflictError):
        journal.reserve(
            record(
                operation_id=conflicting.qualification.operation_id,
                idempotency_key=conflicting.qualification.idempotency_key,
                verified=conflicting,
            )
        )


def test_reserve_requires_dispatching_and_terminal_transition_is_single(tmp_path: Path) -> None:
    journal = make_journal(journal_path(tmp_path))
    journal.initialize()
    pending = record()
    with pytest.raises(JournalError):
        journal.reserve(
            pending.model_copy(
                update={"state": StartJournalState.OUTCOME_UNKNOWN, "failure": failure()}
            )
        )
    journal.reserve(pending)
    changed = journal.mark_unknown(pending, failure())
    assert journal.mark_unknown(changed, failure()) == changed
    live = observation()
    assert live.latest_job is not None
    confirmation = StartConfirmation(
        path=pending.verified.path,
        eventtime=live.eventtime,
        phase=live.phase,
        file_position=live.file_position,
        job=live.latest_job,
    )
    confirmed = journal.mark_confirmed(changed, confirmation)
    with pytest.raises(JournalError):
        journal.mark_confirmed(confirmed, confirmation)


def test_reserve_atomically_rejects_another_unresolved_operation(
    tmp_path: Path,
) -> None:
    journal = make_journal(journal_path(tmp_path))
    journal.initialize()
    journal.reserve(record())
    other = verified_upload(
        operation_id="55555555-5555-4555-8555-555555555555",
        idempotency_key="66666666-6666-4666-8666-666666666666",
    )

    with pytest.raises(JournalFenceError):
        journal.reserve(
            record(
                operation_id=other.qualification.operation_id,
                idempotency_key=other.qualification.idempotency_key,
                verified=other,
            )
        )

    other_printer_uuid = "77777777-7777-4777-8777-777777777777"
    target = other.qualification.target.model_copy(update={"printer_uuid": other_printer_uuid})
    qualification = other.qualification.model_copy(update={"target": target})
    other_printer = other.model_copy(update={"qualification": qualification})
    reserved, created = journal.reserve(
        record(
            operation_id=other_printer.qualification.operation_id,
            idempotency_key=other_printer.qualification.idempotency_key,
            printer_uuid=other_printer_uuid,
            verified=other_printer,
        )
    )
    assert created is True
    assert reserved.printer_uuid == other_printer_uuid


def test_lookup_absence_and_printer_filter_are_exact(tmp_path: Path) -> None:
    journal = make_journal(journal_path(tmp_path))
    journal.initialize()
    assert journal.lookup(verified_upload()) is None
    journal.reserve(record())
    assert journal.unresolved("88888888-8888-4888-8888-888888888888") == ()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission and symlink contract")
def test_journal_rejects_public_files_symlinks_and_unsafe_parents(tmp_path: Path) -> None:
    public = tmp_path / "public.sqlite3"
    public.touch()
    public.chmod(0o644)
    with pytest.raises(JournalError):
        make_journal(public).initialize()
    with pytest.raises(JournalError):
        make_journal(public)._connect()

    directory = tmp_path / "directory.sqlite3"
    directory.mkdir()
    with pytest.raises(JournalError):
        make_journal(directory).initialize()

    target = tmp_path / "target.sqlite3"
    target.touch(mode=0o600)
    link = tmp_path / "link.sqlite3"
    link.symlink_to(target)
    with pytest.raises(JournalError):
        make_journal(link).initialize()

    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(JournalError):
        make_journal(linked_parent / "journal.sqlite3").initialize()

    public_parent = tmp_path / "public-parent"
    public_parent.mkdir()
    public_parent.chmod(0o755)
    with pytest.raises(JournalError):
        make_journal(public_parent / "journal.sqlite3").initialize()


def test_journal_requires_an_existing_parent_and_supported_schema(tmp_path: Path) -> None:
    with pytest.raises(JournalError):
        make_journal(tmp_path / "missing" / "journal.sqlite3").initialize()

    future = tmp_path / "future.sqlite3"
    with closing(sqlite3.connect(future)) as connection, connection:
        connection.execute("PRAGMA user_version = 3")
    future.chmod(0o600)
    with pytest.raises(JournalError):
        make_journal(future).initialize()

    wrong = tmp_path / "wrong.sqlite3"
    with closing(sqlite3.connect(wrong)) as connection, connection:
        connection.execute("CREATE TABLE start_operations (wrong TEXT) STRICT")
        connection.execute("PRAGMA user_version = 1")
    wrong.chmod(0o600)
    with pytest.raises(JournalError):
        make_journal(wrong).initialize()


@pytest.mark.parametrize(
    "weakness",
    ["not_strict", "missing_unique", "extra_index", "expression_index"],
)
def test_journal_rejects_structurally_weakened_schema(
    tmp_path: Path,
    weakness: str,
) -> None:
    path = tmp_path / f"{weakness}.sqlite3"
    unique = "" if weakness == "missing_unique" else " UNIQUE"
    strict = "" if weakness == "not_strict" else " STRICT"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            f"""
            CREATE TABLE start_operations (
                operation_id TEXT PRIMARY KEY NOT NULL,
                idempotency_key TEXT{unique} NOT NULL,
                printer_uuid TEXT NOT NULL,
                state TEXT NOT NULL,
                record_json TEXT NOT NULL
            ){strict}
            """
        )
        if weakness == "extra_index":
            connection.execute("CREATE INDEX extra_printer_index ON start_operations(printer_uuid)")
        elif weakness == "expression_index":
            connection.execute(
                "CREATE UNIQUE INDEX expression_index ON start_operations(lower(printer_uuid))"
            )
        connection.execute("PRAGMA user_version = 1")
    path.chmod(0o600)

    with pytest.raises(JournalError):
        make_journal(path).initialize()


def test_initialize_rolls_back_schema_creation_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Cursor:
        def fetchone(self) -> tuple[int]:
            return (0,)

    class Connection:
        def execute(self, query: str) -> Cursor:
            calls.append(query.strip().split(maxsplit=1)[0])
            if "CREATE TABLE" in query:
                raise sqlite3.OperationalError
            return Cursor()

        def close(self) -> None:
            calls.append("CLOSE")

    store = make_journal(journal_path(tmp_path))
    monkeypatch.setattr(store, "_connect", Connection)
    with pytest.raises(JournalError):
        store.initialize()

    assert "ROLLBACK" in calls
    assert calls[-1] == "CLOSE"


def test_reserve_rolls_back_decoder_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_journal(journal_path(tmp_path))
    store.initialize()

    def broken_match(_rows: object, _verified: object) -> None:
        raise ValueError

    monkeypatch.setattr(journal_module, "_matching_record", broken_match)
    with pytest.raises(JournalError):
        store.reserve(record())


@pytest.mark.parametrize("failure", ["journal_mode", "pragma", "connect"])
def test_connect_closes_failed_connections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    path = journal_path(tmp_path)
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
    with pytest.raises(JournalError):
        make_journal(path)._connect()
    assert closed is (failure != "connect")


def test_terminal_write_rolls_back_commit_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Cursor:
        rowcount = 1

    class Connection:
        def execute(self, query: str, _params: object = None) -> Cursor:
            operation = query.strip().split(maxsplit=1)[0]
            calls.append(operation)
            if operation == "COMMIT":
                raise sqlite3.OperationalError
            return Cursor()

        def close(self) -> None:
            calls.append("CLOSE")

    store = make_journal(journal_path(tmp_path))
    monkeypatch.setattr(store, "_connect", Connection)
    with pytest.raises(JournalError):
        store._replace(record())
    assert calls[-2:] == ["ROLLBACK", "CLOSE"]


def test_schema_decoder_rejects_malformed_pragma_rows() -> None:
    class Cursor:
        def __init__(self, rows: list[tuple[object, ...]]) -> None:
            self._rows = rows

        def fetchall(self) -> list[tuple[object, ...]]:
            return self._rows

    class Connection:
        def execute(self, _query: str, _params: object = None) -> Cursor:
            return Cursor([("short",)])

    with pytest.raises(JournalError):
        journal_module._validate_operation_schema(Connection())  # type: ignore[arg-type]


@pytest.mark.parametrize("corruption", ["json", "state"])
def test_journal_rejects_corrupt_or_contradictory_rows(tmp_path: Path, corruption: str) -> None:
    path = journal_path(tmp_path)
    journal = make_journal(path)
    journal.initialize()
    pending = record()
    journal.reserve(pending)
    with closing(sqlite3.connect(path)) as connection, connection:
        if corruption == "json":
            connection.execute("UPDATE start_operations SET record_json = ?", ("not-json",))
        else:
            connection.execute("UPDATE start_operations SET state = 'outcome_unknown'")
    with pytest.raises(JournalError):
        journal.lookup(pending.verified)
    with pytest.raises(JournalError):
        journal.unresolved(pending.printer_uuid)


def test_low_level_row_and_pragma_decoders_are_strict() -> None:
    for row in ((), (1, 2, 3, 4, 5)):
        with pytest.raises(JournalError):
            _decode_row(row)

    class Cursor:
        def __init__(self, row: tuple[object, ...] | None) -> None:
            self.row = row

        def fetchone(self) -> tuple[object, ...] | None:
            return self.row

    class Connection:
        def __init__(self, row: tuple[object, ...] | None) -> None:
            self.row = row

        def execute(self, _query: str) -> Cursor:
            return Cursor(self.row)

    for value in (None, (), (True,), ("1",), (1, 2)):
        with pytest.raises(JournalError):
            _pragma_integer(Connection(value), "user_version")  # type: ignore[arg-type]
