from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

from klove.domain.control import ControlOperation, ControlResult, ControlStatus
from klove.domain.mqtt_ingress import MqttIngressRecord, MqttIngressState
from klove.errors import JournalError
from klove.persistence.mqtt_ingress_journal import (
    MqttIngressCapacityError,
    MqttIngressConflictError,
    MqttIngressJournal,
    _decode_record,
    _encode_record,
    _pragma_integer,
    _validate_schema,
)

PRINTER = "11111111-1111-4111-8111-111111111111"
DIGEST = "a" * 64


def record(sequence: str = "1", **updates: object) -> MqttIngressRecord:
    number = int(sequence)
    values: dict[str, object] = {
        "printer_uuid": PRINTER,
        "printer_record_revision": 1,
        "sequence_id": sequence,
        "operation": ControlOperation.PAUSE,
        "payload_hash": DIGEST,
        "state_token": "b" * 64,
        "idempotency_key": f"{number:08x}-0000-4000-8000-{number:012x}",
        "state": MqttIngressState.RESERVED,
    }
    values.update(updates)
    return MqttIngressRecord.model_validate(values)


def result(status: ControlStatus = ControlStatus.CONFIRMED) -> ControlResult:
    return ControlResult(operation=ControlOperation.PAUSE, status=status, code="bounded")


def store(tmp_path: Path) -> MqttIngressJournal:
    journal = MqttIngressJournal(tmp_path / "mqtt.sqlite3")
    journal.initialize()
    return journal


def test_reserve_duplicate_completion_and_restart_recovery(tmp_path: Path) -> None:
    journal = store(tmp_path)
    pending = record()
    assert journal.reserve(pending, capacity=2) == (pending, True)
    assert journal.reserve(pending, capacity=2) == (pending, False)
    assert journal.lookup(PRINTER, "1") == pending

    restarted = MqttIngressJournal(tmp_path / "mqtt.sqlite3")
    restarted.initialize()
    unknown = restarted.lookup(PRINTER, "1")
    assert unknown is not None
    assert unknown.state is MqttIngressState.OUTCOME_UNKNOWN
    assert unknown.result is not None
    assert unknown.result.status is ControlStatus.OUTCOME_UNKNOWN
    assert restarted.reserve(pending, capacity=2) == (unknown, False)
    with pytest.raises(MqttIngressConflictError):
        restarted.complete(pending, result())


@pytest.mark.parametrize(
    "updates",
    [
        {"payload_hash": "c" * 64},
        {"state_token": "d" * 64},
        {"operation": ControlOperation.CANCEL},
        {"printer_record_revision": 2},
    ],
)
def test_reserve_rejects_sequence_collision(tmp_path: Path, updates: dict[str, object]) -> None:
    journal = store(tmp_path)
    journal.reserve(record(), capacity=3)
    with pytest.raises(MqttIngressConflictError):
        journal.reserve(record("1", **updates), capacity=3)


def test_duplicate_sequence_keeps_first_canonical_idempotency_key(tmp_path: Path) -> None:
    journal = store(tmp_path)
    first = record()
    journal.reserve(first, capacity=2)
    disposable = record(idempotency_key="99999999-9999-4999-8999-999999999999")
    saved, created = journal.reserve(disposable, capacity=2)
    assert created is False
    assert saved == first
    assert saved.idempotency_key == first.idempotency_key


def test_reserve_rejects_idempotency_collision_two_rows_and_capacity(tmp_path: Path) -> None:
    journal = store(tmp_path)
    first = record()
    journal.reserve(first, capacity=2)
    with pytest.raises(MqttIngressConflictError):
        journal.reserve(record("2", idempotency_key=first.idempotency_key), capacity=2)
    journal.reserve(record("2"), capacity=2)
    with pytest.raises(MqttIngressCapacityError):
        journal.reserve(record("3"), capacity=2)
    with pytest.raises(JournalError):
        journal.reserve(record("3"), capacity=0)
    with pytest.raises(JournalError):
        journal.reserve(record(state=MqttIngressState.COMPLETE, result=result()), capacity=3)

    second = record("2")
    with pytest.raises(MqttIngressConflictError):
        journal.reserve(record("1", idempotency_key=second.idempotency_key), capacity=3)


