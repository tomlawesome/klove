from __future__ import annotations

import hashlib
import json
import secrets as runtime_secrets
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest

from klove.domain.onboarding import RegisteredPrinter, RegistryOperationRecord
from klove.persistence.printer_registry import PrinterStore, _operation_row, _printer_row
from klove.persistence.printer_registry_errors import RegistryStoreError
from klove.persistence.printer_registry_migrations import (
    V1_TO_V2,
    RegistryMigrationStep,
    _apply_plan,
    _user_version,
    ensure_registry_schema,
    validate_v1_source,
    validate_v2,
)
from klove.persistence.printer_registry_schema import (
    _MAPPING_HISTORY_COLUMNS,
    _PROFILE_HISTORY_COLUMNS,
    _SCHEMA_VERSION_V1,
    _SCHEMA_VERSION_V2,
    _initialize_schema_v2,
    _validate_schema_v2_structure,
)
from klove.persistence.secret_store import SecretStore

from ..onboarding_helpers import make_stores

FIXTURE_DIRECTORY = Path(__file__).parents[1] / "fixtures" / "registry-migrations"


def _version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    assert row is not None
    return int(row[0])


def _table_validator(name: str, columns: tuple[str, ...]) -> Callable[[sqlite3.Connection], None]:
    def validate(connection: sqlite3.Connection) -> None:
        actual = tuple(row[1] for row in connection.execute(f"PRAGMA table_info({name})"))
        if actual != columns:
            raise RegistryStoreError

    return validate


def _execute(sql: str) -> Callable[[sqlite3.Connection], None]:
    def apply(connection: sqlite3.Connection) -> None:
        connection.execute(sql)

    return apply


def test_empty_version_zero_initializes_once_and_current_reopen_only_validates() -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    calls: list[str] = []

    def initialize(target: sqlite3.Connection) -> None:
        calls.append("initialize")
        target.execute("CREATE TABLE exact (value TEXT NOT NULL) STRICT")

    validator = _table_validator("exact", ("value",))
    ensure_registry_schema(
        connection,
        current_version=1,
        initialize_current=initialize,
        validators={1: validator},
    )
    ensure_registry_schema(
        connection,
        current_version=1,
        initialize_current=initialize,
        validators={1: validator},
    )
    assert calls == ["initialize"]
    assert _version(connection) == 1
    connection.close()


def test_invalid_current_schema_contract_is_rejected() -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    with pytest.raises(RegistryStoreError):
        ensure_registry_schema(
            connection,
            current_version=0,
            initialize_current=lambda _target: None,
            validators={},
        )
    connection.close()


@pytest.mark.parametrize(
    "object_sql",
    [
        "CREATE TABLE hostile (value TEXT)",
        "CREATE VIEW hostile AS SELECT 1 AS value",
    ],
)
def test_populated_version_zero_is_rejected_without_mutation(object_sql: str) -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute(object_sql)
    before = connection.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name = 'hostile'"
    ).fetchall()
    with pytest.raises(RegistryStoreError):
        ensure_registry_schema(
            connection,
            current_version=1,
            initialize_current=_execute("CREATE TABLE exact (value TEXT)"),
            validators={1: lambda _target: None},
        )
    assert _version(connection) == 0
    assert (
        connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name = 'hostile'"
        ).fetchall()
        == before
    )
    connection.close()


def test_contiguous_synthetic_plan_is_atomic_and_reopens_exact_target() -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute("CREATE TABLE exact (value TEXT NOT NULL) STRICT")
    connection.execute("INSERT INTO exact VALUES ('preserved')")
    connection.execute("PRAGMA user_version = 1")
    validators = {
        1: _table_validator("exact", ("value",)),
        2: _table_validator("exact", ("value", "generation")),
        3: _table_validator("exact", ("value", "generation", "reference")),
    }
    steps = (
        RegistryMigrationStep(1, 2, _execute("ALTER TABLE exact ADD generation INTEGER")),
        RegistryMigrationStep(2, 3, _execute("ALTER TABLE exact ADD reference TEXT")),
    )
    ensure_registry_schema(
        connection,
        current_version=3,
        initialize_current=lambda _target: None,
        validators=validators,
        steps=steps,
    )
    ensure_registry_schema(
        connection,
        current_version=3,
        initialize_current=lambda _target: None,
        validators=validators,
        steps=steps,
    )
    assert _version(connection) == 3
    assert connection.execute("SELECT value FROM exact").fetchall() == [("preserved",)]
    connection.close()


