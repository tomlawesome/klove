from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import klove.persistence.control_journal as journal_module
from klove.domain.control import ControlOperation
from klove.domain.control_journal import (
    ControlJournalEvidence,
    ControlJournalFailure,
    ControlJournalFailureCode,
    ControlJournalRecord,
    ControlJournalState,
)
from klove.domain.models import JobHistoryStatus, PrinterPhase
from klove.errors import JournalError
from klove.persistence.control_journal import (
    ControlJournal,
    ControlJournalCapacityError,
    ControlJournalConflictError,
    ControlJournalFenceError,
    ControlJournalTransitionError,
    _decode,
    _one,
    _pragma_integer,
    _read_identity,
    _validate_indexes,
    _validate_schema,
    _validate_table,
    _validate_triggers,
)

INSTALLATION = "11111111-1111-4111-8111-111111111111"
PRINTER = "22222222-2222-4222-8222-222222222222"
OTHER_PRINTER = "33333333-3333-4333-8333-333333333333"
OPERATION = "44444444-4444-4444-8444-444444444444"
KEY = "55555555-5555-4555-8555-555555555555"
OTHER_OPERATION = "66666666-6666-4666-8666-666666666666"
OTHER_KEY = "77777777-7777-4777-8777-777777777777"
TOKEN = "a" * 64
OTHER_TOKEN = "b" * 64


def evidence(**updates: object) -> ControlJournalEvidence:
    values: dict[str, object] = {
        "eventtime": 10.0,
        "phase": PrinterPhase.PRINTING,
        "job_id": "ABCDEF12",
        "job_start_time": 9.0,
        "filename": "cube.gcode",
        "file_position": 100,
        "job_status": JobHistoryStatus.IN_PROGRESS,
    }
    values.update(updates)
    return ControlJournalEvidence.model_validate(values)


def record(**updates: object) -> ControlJournalRecord:
    values: dict[str, object] = {
        "operation_id": OPERATION,
        "idempotency_key": KEY,
        "printer_uuid": PRINTER,
        "operation": ControlOperation.PAUSE,
        "state_token": TOKEN,
        "preflight": evidence(),
        "state": ControlJournalState.DISPATCHING,
    }
    values.update(updates)
    return ControlJournalRecord.model_validate(values)


def failure(
    code: ControlJournalFailureCode = ControlJournalFailureCode.TRANSPORT_AMBIGUOUS,
) -> ControlJournalFailure:
    return ControlJournalFailure(code=code)


def journal(path: Path, *, capacity: int = 8) -> ControlJournal:
    store = ControlJournal(path, INSTALLATION, capacity=capacity)
    store.initialize()
    return store


def test_identity_reservation_lookup_and_restart_are_durable(tmp_path: Path) -> None:
    path = tmp_path / "control.sqlite3"
    store = journal(path)
    pending = record()

    reserved, created = store.reserve(pending)
    duplicate, duplicate_created = store.reserve(pending)
    assert created is True
    assert duplicate_created is False
    assert reserved == duplicate == pending
    assert store.lookup(OPERATION, KEY) == pending
    assert store.unresolved(PRINTER) == (pending,)
    assert store.identity.installation_id == INSTALLATION
    assert store.identity.store_id != INSTALLATION
    assert store.identity.schema_version == 1

    restarted = ControlJournal(path, INSTALLATION)
    restarted.initialize()
    assert restarted.identity == store.identity
    assert restarted.lookup(OPERATION, KEY) == pending
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute("PRAGMA synchronous").fetchone() == (2,)


def test_uninitialized_identity_and_lookup_absence_are_rejected_or_empty(tmp_path: Path) -> None:
    store = ControlJournal(tmp_path / "uninitialized.sqlite3", INSTALLATION)
    with pytest.raises(JournalError):
        _ = store.identity
    store.initialize()
    assert store.lookup(OPERATION, KEY) is None


def test_uninitialized_reserve_is_rejected(tmp_path: Path) -> None:
    store = ControlJournal(tmp_path / "uninitialized-reserve.sqlite3", INSTALLATION)
    with pytest.raises(JournalError):
        store.reserve(record())


