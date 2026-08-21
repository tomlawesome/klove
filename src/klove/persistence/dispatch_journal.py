"""Private SQLite/WAL journal for coordinator state and ambiguity fences."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Final, cast
from uuid import uuid4

from pydantic import ValidationError

from klove.domain.dispatch import DispatchJournalRecord, DispatchState
from klove.errors import JournalError
from klove.persistence.private_files import (
    PrivateFileError,
    prepare_private_file,
    require_private_file,
)
from klove.persistence.store_identity import StoreIdentity, canonical_uuid4

_SCHEMA_VERSION: Final = 2
_SCHEMA_VERSION_V1: Final = 1
_SCHEMA_VERSION_V2: Final = 2
_ACTIVE_STATES: Final = (
    DispatchState.RECEIVING.value,
    DispatchState.ACCEPTED.value,
    DispatchState.UPLOADING.value,
    DispatchState.VERIFIED.value,
    DispatchState.STARTING.value,
    DispatchState.PRINTING.value,
    DispatchState.OUTCOME_UNKNOWN.value,
)
_TABLE_COLUMNS: Final = (
    "operation_id",
    "idempotency_key",
    "printer_uuid",
    "state",
    "record_json",
)
_TABLE_DEFINITION: Final = (
    ("operation_id", "TEXT", 1, 1),
    ("idempotency_key", "TEXT", 1, 0),
    ("printer_uuid", "TEXT", 1, 0),
    ("state", "TEXT", 1, 0),
    ("record_json", "TEXT", 1, 0),
)
_METADATA_DEFINITION: Final = (("key", "TEXT", 1, 1), ("value", "TEXT", 1, 0))
_METADATA_TABLE_SQL: Final = """
CREATE TABLE dispatch_metadata (
    key TEXT PRIMARY KEY NOT NULL,
    value TEXT NOT NULL
) STRICT
"""
_METADATA_TRIGGER_SQL: Final = """
CREATE TRIGGER dispatch_metadata_no_update
BEFORE UPDATE ON dispatch_metadata
BEGIN
    SELECT RAISE(ABORT, 'dispatch metadata is immutable');
END
"""
_METADATA_DELETE_TRIGGER_SQL: Final = """
CREATE TRIGGER dispatch_metadata_no_delete
BEFORE DELETE ON dispatch_metadata
BEGIN
    SELECT RAISE(ABORT, 'dispatch metadata is immutable');