@pytest.mark.parametrize("failure", ["apply", "target"])
def test_migration_failure_rolls_back_to_exact_source(failure: str) -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute("CREATE TABLE exact (value TEXT NOT NULL) STRICT")
    connection.execute("INSERT INTO exact VALUES ('preserved')")
    connection.execute("PRAGMA user_version = 1")

    def apply(target: sqlite3.Connection) -> None:
        target.execute("ALTER TABLE exact ADD generation INTEGER")
        if failure == "apply":
            raise sqlite3.OperationalError

    def validate_target(target: sqlite3.Connection) -> None:
        if failure == "target":
            raise RegistryStoreError
        _table_validator("exact", ("value", "generation"))(target)

    with pytest.raises((RegistryStoreError, sqlite3.OperationalError)):
        ensure_registry_schema(
            connection,
            current_version=2,
            initialize_current=lambda _target: None,
            validators={1: _table_validator("exact", ("value",)), 2: validate_target},
            steps=(RegistryMigrationStep(1, 2, apply),),
        )
    assert _version(connection) == 1
    assert tuple(row[1] for row in connection.execute("PRAGMA table_info(exact)")) == ("value",)
    assert connection.execute("SELECT value FROM exact").fetchall() == [("preserved",)]
    connection.close()


@pytest.mark.parametrize(
    ("version", "current", "validators", "steps"),
    [
        (3, 2, {2: lambda _target: None}, ()),
        (1, 2, {2: lambda _target: None}, ()),
        (1, 3, {1: lambda _target: None, 2: lambda _target: None, 3: lambda _target: None}, ()),
        (
            1,
            2,
            {1: lambda _target: None, 2: lambda _target: None},
            (RegistryMigrationStep(1, 3, lambda _target: None),),
        ),
    ],
)
def test_future_too_old_gap_and_noncontiguous_plans_fail_closed(
    version: int,
    current: int,
    validators: dict[int, Callable[[sqlite3.Connection], None]],
    steps: tuple[RegistryMigrationStep, ...],
) -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute(f"PRAGMA user_version = {version}")
    with pytest.raises(RegistryStoreError):
        ensure_registry_schema(
            connection,
            current_version=current,
            initialize_current=lambda _target: None,
            validators=validators,
            steps=steps,
        )
    assert _version(connection) == version
    connection.close()


def test_competing_initializer_lock_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "locked.sqlite3"
    first = sqlite3.connect(path, timeout=0, isolation_level=None)
    second = sqlite3.connect(path, timeout=0, isolation_level=None)
    first.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(RegistryStoreError):
            ensure_registry_schema(
                second,
                current_version=1,
                initialize_current=_execute("CREATE TABLE exact (value TEXT)"),
                validators={1: lambda _target: None},
            )
    finally:
        first.execute("ROLLBACK")
        first.close()
        second.close()


def test_transaction_rechecks_source_and_defends_against_inconsistent_plan() -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute("PRAGMA user_version = 2")
    with pytest.raises(RegistryStoreError):
        _apply_plan(
            connection,
            1,
            {1: lambda _target: None},
            (RegistryMigrationStep(1, 2, lambda _target: None),),
        )
    connection.execute("PRAGMA user_version = 1")
    with pytest.raises(RegistryStoreError):
        _apply_plan(
            connection,
            1,
            {1: lambda _target: None},
            (RegistryMigrationStep(2, 3, lambda _target: None),),
        )
    connection.close()