def test_reserve_requires_dispatching_and_accepts_enum_failure(tmp_path: Path) -> None:
    store = journal(tmp_path / "reserve-contract.sqlite3")
    pending = record()
    denied = record(
        state=ControlJournalState.DENIED_BEFORE_DISPATCH,
        failure=failure(),
    )
    with pytest.raises(JournalError):
        store.reserve(denied)
    store.reserve(pending)
    unknown = store.mark_outcome_unknown(
        pending,
        ControlJournalFailureCode.TRANSPORT_AMBIGUOUS,
    )
    assert unknown.failure == failure()


@pytest.mark.parametrize("method", ["mark_outcome_unknown", "mark_denied_before_dispatch"])
def test_terminal_failure_transitions_are_durable_and_unresolved_only_until_terminal(
    tmp_path: Path, method: str
) -> None:
    store = journal(tmp_path / f"{method}.sqlite3")
    pending = record()
    store.reserve(pending)
    changed = getattr(store, method)(pending, failure())
    expected_state = (
        ControlJournalState.OUTCOME_UNKNOWN
        if method == "mark_outcome_unknown"
        else ControlJournalState.DENIED_BEFORE_DISPATCH
    )
    assert changed.state is expected_state
    assert store.unresolved(PRINTER) == (() if changed.is_terminal else (changed,))
    assert store.lookup(OPERATION, KEY) == changed


def test_confirmation_and_evidence_supersession_close_exact_fences(tmp_path: Path) -> None:
    confirmed_store = journal(tmp_path / "confirmed.sqlite3")
    pending = record()
    confirmed_store.reserve(pending)
    confirmed = confirmed_store.mark_confirmed(
        pending,
        evidence(
            state_token=OTHER_TOKEN,
            eventtime=11,
            phase=PrinterPhase.PAUSED,
            file_position=120,
        ),
    )
    assert confirmed.state is ControlJournalState.CONFIRMED
    assert confirmed_store.unresolved(PRINTER) == ()

    superseded_store = journal(tmp_path / "superseded.sqlite3")
    unknown = record(state=ControlJournalState.DISPATCHING)
    superseded_store.reserve(unknown)
    unknown = superseded_store.mark_outcome_unknown(unknown, failure())
    superseded = superseded_store.mark_evidence_superseded(
        unknown,
        evidence(state_token=OTHER_TOKEN, eventtime=12, phase=PrinterPhase.IDLE, file_position=0),
    )
    assert superseded.state is ControlJournalState.EVIDENCE_SUPERSEDED
    assert superseded_store.unresolved(PRINTER) == ()


def test_reserve_rejects_collisions_fences_and_capacity_overflow(tmp_path: Path) -> None:
    store = journal(tmp_path / "collisions.sqlite3", capacity=2)
    pending = record()
    store.reserve(pending)
    with pytest.raises(ControlJournalConflictError):
        store.reserve(record(state_token=OTHER_TOKEN))
    with pytest.raises(ControlJournalConflictError):
        store.lookup(OPERATION, OTHER_KEY)
    with pytest.raises(ControlJournalFenceError):
        store.reserve(record(operation_id=OTHER_OPERATION, idempotency_key=OTHER_KEY))

    terminal = store.mark_denied_before_dispatch(
        pending,
        failure(ControlJournalFailureCode.PRECHECK_DENIED),
    )
    assert terminal.state is ControlJournalState.DENIED_BEFORE_DISPATCH
    capacity_store = journal(tmp_path / "capacity.sqlite3", capacity=1)
    capacity_store.reserve(pending)
    capacity_store.mark_denied_before_dispatch(pending, failure())
    with pytest.raises(ControlJournalCapacityError):
        capacity_store.reserve(record(operation_id=OTHER_OPERATION, idempotency_key=OTHER_KEY))