def test_complete_is_single_exact_atomic_transition(tmp_path: Path) -> None:
    journal = store(tmp_path)
    pending = record()
    journal.reserve(pending, capacity=1)
    terminal = journal.complete(pending, result(ControlStatus.DENIED))
    assert terminal.state is MqttIngressState.COMPLETE
    assert journal.lookup(PRINTER, "1") == terminal
    with pytest.raises(MqttIngressConflictError):
        journal.complete(pending, result())


def test_concurrent_reservation_dispatches_only_once(tmp_path: Path) -> None:
    journal = store(tmp_path)
    pending = record()
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: journal.reserve(pending, capacity=8), range(16)))
    assert sum(created for _, created in outcomes) == 1
    assert all(saved == pending for saved, _ in outcomes)


def test_storage_is_private_wal_and_secret_free(tmp_path: Path) -> None:
    path = tmp_path / "mqtt.sqlite3"
    journal = MqttIngressJournal(path)
    journal.initialize()
    journal.reserve(record(), capacity=1)
    assert path.stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        raw = connection.execute("SELECT record_json FROM mqtt_ingress").fetchone()
    assert raw is not None
    assert "password" not in raw[0]
    assert "access_code" not in raw[0]


@pytest.mark.skipif(os.name == "nt", reason="POSIX private-file contract")
def test_every_operation_rechecks_private_permissions(tmp_path: Path) -> None:
    journal = store(tmp_path)
    journal.reserve(record(), capacity=1)
    path = tmp_path / "mqtt.sqlite3"
    path.chmod(0o644)
    for action in (
        lambda: journal.lookup(PRINTER, "1"),
        lambda: journal.reserve(record(), capacity=1),
        lambda: journal.complete(record(), result()),
    ):
        with pytest.raises(JournalError):
            action()


@pytest.mark.skipif(os.name == "nt", reason="POSIX private-file contract")
def test_initialize_rejects_public_symlink_directory_and_unsafe_parent(tmp_path: Path) -> None:
    public = tmp_path / "public.sqlite3"
    public.touch(mode=0o600)
    public.chmod(0o644)
    with pytest.raises(JournalError):
        MqttIngressJournal(public).initialize()

    target = tmp_path / "target.sqlite3"
    target.touch(mode=0o600)
    link = tmp_path / "link.sqlite3"
    link.symlink_to(target)
    with pytest.raises(JournalError):
        MqttIngressJournal(link).initialize()

    directory = tmp_path / "directory.sqlite3"
    directory.mkdir()
    with pytest.raises(JournalError):
        MqttIngressJournal(directory).initialize()

    public_parent = tmp_path / "public-parent"
    public_parent.mkdir()
    public_parent.chmod(0o755)
    with pytest.raises(JournalError):
        MqttIngressJournal(public_parent / "mqtt.sqlite3").initialize()


def test_rejects_missing_parent_future_version_and_wrong_schema(tmp_path: Path) -> None:
    with pytest.raises(JournalError):
        MqttIngressJournal(tmp_path / "missing" / "mqtt.sqlite3").initialize()
    for name, setup in (
        ("future", "PRAGMA user_version = 2"),
        ("wrong", "CREATE TABLE mqtt_ingress (wrong TEXT) STRICT; PRAGMA user_version = 1"),
    ):
        path = tmp_path / f"{name}.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript(setup)
        path.chmod(0o600)
        with pytest.raises(JournalError):
            MqttIngressJournal(path).initialize()


@pytest.mark.parametrize(
    "weakness",
    ["not_strict", "missing_unique", "extra_index", "partial_index", "expression_index"],
)
def test_rejects_structurally_weakened_schema(tmp_path: Path, weakness: str) -> None:
    path = tmp_path / f"{weakness}.sqlite3"
    strict = "" if weakness == "not_strict" else " STRICT"
    unique = "" if weakness == "missing_unique" else " UNIQUE"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            f"""CREATE TABLE mqtt_ingress (
                printer_uuid TEXT NOT NULL, sequence_id TEXT NOT NULL,
                idempotency_key TEXT{unique} NOT NULL, state TEXT NOT NULL,
                record_json TEXT NOT NULL, PRIMARY KEY (printer_uuid, sequence_id)
            ){strict}"""
        )
        if weakness == "extra_index":
            connection.execute("CREATE INDEX extra ON mqtt_ingress(state)")
        elif weakness == "partial_index":
            connection.execute(
                "CREATE UNIQUE INDEX partial ON mqtt_ingress(state) WHERE state = 'reserved'"
            )
        elif weakness == "expression_index":
            connection.execute("CREATE UNIQUE INDEX expression ON mqtt_ingress(lower(state))")
        connection.execute("PRAGMA user_version = 1")
    path.chmod(0o600)
    with pytest.raises(JournalError):
        MqttIngressJournal(path).initialize()


