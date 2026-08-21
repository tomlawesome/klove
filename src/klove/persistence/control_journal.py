"""Owner-only STRICT SQLite/WAL journal for typed job-control evidence."""

from __future__ import annotations

import re
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Final, cast

from pydantic import ValidationError

from klove.domain.control_journal import (
    ControlJournalEvidence,
    ControlJournalFailure,
    ControlJournalFailureCode,
    ControlJournalIdentity,
    ControlJournalRecord,
    ControlJournalState,
)
from klove.errors import JournalError
from klove.persistence.private_files import (
    PrivateFileError,
    prepare_private_file,
    require_private_file,
)

_SCHEMA_VERSION: Final = 1
_DEFAULT_CAPACITY: Final = 1_024
_MAX_CAPACITY: Final = 100_000
_METADATA_COLUMNS: Final = ("key", "value")
_METADATA_DEFINITION: Final = (("key", "TEXT", 1, 1), ("value", "TEXT", 1, 0))
_OPERATION_COLUMNS: Final = (
    "operation_id",
    "idempotency_key",
    "printer_uuid",
    "operation",
    "state_token",
    "state",
    "record_json",
)
_OPERATION_DEFINITION: Final = (
    ("operation_id", "TEXT", 1, 1),
    ("idempotency_key", "TEXT", 1, 0),
    ("printer_uuid", "TEXT", 1, 0),
    ("operation", "TEXT", 1, 0),
    ("state_token", "TEXT", 1, 0),
    ("state", "TEXT", 1, 0),
    ("record_json", "TEXT", 1, 0),
)
_METADATA_TABLE_SQL: Final = """
CREATE TABLE control_metadata (
    key TEXT PRIMARY KEY NOT NULL,
    value TEXT NOT NULL
) STRICT
"""
_OPERATION_TABLE_SQL: Final = """
CREATE TABLE control_operations (
    operation_id TEXT PRIMARY KEY NOT NULL,
    idempotency_key TEXT UNIQUE NOT NULL,
    printer_uuid TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('pause', 'resume', 'cancel')),
    state_token TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN (
            'dispatching', 'outcome_unknown', 'confirmed',
            'denied_before_dispatch', 'evidence_superseded'
        )
    ),
    record_json TEXT NOT NULL
) STRICT
"""
_METADATA_UPDATE_TRIGGER_SQL: Final = """
CREATE TRIGGER control_metadata_no_update
BEFORE UPDATE ON control_metadata
BEGIN
    SELECT RAISE(ABORT, 'control metadata is immutable');
END
"""
_METADATA_DELETE_TRIGGER_SQL: Final = """
CREATE TRIGGER control_metadata_no_delete
BEFORE DELETE ON control_metadata
BEGIN
    SELECT RAISE(ABORT, 'control metadata is immutable');
END
"""
_METADATA_TRIGGER_SQL: Final = {
    "control_metadata_no_delete": _METADATA_DELETE_TRIGGER_SQL,
    "control_metadata_no_update": _METADATA_UPDATE_TRIGGER_SQL,
}
_IDENTITY_METADATA_KEYS: Final = ("installation_id", "store_id")
_UNRESOLVED_STATES: Final = (
    ControlJournalState.DISPATCHING.value,
    ControlJournalState.OUTCOME_UNKNOWN.value,
)
_ALLOWED_TRANSITIONS: Final = {
    ControlJournalState.DISPATCHING: frozenset(
        {
            ControlJournalState.OUTCOME_UNKNOWN,
            ControlJournalState.CONFIRMED,
            ControlJournalState.DENIED_BEFORE_DISPATCH,
            ControlJournalState.EVIDENCE_SUPERSEDED,
        }
    ),
    ControlJournalState.OUTCOME_UNKNOWN: frozenset(
        {ControlJournalState.CONFIRMED, ControlJournalState.EVIDENCE_SUPERSEDED}
    ),
}


class ControlJournalConflictError(JournalError):
    """An operation or idempotency identity is bound to different evidence."""