END
"""


class DispatchJournalConflictError(JournalError):
    """One operation or idempotency identity is already bound differently."""


class DispatchJournalFenceError(JournalError):
    """Another active or ambiguous coordinator operation fences this printer."""


class DispatchJournalCapacityError(JournalError):
    """The durable journal reached its configured non-evicting capacity."""


class DispatchJournal:
    """Persist exact coordinator progress before each unsafe downstream stage."""

    def __init__(self, path: Path, installation_id: str) -> None:
        self._path = path
        self._installation_id = canonical_uuid4(installation_id)
        self._identity: StoreIdentity | None = None

    @property
    def identity(self) -> StoreIdentity:
        """Return the immutable installation, store, and schema identity."""
        if self._identity is None:
            raise JournalError
        return self._identity

    def initialize(self) -> None:
        """Create or reject the exact private schema before accepting work."""
        self._identity = None
        try:
            prepare_private_file(self._path)
            connection = self._connect()
            try:
                version = _pragma_integer(connection, "user_version")
                if version == 0:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        _initialize_schema(connection, self._installation_id, str(uuid4()))
                        connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                        connection.execute("COMMIT")
                    except sqlite3.Error:
                        connection.execute("ROLLBACK")
                        raise
                    store_id = _read_store_id(connection, self._installation_id)
                elif version == _SCHEMA_VERSION_V1:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        _validate_v1_schema(connection)
                        store_id = str(uuid4())
                        _initialize_metadata(connection, self._installation_id, store_id)
                        connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                        connection.execute("COMMIT")
                    except (JournalError, sqlite3.Error):
                        connection.execute("ROLLBACK")
                        raise
                elif version != _SCHEMA_VERSION_V2:
                    raise JournalError
                _validate_schema(connection)
                if version == _SCHEMA_VERSION_V2:
                    store_id = _read_store_id(connection, self._installation_id)
                self._identity = StoreIdentity(self._installation_id, store_id, _SCHEMA_VERSION)
            finally:
                connection.close()
        except JournalError:
            raise
        except (OSError, PrivateFileError, sqlite3.Error, ValueError) as exc:
            raise JournalError from exc

    def lookup_operation_state(self, operation_id: str) -> DispatchState | None:
        """Return the exact durable state for one operation identity."""
        record = self.lookup_operation(operation_id)
        return None if record is None else record.state

    def lookup_operation(self, operation_id: str) -> DispatchJournalRecord | None:
        """Return one exact, fully validated durable operation record."""
        operation_id = canonical_uuid4(operation_id)
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT operation_id, idempotency_key, printer_uuid, state, record_json
                FROM dispatch_operations WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
            if row is None:
                return None
            return _decode(row)
        except (sqlite3.Error, UnicodeError, ValidationError, ValueError) as exc:
            raise JournalError from exc
        finally:
            connection.close()

    def lookup(self, operation_id: str, idempotency_key: str) -> DispatchJournalRecord | None:
        """Find either durable identity and reject a partial/colliding pair."""
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT operation_id, idempotency_key, printer_uuid, state, record_json
                FROM dispatch_operations
                WHERE operation_id = ? OR idempotency_key = ?
                """,
                (operation_id, idempotency_key),
            ).fetchall()
            record = _one(rows)
            if record is not None and (
                record.request.operation_id != operation_id
                or record.request.idempotency_key != idempotency_key
            ):
                return None
            return record
        except (sqlite3.Error, UnicodeError, ValidationError, ValueError) as exc:
            raise JournalError from exc
        finally:
            connection.close()

    def reserve(
        self,
        record: DispatchJournalRecord,
        *,
        capacity: int,
    ) -> tuple[DispatchJournalRecord, bool]:
        """Reserve one receiving operation or return its exact durable duplicate."""
        if record.state is not DispatchState.RECEIVING or capacity < 1:
            raise JournalError
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = _one(
                    connection.execute(
                        """
                        SELECT operation_id, idempotency_key, printer_uuid, state, record_json
                        FROM dispatch_operations
                        WHERE operation_id = ? OR idempotency_key = ?
                        """,
                        (record.request.operation_id, record.request.idempotency_key),
                    ).fetchall()
                )
                if existing is not None:
                    connection.execute("COMMIT")
                    return existing, False
                count = connection.execute("SELECT COUNT(*) FROM dispatch_operations").fetchone()
                if count is None or len(count) != 1 or type(count[0]) is not int:
                    raise JournalError
                if count[0] >= capacity:
                    raise DispatchJournalCapacityError
                fenced = connection.execute(
                    """
                    SELECT 1 FROM dispatch_operations
                    WHERE printer_uuid = ?
                      AND state IN (?, ?, ?, ?, ?, ?, ?)
                    LIMIT 1
                    """,
                    (record.request.printer_uuid, *_ACTIVE_STATES),
                ).fetchone()
                if fenced is not None:
                    raise DispatchJournalFenceError
                connection.execute(
                    """
                    INSERT INTO dispatch_operations (
                        operation_id, idempotency_key, printer_uuid, state, record_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    _row(record),
                )
                connection.execute("COMMIT")
                return record, True
            except (
                DispatchJournalCapacityError,
                DispatchJournalFenceError,
                JournalError,
                sqlite3.Error,
                UnicodeError,
                ValidationError,
                ValueError,
            ):
                connection.execute("ROLLBACK")
                raise
        except (DispatchJournalCapacityError, DispatchJournalFenceError, JournalError):
            raise
        except (sqlite3.Error, UnicodeError, ValidationError, ValueError) as exc:
            raise JournalError from exc
        finally:
            connection.close()

    def replace(
        self,
        expected: DispatchJournalRecord,
        updated: DispatchJournalRecord,
    ) -> DispatchJournalRecord:
        """Atomically replace one exact record, refusing stale concurrent evidence."""
        if (
            expected.request.operation_id != updated.request.operation_id
            or expected.request.idempotency_key != updated.request.idempotency_key
            or expected.grant != updated.grant
            or expected.request != updated.request
        ):
            raise DispatchJournalConflictError
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = _one(
                    connection.execute(
                        """
                        SELECT operation_id, idempotency_key, printer_uuid, state, record_json
                        FROM dispatch_operations
                        WHERE operation_id = ? AND idempotency_key = ?
                        """,
                        (expected.request.operation_id, expected.request.idempotency_key),
                    ).fetchall()
                )
                if current != expected:
                    raise DispatchJournalConflictError
                cursor = connection.execute(
                    """
                    UPDATE dispatch_operations
                    SET state = ?, record_json = ?
                    WHERE operation_id = ? AND idempotency_key = ?
                    """,
                    (
                        updated.state.value,
                        updated.model_dump_json(),
                        updated.request.operation_id,
                        updated.request.idempotency_key,
                    ),
                )
                if cursor.rowcount != 1:
                    raise DispatchJournalConflictError
                connection.execute("COMMIT")
                return updated
            except (DispatchJournalConflictError, JournalError, sqlite3.Error):
                connection.execute("ROLLBACK")
                raise
        except (DispatchJournalConflictError, JournalError):
            raise
        except sqlite3.Error as exc:
            raise JournalError from exc
        finally:
            connection.close()

    def records(self) -> tuple[DispatchJournalRecord, ...]:
        """Return all durable records for startup reconciliation in stable order."""
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT operation_id, idempotency_key, printer_uuid, state, record_json
                FROM dispatch_operations ORDER BY operation_id
                """
            ).fetchall()
            return tuple(_decode(row) for row in rows)
        except (sqlite3.Error, UnicodeError, ValidationError, ValueError) as exc:
            raise JournalError from exc
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            require_private_file(self._path)
            connection = sqlite3.connect(self._path, timeout=5, isolation_level=None)
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            if connection.execute("PRAGMA journal_mode = WAL").fetchone() != ("wal",):
                raise JournalError
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except JournalError:
            if connection is not None:
                connection.close()
            raise
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            raise JournalError from exc