@pytest.mark.parametrize("corruption", ["json", "state", "identity", "idempotency", "version"])
def test_initialize_rejects_corrupt_or_contradictory_rows(tmp_path: Path, corruption: str) -> None:
    journal = store(tmp_path)
    journal.reserve(record(), capacity=1)
    path = tmp_path / "mqtt.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        if corruption == "json":
            connection.execute("UPDATE mqtt_ingress SET record_json = 'not-json'")
        elif corruption == "state":
            connection.execute("UPDATE mqtt_ingress SET state = 'complete'")
        elif corruption == "identity":
            connection.execute("UPDATE mqtt_ingress SET sequence_id = '2'")
        elif corruption == "idempotency":
            connection.execute(
                "UPDATE mqtt_ingress SET idempotency_key = ?",
                ("99999999-9999-4999-8999-999999999999",),
            )
        else:
            raw = connection.execute("SELECT record_json FROM mqtt_ingress").fetchone()
            assert raw is not None
            data = json.loads(raw[0])
            data["record_version"] = "2"
            connection.execute("UPDATE mqtt_ingress SET record_json = ?", (json.dumps(data),))
    with pytest.raises(JournalError):
        MqttIngressJournal(path).initialize()


def test_commit_fault_rolls_back_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    class Cursor:
        rowcount = 1

        def fetchall(self) -> list[tuple[object, ...]]:
            return []

        def fetchone(self) -> tuple[int]:
            return (0,)

    class Connection:
        def execute(self, query: str, _params: object = None) -> Cursor:
            operation = query.strip().split(maxsplit=1)[0]
            calls.append(operation)
            if operation == "COMMIT":
                raise sqlite3.OperationalError
            return Cursor()

        def close(self) -> None:
            calls.append("CLOSE")

    journal = MqttIngressJournal(tmp_path / "mqtt.sqlite3")
    monkeypatch.setattr(journal, "_connect", Connection)
    with pytest.raises(JournalError):
        journal.reserve(record(), capacity=1)
    assert calls[-2:] == ["ROLLBACK", "CLOSE"]


def test_completion_commit_fault_rolls_back_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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

    journal = MqttIngressJournal(tmp_path / "mqtt.sqlite3")
    monkeypatch.setattr(journal, "_connect", Connection)
    with pytest.raises(JournalError):
        journal.complete(record(), result())
    assert calls[-2:] == ["ROLLBACK", "CLOSE"]


def test_initialize_schema_fault_rolls_back_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    class Cursor:
        def fetchone(self) -> tuple[int]:
            return (0,)

    class Connection:
        def execute(self, query: str, _params: object = None) -> Cursor:
            operation = query.strip().split(maxsplit=1)[0]
            calls.append(operation)
            if "CREATE TABLE" in query:
                raise sqlite3.OperationalError
            return Cursor()

        def close(self) -> None:
            calls.append("CLOSE")

    journal = MqttIngressJournal(tmp_path / "mqtt.sqlite3")
    monkeypatch.setattr(journal, "_connect", Connection)
    with pytest.raises(JournalError):
        journal.initialize()
    assert "ROLLBACK" in calls
    assert calls[-1] == "CLOSE"


