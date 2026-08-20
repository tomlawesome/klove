"""Exact SQLite schema and structural validation for the printer registry."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Final, cast

from klove.persistence.printer_registry_errors import RegistryStoreError

_SCHEMA_VERSION: Final = 1
_PRINTER_COLUMNS: Final = (
    "printer_uuid",
    "endpoint",
    "lifecycle",
    "revision",
    "moonraker_ref",
    "compatibility_ref",
    "record_json",
)
_PRINTER_DEFINITION: Final = (
    ("printer_uuid", "TEXT", 1, 1),
    ("endpoint", "TEXT", 1, 0),
    ("lifecycle", "TEXT", 1, 0),
    ("revision", "INTEGER", 1, 0),
    ("moonraker_ref", "TEXT", 0, 0),
    ("compatibility_ref", "TEXT", 0, 0),
    ("record_json", "TEXT", 1, 0),
)
_OPERATION_COLUMNS: Final = (
    "idempotency_key",
    "operation",
    "printer_uuid",
    "request_fingerprint",
    "state",
    "record_json",
)
_OPERATION_DEFINITION: Final = (
    ("idempotency_key", "TEXT", 1, 1),
    ("operation", "TEXT", 1, 0),
    ("printer_uuid", "TEXT", 1, 0),
    ("request_fingerprint", "TEXT", 1, 0),
    ("state", "TEXT", 1, 0),
    ("record_json", "TEXT", 1, 0),
)
_PRINTER_TABLE_SQL: Final = """
CREATE TABLE registry_printers (
    printer_uuid TEXT PRIMARY KEY NOT NULL,
    endpoint TEXT UNIQUE NOT NULL,
    lifecycle TEXT NOT NULL CHECK (lifecycle IN ('active', 'disabled', 'removed')),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    moonraker_ref TEXT UNIQUE,
    compatibility_ref TEXT UNIQUE,
    record_json TEXT NOT NULL,
    CHECK (
        (lifecycle = 'removed' AND moonraker_ref IS NULL AND compatibility_ref IS NULL)
        OR
        (lifecycle IN ('active', 'disabled') AND moonraker_ref IS NOT NULL
            AND compatibility_ref IS NOT NULL)
    )
) STRICT
"""
_OPERATION_TABLE_SQL: Final = """
CREATE TABLE registry_operations (
    idempotency_key TEXT PRIMARY KEY NOT NULL,
    operation TEXT NOT NULL CHECK (
        operation IN (
            'create', 'update', 'rotate_moonraker', 'rotate_compatibility',
            'disable', 'remove', 'bootstrap_import'
        )
    ),
    printer_uuid TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('preparing', 'committed', 'aborted')),
    record_json TEXT NOT NULL
) STRICT
"""
_OPERATION_INDEX_SQL: Final = (
    "CREATE INDEX registry_operations_printer_idx ON registry_operations(printer_uuid)"
)
_METADATA_COLUMNS: Final = ("key", "value")
_METADATA_DEFINITION: Final = (("key", "TEXT", 1, 1), ("value", "TEXT", 1, 0))
_METADATA_TABLE_SQL: Final = """
CREATE TABLE registry_metadata (
    key TEXT PRIMARY KEY NOT NULL,
    value TEXT NOT NULL
) STRICT
"""


@dataclass(frozen=True, slots=True)
class _TableSchema:
    name: str
    columns: tuple[str, ...]
    definition: tuple[tuple[str, str, int, int], ...]
    sql: str
    indexes: frozenset[tuple[tuple[str, ...], bool]]


_METADATA_SCHEMA: Final = _TableSchema(
    "registry_metadata",
    _METADATA_COLUMNS,
    _METADATA_DEFINITION,
    _METADATA_TABLE_SQL,
    frozenset({(("key",), True)}),
)
_PRINTER_SCHEMA: Final = _TableSchema(
    "registry_printers",
    _PRINTER_COLUMNS,
    _PRINTER_DEFINITION,
    _PRINTER_TABLE_SQL,
    frozenset(
        {
            (("printer_uuid",), True),
            (("endpoint",), True),
            (("moonraker_ref",), True),
            (("compatibility_ref",), True),
        }
    ),
)
_OPERATION_SCHEMA: Final = _TableSchema(
    "registry_operations",
    _OPERATION_COLUMNS,
    _OPERATION_DEFINITION,
    _OPERATION_TABLE_SQL,
    frozenset({(("idempotency_key",), True), (("printer_uuid",), False)}),
)


def _pragma_integer(connection: sqlite3.Connection, name: str) -> int:
    row = connection.execute(f"PRAGMA {name}").fetchone()
    if row is None or len(row) != 1 or type(row[0]) is not int:
        raise RegistryStoreError
    return int(row[0])


def _validate_metadata(connection: sqlite3.Connection, key_identity: str) -> None:
    rows = connection.execute("SELECT key, value FROM registry_metadata ORDER BY key").fetchall()
    if rows != [("request_hmac_key_sha256", key_identity)]:
        raise RegistryStoreError


def _validate_schema(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise RegistryStoreError
    table_names = connection.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    if table_names != [("registry_metadata",), ("registry_operations",), ("registry_printers",)]:
        raise RegistryStoreError
    _validate_table(connection, _METADATA_SCHEMA)
    _validate_table(connection, _PRINTER_SCHEMA)
    _validate_table(connection, _OPERATION_SCHEMA)


def _validate_table(connection: sqlite3.Connection, schema: _TableSchema) -> None:
    rows = connection.execute(f"PRAGMA table_info({schema.name})").fetchall()
    if (
        any(len(row) < 6 for row in rows)
        or tuple((row[1], row[2], row[3], row[5]) for row in rows) != schema.definition
    ):
        raise RegistryStoreError
    table_rows = connection.execute(f"PRAGMA table_list('{schema.name}')").fetchall()
    if (
        len(table_rows) != 1
        or len(table_rows[0]) < 6
        or table_rows[0][1:6] != (schema.name, "table", len(schema.columns), 0, 1)
    ):
        raise RegistryStoreError
    sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (schema.name,)
    ).fetchone()
    if (
        sql_row is None
        or len(sql_row) != 1
        or type(sql_row[0]) is not str
        or _normalize_sql(sql_row[0]) != _normalize_sql(schema.sql)
    ):
        raise RegistryStoreError
    indexes: set[tuple[tuple[str, ...], bool]] = set()
    for row in connection.execute(f"PRAGMA index_list({schema.name})").fetchall():
        if len(row) < 5 or row[4] != 0 or type(row[1]) is not str or row[2] not in {0, 1}:
            raise RegistryStoreError
        info = connection.execute(
            "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (row[1],)
        ).fetchall()
        if not info or any(len(column) != 1 or type(column[0]) is not str for column in info):
            raise RegistryStoreError
        indexes.add((tuple(cast(str, column[0]) for column in info), bool(row[2])))
    if indexes != schema.indexes:
        raise RegistryStoreError


def _normalize_sql(value: str) -> str:
    return " ".join(value.split()).casefold()
