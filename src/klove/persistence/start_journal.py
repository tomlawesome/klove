"""Crash-safe SQLite/WAL journal for at-most-once print start."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path
from typing import Final, cast

from pydantic import ValidationError

from klove.domain.start import (
    StartConfirmation,
    StartFailure,
    StartJournalRecord,
    StartJournalState,
)
from klove.domain.upload import VerifiedUpload
from klove.errors import JournalError

_SCHEMA_VERSION: Final = 1
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
_UNIQUE_INDEXES: Final = {
    ("operation_id",),
    ("idempotency_key",),
}


class JournalConflictError(JournalError):
    """An operation id or idempotency key is already bound to other evidence."""


class JournalFenceError(JournalError):
    """Another unresolved operation already fences the target printer."""


class StartJournal:
    """Persist the dispatch reservation before any start request can be sent."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def initialize(self) -> None:
        """Create or validate the private journal and its exact schema."""
        _prepare_private_file(self._path)
        connection = self._connect()
        try:
            version = _pragma_integer(connection, "user_version")
            if version == 0:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS start_operations (
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
                    connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                    connection.execute("COMMIT")
                except sqlite3.Error:
                    connection.execute("ROLLBACK")
                    raise
            elif version != _SCHEMA_VERSION:
                raise JournalError
            _validate_schema(connection)
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise JournalError from exc
        finally:
            connection.close()

    def lookup(self, verified: VerifiedUpload) -> StartJournalRecord | None:
        """Return an exact duplicate, reject a key collision, or report absence."""
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT operation_id, idempotency_key, printer_uuid, state, record_json
                FROM start_operations
                WHERE operation_id = ? OR idempotency_key = ?
                """,
                (
                    verified.qualification.operation_id,
                    verified.qualification.idempotency_key,
                ),
            ).fetchall()
            return _matching_record(rows, verified)
        except (sqlite3.Error, UnicodeError, ValidationError, ValueError) as exc:
            raise JournalError from exc
        finally:
            connection.close()

    def reserve(self, record: StartJournalRecord) -> tuple[StartJournalRecord, bool]:
        """Atomically insert `dispatching`, or return the exact concurrent record."""
        if record.state is not StartJournalState.DISPATCHING:
            raise JournalError
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    """
                    SELECT operation_id, idempotency_key, printer_uuid, state, record_json
                    FROM start_operations
                    WHERE operation_id = ? OR idempotency_key = ?
                    """,
                    (record.operation_id, record.idempotency_key),
                ).fetchall()
                existing = _matching_record(rows, record.verified)
                if existing is not None:
                    connection.execute("COMMIT")
                    return existing, False
                fenced = connection.execute(
                    """
                    SELECT 1
                    FROM start_operations
                    WHERE printer_uuid = ?
                      AND state IN ('dispatching', 'outcome_unknown')
                    LIMIT 1
                    """,
                    (record.printer_uuid,),
                ).fetchone()
                if fenced is not None:
                    raise JournalFenceError
                connection.execute(
                    """
                    INSERT INTO start_operations (
                        operation_id, idempotency_key, printer_uuid, state, record_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    _record_row(record),
                )
                connection.execute("COMMIT")
                return record, True
            except (
                JournalError,
                sqlite3.Error,
                UnicodeError,
                ValidationError,
                ValueError,
            ):
                connection.execute("ROLLBACK")
                raise
        except JournalError:
            raise
        except (sqlite3.Error, UnicodeError, ValidationError, ValueError) as exc:
            raise JournalError from exc
        finally:
            connection.close()

    def unresolved(self, printer_uuid: str) -> tuple[StartJournalRecord, ...]:
        """Return every operation whose start outcome still requires read-only proof."""
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT operation_id, idempotency_key, printer_uuid, state, record_json
                FROM start_operations
                WHERE printer_uuid = ? AND state IN ('dispatching', 'outcome_unknown')
                ORDER BY operation_id
                """,
                (printer_uuid,),
            ).fetchall()
            return tuple(_decode_row(row) for row in rows)
        except (sqlite3.Error, UnicodeError, ValidationError, ValueError) as exc:
            raise JournalError from exc
        finally:
            connection.close()

    def mark_unknown(
        self,
        record: StartJournalRecord,
        failure: StartFailure,
    ) -> StartJournalRecord:
        """Durably retain an operation that may have dispatched."""
        changed = record.model_copy(
            update={
                "state": StartJournalState.OUTCOME_UNKNOWN,
                "failure": failure,
                "confirmation": None,
            }
        )
        self._replace(changed)
        return changed

    def mark_confirmed(
        self,
        record: StartJournalRecord,
        confirmation: StartConfirmation,
    ) -> StartJournalRecord:
        """Durably attach later exact history evidence to a start operation."""
        changed = record.model_copy(
            update={
                "state": StartJournalState.CONFIRMED,
                "failure": None,
                "confirmation": confirmation,
            }
        )
        self._replace(changed)
        return changed

    def _replace(self, record: StartJournalRecord) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    UPDATE start_operations
                    SET state = ?, record_json = ?
                    WHERE operation_id = ? AND idempotency_key = ?
                      AND state IN ('dispatching', 'outcome_unknown')
                    """,
                    (
                        record.state.value,
                        record.model_dump_json(),
                        record.operation_id,
                        record.idempotency_key,
                    ),
                )
                if cursor.rowcount != 1:
                    raise JournalError
                connection.execute("COMMIT")
            except (sqlite3.Error, JournalError):
                connection.execute("ROLLBACK")
                raise
        except JournalError:
            raise
        except sqlite3.Error as exc:
            raise JournalError from exc
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            _verify_private_file(self._path)
            connection = sqlite3.connect(self._path, timeout=5, isolation_level=None)
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            if mode != ("wal",):
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