def test_transition_is_exact_and_stale_or_terminal_changes_are_rejected(tmp_path: Path) -> None:
    store = journal(tmp_path / "stale.sqlite3")
    pending = record()
    store.reserve(pending)
    unknown = store.mark_outcome_unknown(pending, failure())
    with pytest.raises(ControlJournalTransitionError):
        store.mark_outcome_unknown(pending, failure())
    with pytest.raises(ControlJournalTransitionError):
        store.mark_denied_before_dispatch(unknown, failure())
    with pytest.raises(ControlJournalTransitionError):
        store.transition(
            unknown,
            unknown.model_copy(update={"state": ControlJournalState.CONFIRMED}),
        )
    with pytest.raises(ControlJournalTransitionError):
        store.transition(
            pending,
            record(
                operation=ControlOperation.RESUME,
                state=ControlJournalState.CONFIRMED,
                terminal_evidence=evidence(state_token=OTHER_TOKEN),
            ),
        )


@pytest.mark.parametrize(
    "bad",
    [
        {"filename": ""},
        {"filename": "bad\x00name"},
        {"file_position": -1},
        {"job_id": "bad"},
        {"eventtime": float("nan")},
    ],
)
def test_evidence_is_bounded_and_strict(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        evidence(**bad)


def test_record_state_payload_contract_is_strict() -> None:
    with pytest.raises(ValueError):
        record(state=ControlJournalState.OUTCOME_UNKNOWN)
    with pytest.raises(ValueError):
        record(state=ControlJournalState.CONFIRMED)
    with pytest.raises(ValueError):
        record(
            state=ControlJournalState.CONFIRMED,
            failure=failure(),
            terminal_evidence=evidence(),
        )
    assert record(
        state=ControlJournalState.OUTCOME_UNKNOWN,
        failure=failure(),
    ).is_unresolved


def test_domain_validator_defensively_rejects_constructed_invalid_states() -> None:
    base = record()
    dispatching_values = base.model_dump()
    dispatching_values["failure"] = failure()
    invalid_dispatching = ControlJournalRecord.model_construct(**dispatching_values)
    with pytest.raises(ValueError):
        invalid_dispatching.validate_terminal_payload()
    denied_values = base.model_dump()
    denied_values["state"] = ControlJournalState.DENIED_BEFORE_DISPATCH
    invalid_denied = ControlJournalRecord.model_construct(**denied_values)
    with pytest.raises(ValueError):
        invalid_denied.validate_terminal_payload()
    future_values = base.model_dump()
    future_values["state"] = "future"
    invalid_state = ControlJournalRecord.model_construct(**future_values)
    with pytest.raises(ValueError):
        invalid_state.validate_terminal_payload()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission and symlink contract")
def test_private_path_and_parent_contract(tmp_path: Path) -> None:
    public = tmp_path / "public.sqlite3"
    public.touch(mode=0o600)
    public.chmod(0o644)
    with pytest.raises(JournalError):
        ControlJournal(public, INSTALLATION).initialize()

    parent = tmp_path / "public-parent"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    with pytest.raises(JournalError):
        ControlJournal(parent / "control.sqlite3", INSTALLATION).initialize()

    target = tmp_path / "target.sqlite3"
    target.touch(mode=0o600)
    link = tmp_path / "link.sqlite3"
    link.symlink_to(target)
    with pytest.raises(JournalError):
        ControlJournal(link, INSTALLATION).initialize()


def test_initialization_rejects_invalid_identity_schema_and_metadata(tmp_path: Path) -> None:
    with pytest.raises(JournalError):
        ControlJournal(tmp_path / "invalid-capacity.sqlite3", INSTALLATION, capacity=0)
    with pytest.raises(JournalError):
        ControlJournal(tmp_path / "invalid-installation.sqlite3", "not-a-uuid").initialize()

    future = tmp_path / "future.sqlite3"
    with closing(sqlite3.connect(future)) as connection, connection:
        connection.execute("PRAGMA user_version = 2")
    future.chmod(0o600)
    with pytest.raises(JournalError):
        ControlJournal(future, INSTALLATION).initialize()

    wrong = tmp_path / "wrong.sqlite3"
    with closing(sqlite3.connect(wrong)) as connection, connection:
        connection.execute("CREATE TABLE wrong (value TEXT) STRICT")
        connection.execute("PRAGMA user_version = 1")
    wrong.chmod(0o600)
    with pytest.raises(JournalError):
        ControlJournal(wrong, INSTALLATION).initialize()

    valid = tmp_path / "metadata.sqlite3"
    journal(valid)
    with (
        closing(sqlite3.connect(valid)) as connection,
        connection,
        pytest.raises(sqlite3.IntegrityError),
    ):
        connection.execute(
            "UPDATE control_metadata SET value = ? WHERE key = 'installation_id'",
            (OTHER_PRINTER,),
        )
    ControlJournal(valid, INSTALLATION).initialize()


def test_corrupt_row_and_unsafe_schema_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.sqlite3"
    store = journal(path)
    store.reserve(record())
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE control_operations SET record_json = ? WHERE operation_id = ?",
            ("{}", OPERATION),
        )
    reopened = ControlJournal(path, INSTALLATION)
    reopened.initialize()
    with pytest.raises(JournalError):
        reopened.lookup(OPERATION, KEY)
    with pytest.raises(JournalError):
        reopened.unresolved(PRINTER)

    weak = tmp_path / "weak.sqlite3"
    with closing(sqlite3.connect(weak)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE control_metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)
            """
        )
        connection.execute(
            """
            CREATE TABLE control_operations (
                operation_id TEXT PRIMARY KEY NOT NULL,
                idempotency_key TEXT UNIQUE NOT NULL,
                printer_uuid TEXT NOT NULL,
                operation TEXT NOT NULL,
                state_token TEXT NOT NULL,
                state TEXT NOT NULL,
                record_json TEXT NOT NULL
            )
            """
        )
        connection.execute("PRAGMA user_version = 1")
    weak.chmod(0o600)
    with pytest.raises(JournalError):
        ControlJournal(weak, INSTALLATION).initialize()


def test_connection_and_uuid_factory_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "factory.sqlite3"
    with pytest.raises(JournalError):
        ControlJournal(path, INSTALLATION, uuid_factory=lambda: "not-a-uuid").initialize()  # type: ignore[arg-type]

    missing = ControlJournal(tmp_path / "missing.sqlite3", INSTALLATION)
    with pytest.raises(JournalError):
        missing.lookup(OPERATION, KEY)
    with pytest.raises(JournalError):
        missing.unresolved(PRINTER)


class _Cursor:
    def __init__(
        self,
        *,
        rows: list[tuple[object, ...]] | None = None,
        row: tuple[object, ...] | None = None,
        rowcount: int = 1,
    ) -> None:
        self.rows = [] if rows is None else rows
        self.row = row
        self.rowcount = rowcount

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows

    def fetchone(self) -> tuple[object, ...] | None:
        return self.row


class _Connection:
    def __init__(self, handler: object) -> None:
        self.handler = handler
        self.closed = False

    def execute(self, query: str, *_args: object) -> _Cursor:
        return self.handler(query)  # type: ignore[operator]

    def close(self) -> None:
        self.closed = True


def test_fault_paths_cover_transactions_and_connection_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rollback_path = tmp_path / "rollback.sqlite3"
    with closing(sqlite3.connect(rollback_path)) as connection, connection:
        connection.execute("CREATE TABLE control_metadata (key TEXT)")
    rollback_path.chmod(0o600)
    with pytest.raises(JournalError):
        ControlJournal(rollback_path, INSTALLATION).initialize()

    mode_path = tmp_path / "mode.sqlite3"
    mode_path.touch(mode=0o600)
    mode_connection = _Connection(
        lambda query: _Cursor(row=("delete",))
        if "journal_mode" in query
        else _Cursor()
    )
    monkeypatch.setattr(
        journal_module.sqlite3,
        "connect",
        lambda *_args, **_kwargs: mode_connection,
    )
    with pytest.raises(JournalError):
        ControlJournal(mode_path, INSTALLATION)._connect()
    assert mode_connection.closed

    error_path = tmp_path / "connect-error.sqlite3"
    error_path.touch(mode=0o600)
    error_connection = _Connection(
        lambda query: (_ for _ in ()).throw(sqlite3.OperationalError())
        if "busy_timeout" in query
        else _Cursor()
    )
    monkeypatch.setattr(
        journal_module.sqlite3,
        "connect",
        lambda *_args, **_kwargs: error_connection,
    )
    with pytest.raises(JournalError):
        ControlJournal(error_path, INSTALLATION)._connect()
    assert error_connection.closed

    monkeypatch.setattr(
        journal_module,
        "require_private_file",
        lambda _path: (_ for _ in ()).throw(JournalError()),
    )
    with pytest.raises(JournalError):
        ControlJournal(error_path, INSTALLATION)._connect()


def test_fault_paths_cover_atomic_reserve_and_transition_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = journal(tmp_path / "faults.sqlite3")
    pending = record()

    count_connection = _Connection(
        lambda query: _Cursor(row=(None,))
        if "COUNT(*)" in query
        else _Cursor(rows=[])
    )
    monkeypatch.setattr(store, "_connect", lambda: count_connection)
    with pytest.raises(JournalError):
        store.reserve(pending)
    assert count_connection.closed

    sql_connection = _Connection(
        lambda query: (_ for _ in ()).throw(sqlite3.OperationalError())
        if query.lstrip().startswith("SELECT operation_id")
        else _Cursor()
    )
    monkeypatch.setattr(store, "_connect", lambda: sql_connection)
    with pytest.raises(JournalError):
        store.reserve(pending)
    assert sql_connection.closed

    expected = record()
    store = journal(tmp_path / "transition-faults.sqlite3")
    store.reserve(expected)
    updated = store.mark_outcome_unknown(expected, failure())
    update_connection = _Connection(
        lambda query: _Cursor(rows=[journal_module._row(expected)])
        if query.lstrip().startswith("SELECT operation_id")
        else _Cursor(rowcount=0)
    )
    monkeypatch.setattr(store, "_connect", lambda: update_connection)
    with pytest.raises(ControlJournalTransitionError):
        store.transition(expected, updated)
    assert update_connection.closed

    transition_sql_connection = _Connection(
        lambda query: (_ for _ in ()).throw(sqlite3.OperationalError())
        if query.lstrip().startswith("SELECT operation_id")
        else _Cursor()
    )
    monkeypatch.setattr(store, "_connect", lambda: transition_sql_connection)
    with pytest.raises(JournalError):
        store.transition(expected, updated)
    assert transition_sql_connection.closed


def test_private_schema_helpers_fail_closed_on_each_structural_mismatch() -> None:
    with pytest.raises(JournalError):
        _pragma_integer(_Connection(lambda _query: _Cursor(row=(True,))), "user_version")
    with pytest.raises(JournalError):
        _validate_schema(
            _Connection(
                lambda query: _Cursor(rows=[("bad",)])
                if "quick_check" in query
                else _Cursor()
            )
        )

    with pytest.raises(JournalError):
        _validate_table(
            _Connection(lambda _query: _Cursor(rows=[("short",)])),
            "control_metadata",
            (("key", "TEXT", 1, 1), ("value", "TEXT", 1, 0)),
            "CREATE TABLE control_metadata (key TEXT, value TEXT) STRICT",
            2,
        )
    bad_definition = _Connection(
        lambda _query: _Cursor(
            rows=[
                (0, "key", "TEXT", 1, None, 1),
                (1, "wrong", "TEXT", 1, None, 0),
            ]
        )
    )
    with pytest.raises(JournalError):
        _validate_table(
            bad_definition,
            "control_metadata",
            (("key", "TEXT", 1, 1), ("value", "TEXT", 1, 0)),
            "CREATE TABLE control_metadata (key TEXT, value TEXT) STRICT",
            2,
        )
    table_mismatch = _Connection(
        lambda query: _Cursor(
            rows=[
                (0, "key", "TEXT", 1, None, 1),
                (1, "value", "TEXT", 1, None, 0),
            ]
        )
        if "table_info" in query
        else _Cursor(rows=[("main", "control_metadata", "table", 2, 0, 0)])
    )
    with pytest.raises(JournalError):
        _validate_table(
            table_mismatch,
            "control_metadata",
            (("key", "TEXT", 1, 1), ("value", "TEXT", 1, 0)),
            "CREATE TABLE control_metadata (key TEXT, value TEXT) STRICT",
            2,
        )
    no_sql = _Connection(
        lambda query: _Cursor(
            rows=[
                (0, "key", "TEXT", 1, None, 1),
                (1, "value", "TEXT", 1, None, 0),
            ]
        )
        if "table_info" in query
        else _Cursor(rows=[("main", "control_metadata", "table", 2, 0, 1)])
        if "table_list" in query
        else _Cursor(row=None)
    )
    with pytest.raises(JournalError):
        _validate_table(
            no_sql,
            "control_metadata",
            (("key", "TEXT", 1, 1), ("value", "TEXT", 1, 0)),
            "CREATE TABLE control_metadata (key TEXT, value TEXT) STRICT",
            2,
        )
    wrong_sql = _Connection(
        lambda query: _Cursor(
            rows=[
                (0, "key", "TEXT", 1, None, 1),
                (1, "value", "TEXT", 1, None, 0),
            ]
        )
        if "table_info" in query
        else _Cursor(rows=[("main", "control_metadata", "table", 2, 0, 1)])
        if "table_list" in query
        else _Cursor(row=("CREATE TABLE control_metadata (key TEXT, value TEXT)",))
    )
    with pytest.raises(JournalError):
        _validate_table(
            wrong_sql,
            "control_metadata",
            (("key", "TEXT", 1, 1), ("value", "TEXT", 1, 0)),
            "CREATE TABLE control_metadata (key TEXT, value TEXT) STRICT",
            2,
        )

    with pytest.raises(JournalError):
        _validate_indexes(
            _Connection(lambda _query: _Cursor(rows=[("seq", "idx", 0, "origin", 0)])),
            "x",
            {("x",)},
        )
    bad_info = _Connection(
        lambda query: _Cursor(rows=[("seq", "idx", 1, "origin", 0)])
        if "index_list" in query
        else _Cursor(rows=[("bad", "extra")])
    )
    with pytest.raises(JournalError):
        _validate_indexes(bad_info, "x", {("x",)})
    with pytest.raises(JournalError):
        _validate_indexes(_Connection(lambda _query: _Cursor(rows=[])), "x", {("x",)})

    trigger_names = [("control_metadata_no_delete",), ("control_metadata_no_update",)]
    with pytest.raises(JournalError):
        _validate_triggers(
            _Connection(
                lambda query: _Cursor(rows=[("wrong",)])
                if "ORDER BY name" in query
                else _Cursor()
            )
        )
    with pytest.raises(JournalError):
        _validate_triggers(
            _Connection(
                lambda query: _Cursor(rows=trigger_names)
                if "ORDER BY name" in query
                else _Cursor(row=None)
            )
        )
    with pytest.raises(JournalError):
        _validate_triggers(
            _Connection(
                lambda query: _Cursor(rows=trigger_names)
                if "ORDER BY name" in query
                else _Cursor(row=("wrong",))
            )
        )


def test_private_decoders_and_identity_reject_malformed_rows() -> None:
    with pytest.raises(ControlJournalConflictError):
        _one([(), ()])
    with pytest.raises(JournalError):
        _decode((OPERATION, KEY))
    valid_row = journal_module._row(record())
    mismatch = list(valid_row)
    mismatch[3] = "resume"
    with pytest.raises(JournalError):
        _decode(tuple(mismatch))
    with pytest.raises(JournalError):
        _read_identity(
            _Connection(lambda _query: _Cursor(rows=[("wrong", "value")])),
            INSTALLATION,
        )
