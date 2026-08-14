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

from ..start_helpers import observation, preflight, verified_upload


def journal_path(tmp_path: Path) -> Path:
    return tmp_path / "start-journal.sqlite3"


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
    journal = StartJournal(path)
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

    restarted = StartJournal(path)
    restarted.initialize()
    assert restarted.lookup(pending.verified) == confirmed
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute("PRAGMA synchronous").fetchone() == (2,)


@pytest.mark.parametrize("collision", ["operation", "key", "two_rows"])
def test_journal_rejects_every_operation_or_key_collision(tmp_path: Path, collision: str) -> None:
    journal = StartJournal(journal_path(tmp_path))
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
    journal = StartJournal(journal_path(tmp_path))
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
    journal = StartJournal(journal_path(tmp_path))
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
    journal = StartJournal(journal_path(tmp_path))
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
        StartJournal(public).initialize()
    with pytest.raises(JournalError):
        StartJournal(public)._connect()

    directory = tmp_path / "directory.sqlite3"
    directory.mkdir()
    with pytest.raises(JournalError):
        StartJournal(directory).initialize()

    target = tmp_path / "target.sqlite3"
    target.touch(mode=0o600)
    link = tmp_path / "link.sqlite3"
    link.symlink_to(target)
    with pytest.raises(JournalError):
        StartJournal(link).initialize()

    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(JournalError):
        StartJournal(linked_parent / "journal.sqlite3").initialize()

    public_parent = tmp_path / "public-parent"
    public_parent.mkdir()
    public_parent.chmod(0o755)
    with pytest.raises(JournalError):
        StartJournal(public_parent / "journal.sqlite3").initialize()


def test_journal_requires_an_existing_parent_and_supported_schema(tmp_path: Path) -> None:
    with pytest.raises(JournalError):
        StartJournal(tmp_path / "missing" / "journal.sqlite3").initialize()

    future = tmp_path / "future.sqlite3"
    with closing(sqlite3.connect(future)) as connection, connection:
        connection.execute("PRAGMA user_version = 2")
    future.chmod(0o600)
    with pytest.raises(JournalError):
        StartJournal(future).initialize()

    wrong = tmp_path / "wrong.sqlite3"
    with closing(sqlite3.connect(wrong)) as connection, connection:
        connection.execute("CREATE TABLE start_operations (wrong TEXT) STRICT")
        connection.execute("PRAGMA user_version = 1")
    wrong.chmod(0o600)
    with pytest.raises(JournalError):
        StartJournal(wrong).initialize()


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
        StartJournal(path).initialize()


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

    store = StartJournal(journal_path(tmp_path))
    monkeypatch.setattr(store, "_connect", Connection)
    with pytest.raises(JournalError):
        store.initialize()

    assert "ROLLBACK" in calls
    assert calls[-1] == "CLOSE"


def test_reserve_rolls_back_decoder_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StartJournal(journal_path(tmp_path))
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
        StartJournal(path)._connect()
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

    store = StartJournal(journal_path(tmp_path))
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
        _validate_schema(Connection())  # type: ignore[arg-type]


@pytest.mark.parametrize("corruption", ["json", "state"])
def test_journal_rejects_corrupt_or_contradictory_rows(tmp_path: Path, corruption: str) -> None:
    path = journal_path(tmp_path)
    journal = StartJournal(path)
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