def test_reserve_rejects_malformed_count_and_recovery_lost_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    class Cursor:
        def __init__(
            self,
            *,
            rows: list[tuple[object, ...]] | None = None,
            row: tuple[object, ...] | None = None,
            rowcount: int = 1,
        ) -> None:
            self._rows = rows or []
            self._row = row
            self.rowcount = rowcount

        def fetchall(self) -> list[tuple[object, ...]]:
            return self._rows

        def fetchone(self) -> tuple[object, ...] | None:
            return self._row

    class MalformedCountConnection:
        def execute(self, query: str, _params: object = None) -> Cursor:
            calls.append(query.strip().split(maxsplit=1)[0])
            if "COUNT" in query:
                return Cursor(row=(True,))
            return Cursor()

        def close(self) -> None:
            calls.append("CLOSE")

    journal = MqttIngressJournal(tmp_path / "mqtt.sqlite3")
    monkeypatch.setattr(journal, "_connect", MalformedCountConnection)
    with pytest.raises(JournalError):
        journal.reserve(record(), capacity=1)
    assert calls[-2:] == ["ROLLBACK", "CLOSE"]

    pending = record()

    class LostUpdateConnection:
        def execute(self, query: str, _params: object = None) -> Cursor:
            calls.append(query.strip().split(maxsplit=1)[0])
            if query.lstrip().startswith("SELECT"):
                return Cursor(
                    rows=[
                        (
                            PRINTER,
                            "1",
                            pending.idempotency_key,
                            pending.state.value,
                            _encode_record(pending),
                        )
                    ]
                )
            if query.lstrip().startswith("UPDATE"):
                return Cursor(rowcount=0)
            return Cursor()

    with pytest.raises(JournalError):
        MqttIngressJournal._recover_reserved(LostUpdateConnection())  # type: ignore[arg-type]
    assert calls[-1] == "ROLLBACK"


@pytest.mark.parametrize("failure", ["journal_mode", "pragma", "connect", "journal_error"])
def test_connect_closes_and_wraps_setup_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path = tmp_path / "mqtt.sqlite3"
    path.touch(mode=0o600)
    closed = False

    class Cursor:
        def fetchone(self) -> tuple[str]:
            return ("delete",)

    class Connection:
        def execute(self, query: str) -> Cursor:
            if failure == "pragma" and "trusted_schema" in query:
                raise sqlite3.OperationalError
            return Cursor()

        def close(self) -> None:
            nonlocal closed
            closed = True

    def connect(*_args: object, **_kwargs: object) -> Connection:
        if failure == "connect":
            raise sqlite3.OperationalError
        if failure == "journal_error":
            raise JournalError
        return Connection()

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(JournalError):
        MqttIngressJournal(path)._connect()
    assert closed is (failure not in {"connect", "journal_error"})


def test_corruption_error_does_not_echo_hostile_record(tmp_path: Path) -> None:
    journal = store(tmp_path)
    journal.reserve(record(), capacity=1)
    marker = "DO_NOT_ECHO_PRIVATE_MATERIAL"
    with closing(sqlite3.connect(tmp_path / "mqtt.sqlite3")) as connection, connection:
        connection.execute("UPDATE mqtt_ingress SET record_json = ?", (marker,))
    with pytest.raises(JournalError) as captured:
        journal.lookup(PRINTER, "1")
    assert marker not in str(captured.value)


def test_low_level_decoders_and_pragma_are_strict() -> None:
    for row in ((), (1,), ("{}", "extra")):
        with pytest.raises(JournalError):
            _decode_record(row)

    class Cursor:
        def __init__(self, value: tuple[object, ...] | None) -> None:
            self.value = value

        def fetchone(self) -> tuple[object, ...] | None:
            return self.value

    class Connection:
        def __init__(self, value: tuple[object, ...] | None) -> None:
            self.value = value

        def execute(self, _query: str) -> Cursor:
            return Cursor(self.value)

    for value in (None, (), (True,), ("1",), (1, 2)):
        with pytest.raises(JournalError):
            _pragma_integer(Connection(value), "user_version")  # type: ignore[arg-type]
    with pytest.raises(JournalError):
        _validate_schema(Connection((2,)))  # type: ignore[arg-type]


def test_schema_decoder_wraps_malformed_rows() -> None:
    class Cursor:
        def fetchall(self) -> list[tuple[object, ...]]:
            return [("short",)]

        def fetchone(self) -> tuple[int]:
            return (1,)

    class Connection:
        def execute(self, _query: str) -> Cursor:
            return Cursor()

    with pytest.raises(JournalError):
        _validate_schema(Connection())  # type: ignore[arg-type]