def test_malformed_pragma_begin_and_rollback_failures_are_bounded() -> None:
    class Cursor:
        def __init__(self, row: tuple[object, ...] | None = None) -> None:
            self._row = row

        def fetchone(self) -> tuple[object, ...] | None:
            return self._row

    class Malformed:
        def execute(self, _query: str) -> Cursor:
            return Cursor(None)

    with pytest.raises(RegistryStoreError):
        _user_version(Malformed())  # type: ignore[arg-type]

    class BeginFailure:
        def execute(self, query: str) -> Cursor:
            if query == "PRAGMA user_version":
                return Cursor((0,))
            raise sqlite3.OperationalError

    with pytest.raises(RegistryStoreError):
        ensure_registry_schema(
            BeginFailure(),  # type: ignore[arg-type]
            current_version=1,
            initialize_current=lambda _target: None,
            validators={1: lambda _target: None},
        )

    class RollbackFailure:
        def execute(self, query: str) -> Cursor:
            if query == "PRAGMA user_version":
                return Cursor((0,))
            if query == "BEGIN EXCLUSIVE":
                return Cursor()
            if query == "ROLLBACK":
                raise sqlite3.OperationalError
            raise AssertionError(query)

    with pytest.raises(RegistryStoreError):
        ensure_registry_schema(
            RollbackFailure(),  # type: ignore[arg-type]
            current_version=1,
            initialize_current=lambda _target: (_ for _ in ()).throw(ValueError()),
            validators={1: lambda _target: None},
        )


def test_v1_fixture_digest_records_and_ephemeral_secrets_are_exact(tmp_path: Path) -> None:
    fixture_path = FIXTURE_DIRECTORY / "v1.json"
    expected_digest = (FIXTURE_DIRECTORY / "v1.sha256").read_text(encoding="ascii").split()[0]
    fixture_bytes = fixture_path.read_bytes()
    assert hashlib.sha256(fixture_bytes).hexdigest() == expected_digest
    document = json.loads(fixture_bytes)
    assert document["schema_version"] == 1
    printer = RegisteredPrinter.model_validate_json(json.dumps(document["printer"]))
    operation = RegistryOperationRecord.model_validate_json(json.dumps(document["operation"]))

    secret_directory = tmp_path / "secrets"
    secret_directory.mkdir(mode=0o700)
    secret_store = SecretStore(secret_directory)
    secret_store.initialize()
    moonraker_value = runtime_secrets.token_urlsafe(24)
    compatibility_value = runtime_secrets.token_urlsafe(15)
    assert printer.moonraker_credential_ref is not None
    assert printer.compatibility_credential_ref is not None
    database = tmp_path / "registry.sqlite3"
    store = PrinterStore(database, secret_store)
    store.initialize()
    secret_store.write(printer.moonraker_credential_ref, moonraker_value, minimum_length=32)
    secret_store.write(
        printer.compatibility_credential_ref,
        compatibility_value,
        minimum_length=20,
    )
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            """
            INSERT INTO registry_printers (
                printer_uuid, endpoint, lifecycle, revision,
                moonraker_ref, compatibility_ref, record_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            _printer_row(printer),
        )
        connection.execute(
            """
            INSERT INTO registry_operations (
                idempotency_key, operation, printer_uuid,
                request_fingerprint, state, record_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            _operation_row(operation),
        )
    PrinterStore(database, secret_store).initialize()
    assert store.get(printer.printer_uuid) == printer
    assert store.lookup_operation(operation.idempotency_key) == operation
    assert (
        moonraker_value.encode() not in fixture_bytes
        and moonraker_value.encode() not in database.read_bytes()
    )
    assert compatibility_value.encode() not in fixture_bytes
    assert compatibility_value.encode() not in database.read_bytes()


