"""Private SQLite/WAL journal for at-most-once Grove MQTT controls."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Final, cast

from pydantic import ValidationError

from klove.domain.control import ControlResult
from klove.domain.mqtt_ingress import (
    MqttIngressRecord,
    MqttIngressState,
    completed_ingress,
    unknown_ingress,
)
from klove.errors import JournalError
from klove.persistence.private_files import (
    PrivateFileError,
    prepare_private_file,
    require_private_file,
)

_SCHEMA_VERSION: Final = 1


class MqttIngressConflictError(JournalError):
    """A sequence or generated idempotency identity is already bound differently."""


class MqttIngressCapacityError(JournalError):
    """The non-evicting durable ingress journal reached its configured bound."""


class MqttIngressJournal:
    """Reserve before control and recover every interrupted reservation as unknown."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def initialize(self) -> None:
        """Create or validate exact private storage, then fail closed on crash gaps."""
        try:
            prepare_private_file(self._path)
            connection = self._connect()
            try:
                version = _pragma_integer(connection, "user_version")
                if version == 0:
                    self._create_schema(connection)
                elif version != _SCHEMA_VERSION:
                    raise JournalError
                _validate_schema(connection)
                for row in connection.execute(
                    """
                    SELECT printer_uuid, sequence_id, idempotency_key, state, record_json
                    FROM mqtt_ingress
                    """
                ).fetchall():
                    _decode_record(row)
                self._recover_reserved(connection)
            finally:
                connection.close()
        except JournalError:
            raise
        except (OSError, PrivateFileError, sqlite3.Error, ValueError) as exc:
            raise JournalError from exc

    def lookup(self, printer_uuid: str, sequence_id: str) -> MqttIngressRecord | None:
        """Return one exact secret-free ingress result or absence."""
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT printer_uuid, sequence_id, idempotency_key, state, record_json
                FROM mqtt_ingress
                WHERE printer_uuid = ? AND sequence_id = ?
                """,
                (printer_uuid, sequence_id),
            ).fetchone()
            return None if row is None else _decode_record(row)
        except (sqlite3.Error, UnicodeError, ValidationError, ValueError) as exc:
            raise JournalError from exc
        finally:
            connection.close()

    def reserve(
        self,
        record: MqttIngressRecord,
        *,
        capacity: int,
    ) -> tuple[MqttIngressRecord, bool]:
        """Commit one pre-dispatch reservation or return its exact duplicate."""
        if record.state is not MqttIngressState.RESERVED or capacity < 1:
            raise JournalError
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    """
                    SELECT printer_uuid, sequence_id, idempotency_key, state, record_json
                    FROM mqtt_ingress
                    WHERE (printer_uuid = ? AND sequence_id = ?) OR idempotency_key = ?
                    """,
                    (record.printer_uuid, record.sequence_id, record.idempotency_key),
                ).fetchall()
                if rows:
                    if len(rows) != 1:
                        raise MqttIngressConflictError
                    existing = _decode_record(rows[0])
                    if not _same_request(existing, record):
                        raise MqttIngressConflictError
                    connection.execute("COMMIT")
                    return existing, False
                count = connection.execute("SELECT COUNT(*) FROM mqtt_ingress").fetchone()
                if count is None or len(count) != 1 or type(count[0]) is not int:
                    raise JournalError
                if count[0] >= capacity:
                    raise MqttIngressCapacityError
                connection.execute(
                    """
                    INSERT INTO mqtt_ingress (
                        printer_uuid, sequence_id, idempotency_key, state, record_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        record.printer_uuid,
                        record.sequence_id,
                        record.idempotency_key,
                        record.state.value,
                        _encode_record(record),
                    ),
                )
                connection.execute("COMMIT")
                return record, True
            except Exception:
                connection.execute("ROLLBACK")
                raise
        except (MqttIngressCapacityError, MqttIngressConflictError):
            raise
        except (sqlite3.Error, UnicodeError, ValidationError, ValueError) as exc:
            raise JournalError from exc
        finally:
            connection.close()

    def complete(
        self,
        reserved: MqttIngressRecord,
        result: ControlResult,
    ) -> MqttIngressRecord:
        """Atomically bind one terminal control result to its exact reservation."""
        terminal = completed_ingress(reserved, result)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                changed = connection.execute(
                    """
                    UPDATE mqtt_ingress SET state = ?, record_json = ?
                    WHERE printer_uuid = ? AND sequence_id = ?
                      AND idempotency_key = ? AND state = ? AND record_json = ?
                    """,
                    (
                        terminal.state.value,
                        _encode_record(terminal),
                        reserved.printer_uuid,
                        reserved.sequence_id,
                        reserved.idempotency_key,
                        MqttIngressState.RESERVED.value,
                        _encode_record(reserved),
                    ),
                ).rowcount
                if changed != 1:
                    raise MqttIngressConflictError
                connection.execute("COMMIT")
                return terminal
            except Exception:
                connection.execute("ROLLBACK")
                raise
        except MqttIngressConflictError:
            raise
        except (sqlite3.Error, UnicodeError, ValidationError, ValueError) as exc:
            raise JournalError from exc
        finally:
            connection.close()

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                CREATE TABLE mqtt_ingress (
                    printer_uuid TEXT NOT NULL,
                    sequence_id TEXT NOT NULL,
                    idempotency_key TEXT UNIQUE NOT NULL,
                    state TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    PRIMARY KEY (printer_uuid, sequence_id)
                ) STRICT
                """
            )
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            connection.execute("COMMIT")
        except sqlite3.Error:
            connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _recover_reserved(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            rows = connection.execute(
                """
                SELECT printer_uuid, sequence_id, idempotency_key, state, record_json
                FROM mqtt_ingress WHERE state = ?
                """,
                (MqttIngressState.RESERVED.value,),
            ).fetchall()
            for row in rows:
                record = _decode_record(row)
                recovered = unknown_ingress(record)
                changed = connection.execute(
                    """
                    UPDATE mqtt_ingress SET state = ?, record_json = ?
                    WHERE printer_uuid = ? AND sequence_id = ? AND record_json = ?
                    """,
                    (
                        recovered.state.value,
                        _encode_record(recovered),
                        record.printer_uuid,
                        record.sequence_id,
                        cast(str, row[4]),
                    ),
                ).rowcount
                if changed != 1:
                    raise JournalError
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def _connect(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            require_private_file(self._path)
            connection = sqlite3.connect(self._path, isolation_level=None, timeout=5)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            if mode != ("wal",):
                raise JournalError
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA busy_timeout = 5000")
            return connection
        except JournalError:
            if connection is not None:
                connection.close()
            raise
        except (OSError, PrivateFileError, sqlite3.Error, ValueError) as exc:
            if connection is not None:
                connection.close()
            raise JournalError from exc


def _same_request(left: MqttIngressRecord, right: MqttIngressRecord) -> bool:
    return (
        left.printer_uuid == right.printer_uuid
        and left.printer_record_revision == right.printer_record_revision
        and left.sequence_id == right.sequence_id
        and left.operation is right.operation
        and left.payload_hash == right.payload_hash
        and left.state_token == right.state_token
    )


def _encode_record(record: MqttIngressRecord) -> str:
    return json.dumps(
        record.model_dump(mode="json"), ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )


def _decode_record(row: tuple[object, ...]) -> MqttIngressRecord:
    if len(row) != 5 or not all(type(value) is str for value in row):
        raise JournalError
    printer_uuid, sequence_id, idempotency_key, state, record_json = cast(
        tuple[str, str, str, str, str], row
    )
    record = MqttIngressRecord.model_validate_json(record_json)
    if (
        record.printer_uuid != printer_uuid
        or record.sequence_id != sequence_id
        or record.idempotency_key != idempotency_key
        or record.state.value != state
    ):
        raise JournalError
    return record


def _pragma_integer(connection: sqlite3.Connection, name: str) -> int:
    row = connection.execute(f"PRAGMA {name}").fetchone()
    if row is None or len(row) != 1 or type(row[0]) is not int:
        raise JournalError
    return row[0]


def _validate_schema(connection: sqlite3.Connection) -> None:
    if _pragma_integer(connection, "user_version") != _SCHEMA_VERSION:
        raise JournalError
    rows = connection.execute("PRAGMA table_info(mqtt_ingress)").fetchall()
    if any(len(row) < 6 for row in rows):
        raise JournalError
    actual = tuple((row[1], row[2], row[3], row[5]) for row in rows)
    if actual != (
        ("printer_uuid", "TEXT", 1, 1),
        ("sequence_id", "TEXT", 1, 2),
        ("idempotency_key", "TEXT", 1, 0),
        ("state", "TEXT", 1, 0),
        ("record_json", "TEXT", 1, 0),
    ):
        raise JournalError
    table_rows = connection.execute("PRAGMA table_list('mqtt_ingress')").fetchall()
    if (
        len(table_rows) != 1
        or len(table_rows[0]) < 6
        or table_rows[0][1:6] != ("mqtt_ingress", "table", 5, 0, 1)
    ):
        raise JournalError
    indexes = connection.execute("PRAGMA index_list(mqtt_ingress)").fetchall()
    if any(len(row) < 5 or row[2] != 1 or row[4] != 0 for row in indexes):
        raise JournalError
    unique_columns: set[tuple[str, ...]] = set()
    for index in indexes:
        name = cast(str, index[1])
        columns = connection.execute(
            "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (name,)
        ).fetchall()
        if any(len(column) != 1 or type(column[0]) is not str for column in columns):
            raise JournalError
        unique_columns.add(tuple(cast(str, column[0]) for column in columns))
    if unique_columns != {("printer_uuid", "sequence_id"), ("idempotency_key",)}:
        raise JournalError