def _prepare_private_file(path: Path) -> None:
    parent = path.parent
    try:
        parent_metadata = parent.lstat()
        if not stat.S_ISDIR(parent_metadata.st_mode) or stat.S_ISLNK(parent_metadata.st_mode):
            raise JournalError
        if os.name != "nt" and (
            parent_metadata.st_uid != os.geteuid() or stat.S_IMODE(parent_metadata.st_mode) & 0o077
        ):
            raise JournalError
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        _verify_private_file(path)
    except (OSError, ValueError) as exc:
        raise JournalError from exc


def _verify_private_file(path: Path) -> None:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise JournalError
    if os.name != "nt" and (
        metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise JournalError


def _pragma_integer(connection: sqlite3.Connection, name: str) -> int:
    row = connection.execute(f"PRAGMA {name}").fetchone()
    if row is None or len(row) != 1 or isinstance(row[0], bool) or not isinstance(row[0], int):
        raise JournalError
    return int(row[0])


def _validate_schema(connection: sqlite3.Connection) -> None:
    rows = connection.execute("PRAGMA table_info(start_operations)").fetchall()
    if any(len(row) < 6 for row in rows):
        raise JournalError
    definition = tuple((row[1], row[2], row[3], row[5]) for row in rows)
    if definition != _TABLE_DEFINITION:
        raise JournalError
    table_rows = connection.execute("PRAGMA table_list('start_operations')").fetchall()
    if (
        len(table_rows) != 1
        or len(table_rows[0]) < 6
        or table_rows[0][1:6] != ("start_operations", "table", len(_TABLE_COLUMNS), 0, 1)
    ):
        raise JournalError
    index_rows = connection.execute("PRAGMA index_list(start_operations)").fetchall()
    if any(len(row) < 5 or row[2] != 1 or row[4] != 0 for row in index_rows):
        raise JournalError
    unique_indexes: set[tuple[object, ...]] = set()
    for row in index_rows:
        name = cast(str, row[1])
        columns = connection.execute(
            "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
            (name,),
        ).fetchall()
        if any(len(column) != 1 or not isinstance(column[0], str) for column in columns):
            raise JournalError
        unique_indexes.add(tuple(column[0] for column in columns))
    if unique_indexes != _UNIQUE_INDEXES:
        raise JournalError


def _matching_record(
    rows: list[tuple[object, ...]],
    verified: VerifiedUpload,
) -> StartJournalRecord | None:
    if not rows:
        return None
    if len(rows) != 1:
        raise JournalConflictError
    record = _decode_row(rows[0])
    qualification = verified.qualification
    if (
        record.operation_id != qualification.operation_id
        or record.idempotency_key != qualification.idempotency_key
        or record.verified != verified
    ):
        raise JournalConflictError
    return record


def _decode_row(row: tuple[object, ...]) -> StartJournalRecord:
    if len(row) != len(_TABLE_COLUMNS) or not all(type(value) is str for value in row):
        raise JournalError
    operation_id, idempotency_key, printer_uuid, state, record_json = row
    record = StartJournalRecord.model_validate_json(cast(str, record_json))
    if (
        record.operation_id != operation_id
        or record.idempotency_key != idempotency_key
        or record.printer_uuid != printer_uuid
        or record.state.value != state
    ):
        raise JournalError
    return record


def _record_row(record: StartJournalRecord) -> tuple[str, str, str, str, str]:
    return (
        record.operation_id,
        record.idempotency_key,
        record.printer_uuid,
        record.state.value,
        record.model_dump_json(),
    )