def _pragma_integer(connection: sqlite3.Connection, name: str) -> int:
    row = connection.execute(f"PRAGMA {name}").fetchone()
    if row is None or len(row) != 1 or type(row[0]) is not int:
        raise JournalError
    return row[0]


def _initialize_schema(
    connection: sqlite3.Connection, installation_id: str, store_id: str
) -> None:
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
    _initialize_metadata(connection, installation_id, store_id)


def _initialize_metadata(
    connection: sqlite3.Connection, installation_id: str, store_id: str
) -> None:
    connection.execute(_METADATA_TABLE_SQL)
    connection.executemany(
        "INSERT INTO dispatch_metadata (key, value) VALUES (?, ?)",
        (
            ("installation_id", canonical_uuid4(installation_id)),
            ("store_id", canonical_uuid4(store_id)),
        ),
    )
    connection.execute(_METADATA_TRIGGER_SQL)
    connection.execute(_METADATA_DELETE_TRIGGER_SQL)


def _read_store_id(connection: sqlite3.Connection, installation_id: str) -> str:
    rows = connection.execute(
        "SELECT key, value FROM dispatch_metadata ORDER BY key"
    ).fetchall()
    if len(rows) != 2 or any(
        len(row) != 2 or type(row[0]) is not str or type(row[1]) is not str for row in rows
    ):
        raise JournalError
    metadata = dict(rows)
    if set(metadata) != {"installation_id", "store_id"}:
        raise JournalError
    if canonical_uuid4(metadata["installation_id"]) != canonical_uuid4(installation_id):
        raise JournalError
    return canonical_uuid4(metadata["store_id"])


def _validate_schema(connection: sqlite3.Connection) -> None:
    table_names = connection.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    if table_names != [("dispatch_metadata",), ("dispatch_operations",)]:
        raise JournalError
    _validate_operation_schema(connection)
    metadata = connection.execute("PRAGMA table_info(dispatch_metadata)").fetchall()
    if any(len(column) < 6 for column in metadata):
        raise JournalError
    if tuple((column[1], column[2], column[3], column[5]) for column in metadata) != (
        ("key", "TEXT", 1, 1),
        ("value", "TEXT", 1, 0),
    ):
        raise JournalError
    indexes = connection.execute("PRAGMA index_list(dispatch_metadata)").fetchall()
    if len(indexes) != 1 or any(
        len(index) < 5 or index[2] != 1 or index[4] != 0 for index in indexes
    ):
        raise JournalError
    names = connection.execute(
        "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (indexes[0][1],)
    ).fetchall()
    if names != [("key",)]:
        raise JournalError
    trigger_names = connection.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'trigger' AND tbl_name = 'dispatch_metadata'
        ORDER BY name
        """
    ).fetchall()
    if trigger_names != [
        ("dispatch_metadata_no_delete",),
        ("dispatch_metadata_no_update",),
    ]:
        raise JournalError


def _validate_v1_schema(connection: sqlite3.Connection) -> None:
    table_names = connection.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    if table_names != [("dispatch_operations",)]:
        raise JournalError
    _validate_operation_schema(connection)


def _validate_operation_schema(connection: sqlite3.Connection) -> None:
    columns = connection.execute("PRAGMA table_info(dispatch_operations)").fetchall()
    if any(len(column) < 6 for column in columns):
        raise JournalError
    if tuple((column[1], column[2], column[3], column[5]) for column in columns) != (
        _TABLE_DEFINITION
    ):
        raise JournalError
    indexes = connection.execute("PRAGMA index_list(dispatch_operations)").fetchall()
    found: set[tuple[str, ...]] = set()
    for index in indexes:
        if len(index) < 5 or index[2] != 1 or index[4] != 0:
            raise JournalError
        names = connection.execute(
            "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (index[1],)
        ).fetchall()
        if any(len(name) != 1 or type(name[0]) is not str for name in names):
            raise JournalError
        found.add(tuple(name[0] for name in names))
    if found != {("operation_id",), ("idempotency_key",)}:
        raise JournalError


def _one(rows: list[tuple[object, ...]]) -> DispatchJournalRecord | None:
    if not rows:
        return None
    if len(rows) != 1:
        raise DispatchJournalConflictError
    return _decode(rows[0])


def _decode(row: tuple[object, ...]) -> DispatchJournalRecord:
    if len(row) != 5 or not all(type(value) is str for value in row):
        raise JournalError
    operation_id, idempotency_key, printer_uuid, state, record_json = row
    record = DispatchJournalRecord.model_validate_json(cast(str, record_json))
    if (
        record.request.operation_id != operation_id
        or record.request.idempotency_key != idempotency_key
        or record.request.printer_uuid != printer_uuid
        or record.state.value != state
    ):
        raise JournalError
    return record


def _row(record: DispatchJournalRecord) -> tuple[str, str, str, str, str]:
    return (
        record.request.operation_id,
        record.request.idempotency_key,
        record.request.printer_uuid,
        record.state.value,
        record.model_dump_json(),
    )