class ControlJournalFenceError(JournalError):
    """Another unresolved control operation fences the target printer."""


class ControlJournalCapacityError(JournalError):
    """The non-evicting durable journal reached its configured capacity."""


class ControlJournalTransitionError(JournalError):
    """A stale, terminal, or otherwise unallowed transition was requested."""


class ControlJournal:
    """Persist one control reservation before its parameter-free RPC."""

    def __init__(
        self,
        path: Path,
        installation_id: str,
        *,
        capacity: int = _DEFAULT_CAPACITY,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        if type(capacity) is not int or not 1 <= capacity <= _MAX_CAPACITY:
            raise JournalError
        self._path = path
        self._installation_id = installation_id
        self._capacity = capacity
        self._uuid_factory = uuid_factory
        self._identity: ControlJournalIdentity | None = None

    @property
    def identity(self) -> ControlJournalIdentity:
        """Return the immutable installation/store identity after initialization."""
        if self._identity is None:
            raise JournalError
        return self._identity

    def initialize(self) -> None:
        """Create or validate the complete private schema and immutable metadata."""
        try:
            installation_id = _canonical_uuid(self._installation_id)
            prepare_private_file(self._path)
            connection = self._connect()
            try:
                version = _pragma_integer(connection, "user_version")
                if version == 0:
                    store_id = _new_uuid(self._uuid_factory)
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        connection.execute(_METADATA_TABLE_SQL)
                        connection.execute(_OPERATION_TABLE_SQL)
                        connection.execute(_METADATA_UPDATE_TRIGGER_SQL)
                        connection.execute(_METADATA_DELETE_TRIGGER_SQL)
                        connection.execute(
                            "INSERT INTO control_metadata (key, value) VALUES (?, ?)",
                            ("installation_id", installation_id),
                        )
                        connection.execute(
                            "INSERT INTO control_metadata (key, value) VALUES (?, ?)",
                            ("store_id", store_id),
                        )
                        connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                        connection.execute("COMMIT")
                    except sqlite3.Error:
                        connection.execute("ROLLBACK")
                        raise
                elif version != _SCHEMA_VERSION:
                    raise JournalError
                _validate_schema(connection)
                metadata = _read_identity(connection, installation_id)
                self._identity = metadata
            finally:
                connection.close()
        except JournalError:
            raise
        except (
            OSError,
            PrivateFileError,
            sqlite3.Error,
            UnicodeError,
            ValidationError,
            ValueError,
        ) as exc:
            raise JournalError from exc

    def lookup(self, operation_id: str, idempotency_key: str) -> ControlJournalRecord | None:
        """Look up an exact pair, rejecting either identity's collision."""
        operation_id = _canonical_uuid(operation_id)
        idempotency_key = _canonical_uuid(idempotency_key)
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT operation_id, idempotency_key, printer_uuid, operation,
                       state_token, state, record_json
                FROM control_operations
                WHERE operation_id = ? OR idempotency_key = ?
                """,
                (operation_id, idempotency_key),
            ).fetchall()
            record = _one(rows)
            if record is None:
                return None
            if record.operation_id != operation_id or record.idempotency_key != idempotency_key:
                raise ControlJournalConflictError
            return record
        except ControlJournalConflictError:
            raise
        except (sqlite3.Error, UnicodeError, ValidationError, ValueError) as exc:
            raise JournalError from exc
        finally:
            connection.close()

    def reserve(self, record: ControlJournalRecord) -> tuple[ControlJournalRecord, bool]:
        """Atomically reserve dispatching evidence or return its exact duplicate."""
        if record.state is not ControlJournalState.DISPATCHING:
            raise JournalError
        self._require_initialized()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = _one(
                    connection.execute(
                        """
                        SELECT operation_id, idempotency_key, printer_uuid, operation,
                               state_token, state, record_json
                        FROM control_operations
                        WHERE operation_id = ? OR idempotency_key = ?
                        """,
                        (record.operation_id, record.idempotency_key),
                    ).fetchall()
                )
                if existing is not None:
                    if existing != record:
                        raise ControlJournalConflictError
                    connection.execute("COMMIT")
                    return existing, False
                count = connection.execute("SELECT COUNT(*) FROM control_operations").fetchone()
                if count is None or len(count) != 1 or type(count[0]) is not int:
                    raise JournalError
                if count[0] >= self._capacity:
                    raise ControlJournalCapacityError
                fenced = connection.execute(
                    """
                    SELECT 1 FROM control_operations
                    WHERE printer_uuid = ? AND state IN (?, ?)
                    LIMIT 1
                    """,
                    (record.printer_uuid, *_UNRESOLVED_STATES),
                ).fetchone()
                if fenced is not None:
                    raise ControlJournalFenceError
                connection.execute(
                    """
                    INSERT INTO control_operations (
                        operation_id, idempotency_key, printer_uuid, operation,
                        state_token, state, record_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    _row(record),
                )
                connection.execute("COMMIT")
                return record, True
            except (
                ControlJournalCapacityError,
                ControlJournalConflictError,
                ControlJournalFenceError,
                JournalError,
                sqlite3.Error,
                UnicodeError,
                ValidationError,
                ValueError,
            ):
                connection.execute("ROLLBACK")
                raise
        except (
            ControlJournalCapacityError,
            ControlJournalConflictError,
            ControlJournalFenceError,
            JournalError,
        ):
            raise
        except (sqlite3.Error, UnicodeError, ValidationError, ValueError) as exc:
            raise JournalError from exc
        finally:
            connection.close()

    def unresolved(self, printer_uuid: str) -> tuple[ControlJournalRecord, ...]:
        """Return only unresolved rows for one exact printer UUID."""
        printer_uuid = _canonical_uuid(printer_uuid)
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT operation_id, idempotency_key, printer_uuid, operation,
                       state_token, state, record_json
                FROM control_operations
                WHERE printer_uuid = ? AND state IN (?, ?)
                ORDER BY operation_id
                """,
                (printer_uuid, *_UNRESOLVED_STATES),
            ).fetchall()
            return tuple(_decode(row) for row in rows)
        except (sqlite3.Error, UnicodeError, ValidationError, ValueError) as exc:
            raise JournalError from exc
        finally:
            connection.close()

    def transition(
        self,
        expected: ControlJournalRecord,
        updated: ControlJournalRecord,
    ) -> ControlJournalRecord:
        """Atomically apply one exact non-stale terminal transition."""
        try:
            updated = ControlJournalRecord.model_validate(updated.model_dump())
        except ValidationError as exc:
            raise ControlJournalTransitionError from exc
        if not _immutable_identity_matches(expected, updated):
            raise ControlJournalTransitionError
        if updated.state not in _ALLOWED_TRANSITIONS.get(expected.state, frozenset()):
            raise ControlJournalTransitionError
        self._require_initialized()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = _one(
                    connection.execute(
                        """
                        SELECT operation_id, idempotency_key, printer_uuid, operation,
                               state_token, state, record_json
                        FROM control_operations
                        WHERE operation_id = ? AND idempotency_key = ?
                        """,
                        (expected.operation_id, expected.idempotency_key),
                    ).fetchall()
                )
                if current != expected:
                    raise ControlJournalTransitionError
                cursor = connection.execute(
                    """
                    UPDATE control_operations
                    SET state = ?, record_json = ?
                    WHERE operation_id = ? AND idempotency_key = ? AND state = ?
                    """,
                    (
                        updated.state.value,
                        updated.model_dump_json(),
                        expected.operation_id,
                        expected.idempotency_key,
                        expected.state.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ControlJournalTransitionError
                connection.execute("COMMIT")
                return updated
            except (ControlJournalTransitionError, JournalError, sqlite3.Error):
                connection.execute("ROLLBACK")
                raise
        except (ControlJournalTransitionError, JournalError):
            raise
        except sqlite3.Error as exc:
            raise JournalError from exc
        finally:
            connection.close()

    def mark_outcome_unknown(
        self,
        expected: ControlJournalRecord,
        failure: ControlJournalFailure | ControlJournalFailureCode,
    ) -> ControlJournalRecord:
        """Durably retain a reservation that may have dispatched."""
        return self.transition(
            expected,
            _with_failure(expected, ControlJournalState.OUTCOME_UNKNOWN, failure),
        )

    def mark_confirmed(
        self,
        expected: ControlJournalRecord,
        evidence: ControlJournalEvidence,
    ) -> ControlJournalRecord:
        """Attach exact later postcondition evidence and close the fence."""
        updated = expected.model_copy(
            update={
                "state": ControlJournalState.CONFIRMED,
                "failure": None,
                "terminal_evidence": evidence,
            }
        )
        return self.transition(expected, updated)

    def mark_denied_before_dispatch(
        self,
        expected: ControlJournalRecord,
        failure: ControlJournalFailure | ControlJournalFailureCode,
    ) -> ControlJournalRecord:
        """Close a reservation only when dispatch was proven not to occur."""
        return self.transition(
            expected,
            _with_failure(expected, ControlJournalState.DENIED_BEFORE_DISPATCH, failure),
        )

    def mark_evidence_superseded(
        self,
        expected: ControlJournalRecord,
        evidence: ControlJournalEvidence,
    ) -> ControlJournalRecord:
        """Close old-token evidence after exact changed evidence is observed."""
        updated = expected.model_copy(
            update={
                "state": ControlJournalState.EVIDENCE_SUPERSEDED,
                "failure": None,
                "terminal_evidence": evidence,
            }
        )
        return self.transition(expected, updated)

    def _require_initialized(self) -> None:
        if self._identity is None:
            raise JournalError

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
        except (OSError, sqlite3.Error, PrivateFileError) as exc:
            if connection is not None:
                connection.close()
            raise JournalError from exc


def _canonical_uuid(value: str) -> str:
    if type(value) is not str or not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        value,
    ):
        raise JournalError
    return value


def _new_uuid(factory: Callable[[], uuid.UUID]) -> str:
    candidate = factory()
    if type(candidate) is not uuid.UUID or candidate.version != 4:
        raise JournalError
    return _canonical_uuid(str(candidate))


def _pragma_integer(connection: sqlite3.Connection, name: str) -> int:
    row = connection.execute(f"PRAGMA {name}").fetchone()
    if row is None or len(row) != 1 or type(row[0]) is not int:
        raise JournalError
    return row[0]


def _validate_schema(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise JournalError
    tables = connection.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    if tables != [("control_metadata",), ("control_operations",)]:
        raise JournalError
    _validate_table(connection, "control_metadata", _METADATA_DEFINITION, _METADATA_TABLE_SQL, 2)
    _validate_table(
        connection,
        "control_operations",
        _OPERATION_DEFINITION,
        _OPERATION_TABLE_SQL,
        7,
    )
    _validate_indexes(connection, "control_metadata", {("key",)})
    _validate_indexes(connection, "control_operations", {("operation_id",), ("idempotency_key",)})
    _validate_triggers(connection)


def _validate_table(
    connection: sqlite3.Connection,
    name: str,
    definition: tuple[tuple[str, str, int, int], ...],
    sql: str,
    column_count: int,
) -> None:
    columns = connection.execute(f"PRAGMA table_info({name})").fetchall()
    if any(len(column) < 6 for column in columns):
        raise JournalError
    if tuple((column[1], column[2], column[3], column[5]) for column in columns) != definition:
        raise JournalError
    table_rows = connection.execute(f"PRAGMA table_list('{name}')").fetchall()
    if (
        len(table_rows) != 1
        or len(table_rows[0]) < 6
        or table_rows[0][1:6] != (name, "table", column_count, 0, 1)
    ):
        raise JournalError
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    if row is None or len(row) != 1 or type(row[0]) is not str:
        raise JournalError
    if _normalise_sql(row[0]) != _normalise_sql(sql):
        raise JournalError


def _validate_indexes(
    connection: sqlite3.Connection,
    table: str,
    expected: set[tuple[str, ...]],
) -> None:
    indexes = connection.execute(f"PRAGMA index_list({table})").fetchall()
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
    if found != expected:
        raise JournalError


def _validate_triggers(connection: sqlite3.Connection) -> None:
    names = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
    ).fetchall()
    if names != [(name,) for name in sorted(_METADATA_TRIGGER_SQL)]:
        raise JournalError
    for name, expected_sql in _METADATA_TRIGGER_SQL.items():
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?", (name,)
        ).fetchone()
        if (
            row is None
            or len(row) != 1
            or type(row[0]) is not str
            or _normalise_sql(row[0]) != _normalise_sql(expected_sql)
        ):
            raise JournalError


def _read_identity(connection: sqlite3.Connection, installation_id: str) -> ControlJournalIdentity:
    rows = connection.execute(
        "SELECT key, value FROM control_metadata ORDER BY key"
    ).fetchall()
    if (
        len(rows) != 2
        or tuple(row[0] for row in rows) != _IDENTITY_METADATA_KEYS
        or any(len(row) != 2 or type(row[0]) is not str or type(row[1]) is not str for row in rows)
        or rows[0][1] != installation_id
    ):
        raise JournalError
    return ControlJournalIdentity(
        installation_id=rows[0][1],
        store_id=rows[1][1],
        schema_version=_SCHEMA_VERSION,
    )


def _one(rows: list[tuple[object, ...]]) -> ControlJournalRecord | None:
    if not rows:
        return None
    if len(rows) != 1:
        raise ControlJournalConflictError
    return _decode(rows[0])


def _decode(row: tuple[object, ...]) -> ControlJournalRecord:
    if len(row) != len(_OPERATION_COLUMNS) or not all(type(value) is str for value in row):
        raise JournalError
    operation_id, idempotency_key, printer_uuid, operation, state_token, state, record_json = row
    record = ControlJournalRecord.model_validate_json(cast(str, record_json))
    if (
        record.operation_id != operation_id
        or record.idempotency_key != idempotency_key
        or record.printer_uuid != printer_uuid
        or record.operation.value != operation
        or record.state_token != state_token
        or record.state.value != state
    ):
        raise JournalError
    return record


def _row(record: ControlJournalRecord) -> tuple[str, str, str, str, str, str, str]:
    return (
        record.operation_id,
        record.idempotency_key,
        record.printer_uuid,
        record.operation.value,
        record.state_token,
        record.state.value,
        record.model_dump_json(),
    )


def _immutable_identity_matches(
    expected: ControlJournalRecord,
    updated: ControlJournalRecord,
) -> bool:
    return (
        expected.operation_id == updated.operation_id
        and expected.idempotency_key == updated.idempotency_key
        and expected.printer_uuid == updated.printer_uuid
        and expected.operation is updated.operation
        and expected.state_token == updated.state_token
        and expected.preflight == updated.preflight
        and expected.contract_version == updated.contract_version
    )


def _with_failure(
    expected: ControlJournalRecord,
    state: ControlJournalState,
    failure: ControlJournalFailure | ControlJournalFailureCode,
) -> ControlJournalRecord:
    if isinstance(failure, ControlJournalFailureCode):
        failure = ControlJournalFailure(code=failure)
    return expected.model_copy(
        update={"state": state, "failure": failure, "terminal_evidence": None}
    )


def _normalise_sql(value: str) -> str:
    return " ".join(value.lower().split())


__all__ = [
    "ControlJournal",
    "ControlJournalCapacityError",
    "ControlJournalConflictError",
    "ControlJournalFenceError",
    "ControlJournalTransitionError",
    "_decode",
    "_pragma_integer",
    "_validate_schema",
]