def test_v2_schema_has_exact_composite_keys_and_immutable_triggers() -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    _initialize_schema_v2(connection, "a" * 64)
    _validate_schema_v2_structure(connection)
    assert tuple(
        row[1] for row in connection.execute("PRAGMA table_info(registry_mapping_history)")
    ) == (*_MAPPING_HISTORY_COLUMNS,)
    assert tuple(
        row[5] for row in connection.execute("PRAGMA table_info(registry_mapping_history)")
    ) == (
        1,
        2,
        0,
        0,
        0,
    )
    assert tuple(
        row[1] for row in connection.execute("PRAGMA table_info(registry_profile_history)")
    ) == (*_PROFILE_HISTORY_COLUMNS,)
    assert tuple(
        row[5] for row in connection.execute("PRAGMA table_info(registry_profile_history)")
    ) == (
        1,
        2,
        3,
        0,
        0,
        4,
        0,
    )
    connection.execute(
        """
        INSERT INTO registry_mapping_history VALUES (?, ?, ?, ?, ?)
        """,
        ("11111111-1111-4111-8111-111111111111", 1, 0, "a" * 64, "{}"),
    )
    connection.execute(
        """
        INSERT INTO registry_profile_history VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "11111111-1111-4111-8111-111111111111",
            1,
            "profile",
            1,
            "b" * 64,
            "baseline",
            "{}",
        ),
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE registry_mapping_history SET mapping_json = '{}'")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM registry_mapping_history")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE registry_profile_history SET profile_json = '{}' ")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM registry_profile_history")
    connection.close()


def test_v1_to_v2_backfill_is_fixture_bound_and_reopens_exactly(tmp_path: Path) -> None:
    fixture_path = FIXTURE_DIRECTORY / "v2.json"
    assert (
        hashlib.sha256(fixture_path.read_bytes()).hexdigest()
        == ((FIXTURE_DIRECTORY / "v2.sha256").read_text(encoding="ascii").split()[0])
    )
    document = json.loads((FIXTURE_DIRECTORY / "v1.json").read_bytes())
    printer = RegisteredPrinter.model_validate_json(json.dumps(document["printer"]))
    operation = RegistryOperationRecord.model_validate_json(json.dumps(document["operation"]))
    _secrets, store = make_stores(tmp_path)
    with closing(sqlite3.connect(tmp_path / "registry.sqlite3")) as connection, connection:
        connection.execute(
            "INSERT INTO registry_printers VALUES (?, ?, ?, ?, ?, ?, ?)", _printer_row(printer)
        )
        connection.execute(
            "INSERT INTO registry_operations VALUES (?, ?, ?, ?, ?, ?)", _operation_row(operation)
        )
    with closing(
        sqlite3.connect(tmp_path / "registry.sqlite3", isolation_level=None)
    ) as connection:
        ensure_registry_schema(
            connection,
            current_version=_SCHEMA_VERSION_V2,
            initialize_current=lambda _target: None,
            validators={_SCHEMA_VERSION_V1: validate_v1_source, _SCHEMA_VERSION_V2: validate_v2},
            steps=(V1_TO_V2,),
        )
        assert _version(connection) == _SCHEMA_VERSION_V2
        expected = json.loads((FIXTURE_DIRECTORY / "v2.json").read_bytes())
        mapping = connection.execute(
            """
            SELECT printer_uuid, registry_revision, observed_at_unix_ms,
                   mapping_fingerprint, mapping_json
            FROM registry_mapping_history
            """
        ).fetchall()
        profiles = connection.execute(
            """
            SELECT printer_uuid, registry_revision, slicer_profile_id,
                   generation, profile_fingerprint, event, profile_json
            FROM registry_profile_history
            """
        ).fetchall()
        expected_mapping = expected["mapping_history"][0]
        assert mapping == [
            (
                expected_mapping["printer_uuid"],
                expected_mapping["registry_revision"],
                expected_mapping["observed_at_unix_ms"],
                expected_mapping["mapping_fingerprint"],
                expected_mapping["mapping_json"],
            )
        ]
        expected_profile = expected["profile_history"][0]
        assert profiles == [
            (
                expected_profile["printer_uuid"],
                expected_profile["registry_revision"],
                expected_profile["slicer_profile_id"],
                expected_profile["generation"],
                expected_profile["profile_fingerprint"],
                expected_profile["event"],
                expected_profile["profile_json"],
            )
        ]
        validate_v2(connection)
    assert store.get(printer.printer_uuid) == printer
