"""Exact SQLite schema and structural validation for the printer registry."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Final, cast
from uuid import UUID, uuid4

from klove.persistence.printer_registry_errors import RegistryStoreError
from klove.persistence.printer_registry_fences import (
    FENCE_TABLE_SQL,
    FENCE_TRIGGER_SQL,
    initialize_fence_catalogue,
)

_SCHEMA_VERSION: Final = 3
_SCHEMA_VERSION_V1: Final = 1
_SCHEMA_VERSION_V2: Final = 2
_SCHEMA_VERSION_V3: Final = 3
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
_MAPPING_HISTORY_COLUMNS: Final = (
    "printer_uuid",
    "registry_revision",
    "observed_at_unix_ms",
    "mapping_fingerprint",
    "mapping_json",
)
_MAPPING_HISTORY_DEFINITION: Final = (
    ("printer_uuid", "TEXT", 1, 1),
    ("registry_revision", "INTEGER", 1, 2),
    ("observed_at_unix_ms", "INTEGER", 1, 0),
    ("mapping_fingerprint", "TEXT", 1, 0),
    ("mapping_json", "TEXT", 1, 0),
)
_MAPPING_HISTORY_TABLE_SQL: Final = """
CREATE TABLE registry_mapping_history (
    printer_uuid TEXT NOT NULL,
    registry_revision INTEGER NOT NULL CHECK (registry_revision >= 1),
    observed_at_unix_ms INTEGER NOT NULL CHECK (observed_at_unix_ms >= 0),
    mapping_fingerprint TEXT NOT NULL CHECK (
        length(mapping_fingerprint) = 64
        AND mapping_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    mapping_json TEXT NOT NULL,
    PRIMARY KEY (printer_uuid, registry_revision)
) STRICT
"""
_PROFILE_HISTORY_COLUMNS: Final = (
    "printer_uuid",
    "registry_revision",
    "slicer_profile_id",
    "generation",
    "profile_fingerprint",
    "event",
    "profile_json",
)
_PROFILE_HISTORY_DEFINITION: Final = (
    ("printer_uuid", "TEXT", 1, 1),
    ("registry_revision", "INTEGER", 1, 2),
    ("slicer_profile_id", "TEXT", 1, 3),
    ("generation", "INTEGER", 1, 0),
    ("profile_fingerprint", "TEXT", 1, 0),
    ("event", "TEXT", 1, 4),
    ("profile_json", "TEXT", 1, 0),
)
_PROFILE_HISTORY_TABLE_SQL: Final = """
CREATE TABLE registry_profile_history (
    printer_uuid TEXT NOT NULL,
    registry_revision INTEGER NOT NULL CHECK (registry_revision >= 1),
    slicer_profile_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    profile_fingerprint TEXT NOT NULL CHECK (
        length(profile_fingerprint) = 64
        AND profile_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    event TEXT NOT NULL CHECK (event IN ('baseline', 'bound', 'retired')),
    profile_json TEXT NOT NULL,
    PRIMARY KEY (printer_uuid, registry_revision, slicer_profile_id, event)
) STRICT
"""
_MAPPING_HISTORY_UPDATE_TRIGGER_SQL: Final = """
CREATE TRIGGER registry_mapping_history_no_update
BEFORE UPDATE ON registry_mapping_history
BEGIN
    SELECT RAISE(ABORT, 'registry mapping history is immutable');
END
"""
_MAPPING_HISTORY_DELETE_TRIGGER_SQL: Final = """
CREATE TRIGGER registry_mapping_history_no_delete
BEFORE DELETE ON registry_mapping_history
BEGIN
    SELECT RAISE(ABORT, 'registry mapping history is immutable');
END
"""
_PROFILE_HISTORY_UPDATE_TRIGGER_SQL: Final = """
CREATE TRIGGER registry_profile_history_no_update
BEFORE UPDATE ON registry_profile_history
BEGIN
    SELECT RAISE(ABORT, 'registry profile history is immutable');
END
"""
_PROFILE_HISTORY_DELETE_TRIGGER_SQL: Final = """
CREATE TRIGGER registry_profile_history_no_delete
BEFORE DELETE ON registry_profile_history
BEGIN
    SELECT RAISE(ABORT, 'registry profile history is immutable');
END
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
_MAPPING_HISTORY_SCHEMA: Final = _TableSchema(
    "registry_mapping_history",
    _MAPPING_HISTORY_COLUMNS,
    _MAPPING_HISTORY_DEFINITION,
    _MAPPING_HISTORY_TABLE_SQL,
    frozenset({(("printer_uuid", "registry_revision"), True)}),
)
_PROFILE_HISTORY_SCHEMA: Final = _TableSchema(
    "registry_profile_history",
    _PROFILE_HISTORY_COLUMNS,
    _PROFILE_HISTORY_DEFINITION,
    _PROFILE_HISTORY_TABLE_SQL,
    frozenset({(("printer_uuid", "registry_revision", "slicer_profile_id", "event"), True)}),
)
_V2_TRIGGER_SQL: Final = {
    "registry_mapping_history_no_update": _MAPPING_HISTORY_UPDATE_TRIGGER_SQL,
    "registry_mapping_history_no_delete": _MAPPING_HISTORY_DELETE_TRIGGER_SQL,
    "registry_profile_history_no_update": _PROFILE_HISTORY_UPDATE_TRIGGER_SQL,
    "registry_profile_history_no_delete": _PROFILE_HISTORY_DELETE_TRIGGER_SQL,
}
_V3_TRIGGER_SQL: Final = {
    **_V2_TRIGGER_SQL,
    **FENCE_TRIGGER_SQL,
}


def _initialize_schema_v2(connection: sqlite3.Connection, key_identity: str) -> None:
    """Create the complete empty v2 catalog inside the caller's transaction."""
    connection.execute(_PRINTER_TABLE_SQL)
    connection.execute(_OPERATION_TABLE_SQL)
    connection.execute(_OPERATION_INDEX_SQL)
    connection.execute(_METADATA_TABLE_SQL)
    connection.execute(
        "INSERT INTO registry_metadata (key, value) VALUES (?, ?)",
        ("request_hmac_key_sha256", key_identity),
    )
    connection.execute(_MAPPING_HISTORY_TABLE_SQL)
    connection.execute(_PROFILE_HISTORY_TABLE_SQL)
    connection.execute(_MAPPING_HISTORY_UPDATE_TRIGGER_SQL)
    connection.execute(_MAPPING_HISTORY_DELETE_TRIGGER_SQL)
    connection.execute(_PROFILE_HISTORY_UPDATE_TRIGGER_SQL)
    connection.execute(_PROFILE_HISTORY_DELETE_TRIGGER_SQL)


def _initialize_schema_v3(
    connection: sqlite3.Connection,
    key_identity: str,
    *,
    installation_uuid: str | None = None,
    store_uuid: str | None = None,
) -> None:
    """Create the complete empty v3 catalogue inside the caller's transaction."""
    _initialize_schema_v2(connection, key_identity)
    connection.execute(
        "INSERT INTO registry_metadata (key, value) VALUES (?, ?)",
        ("installation_uuid", installation_uuid or str(uuid4())),
    )
    connection.execute(
        "INSERT INTO registry_metadata (key, value) VALUES (?, ?)",
        ("store_uuid", store_uuid or str(uuid4())),
    )
    initialize_fence_catalogue(connection)


def _pragma_integer(connection: sqlite3.Connection, name: str) -> int:
    row = connection.execute(f"PRAGMA {name}").fetchone()
    if row is None or len(row) != 1 or type(row[0]) is not int:
        raise RegistryStoreError
    return int(row[0])


def _validate_metadata(connection: sqlite3.Connection, key_identity: str) -> None:
    rows = connection.execute("SELECT key, value FROM registry_metadata ORDER BY key").fetchall()
    if rows != [("request_hmac_key_sha256", key_identity)]:
        raise RegistryStoreError


def _validate_metadata_v3(
    connection: sqlite3.Connection,
    key_identity: str,
) -> tuple[str, str]:
    """Validate the exact secret-free identity of this registry installation."""
    rows = connection.execute("SELECT key, value FROM registry_metadata ORDER BY key").fetchall()
    if len(rows) != 3:
        raise RegistryStoreError
    values = dict(rows)
    if set(values) != {"installation_uuid", "request_hmac_key_sha256", "store_uuid"}:
        raise RegistryStoreError
    if values["request_hmac_key_sha256"] != key_identity:
        raise RegistryStoreError
    for key in ("installation_uuid", "store_uuid"):
        value = values[key]
        if (
            type(value) is not str
            or len(value) != 36
            or value != value.lower()
            or value.count("-") != 4
        ):
            raise RegistryStoreError
        try:
            parsed = UUID(value)
        except (AttributeError, ValueError) as exc:
            raise RegistryStoreError from exc
        if parsed.version != 4 or str(parsed) != value:
            raise RegistryStoreError
    if values["installation_uuid"] == values["store_uuid"]:
        raise RegistryStoreError
    return values["installation_uuid"], values["store_uuid"]


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


def _validate_schema_v2_structure(connection: sqlite3.Connection) -> None:
    """Validate the exact v2 catalog without interpreting its row payloads."""
    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise RegistryStoreError
    table_names = connection.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    if table_names != [
        ("registry_mapping_history",),
        ("registry_metadata",),
        ("registry_operations",),
        ("registry_printers",),
        ("registry_profile_history",),
    ]:
        raise RegistryStoreError
    _validate_table(connection, _METADATA_SCHEMA)
    _validate_table(connection, _PRINTER_SCHEMA)
    _validate_table(connection, _OPERATION_SCHEMA)
    _validate_table(connection, _MAPPING_HISTORY_SCHEMA)
    _validate_table(connection, _PROFILE_HISTORY_SCHEMA)
    trigger_names = connection.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'trigger'
        ORDER BY name
        """
    ).fetchall()
    if trigger_names != [(name,) for name in sorted(_V2_TRIGGER_SQL)]:
        raise RegistryStoreError
    for name, expected_sql in _V2_TRIGGER_SQL.items():
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?", (name,)
        ).fetchone()
        if (
            row is None
            or len(row) != 1
            or type(row[0]) is not str
            or _normalize_sql(row[0]) != _normalize_sql(expected_sql)
        ):
            raise RegistryStoreError


def _validate_schema_v3_structure(  # noqa: PLR0912
    connection: sqlite3.Connection,
) -> None:
    """Validate the exact v3 tables, indexes, triggers, and strict metadata shape."""
    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise RegistryStoreError
    table_names = connection.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    if table_names != [
        ("registry_fence_references",),
        ("registry_fence_stores",),
        ("registry_fence_transitions",),
        ("registry_mapping_history",),
        ("registry_metadata",),
        ("registry_operations",),
        ("registry_printers",),
        ("registry_profile_history",),
    ]:
        raise RegistryStoreError
    _validate_table(connection, _METADATA_SCHEMA)
    _validate_table(connection, _PRINTER_SCHEMA)
    _validate_table(connection, _OPERATION_SCHEMA)
    _validate_table(connection, _MAPPING_HISTORY_SCHEMA)
    _validate_table(connection, _PROFILE_HISTORY_SCHEMA)
    fence_definitions = {
        "registry_fence_stores": (
            ("owner_store", "TEXT", 1, 1),
            ("installation_uuid", "TEXT", 1, 0),
            ("store_id", "TEXT", 1, 0),
            ("schema_version", "INTEGER", 1, 0),
        ),
        "registry_fence_references": (
            ("fence_reference_id", "TEXT", 1, 1),
            ("printer_uuid", "TEXT", 1, 0),
            ("fence_kind", "TEXT", 1, 0),
            ("owner_store", "TEXT", 1, 0),
            ("store_id", "TEXT", 1, 0),
            ("owner_schema_version", "INTEGER", 1, 0),
            ("operation_id", "TEXT", 1, 0),
            ("state", "TEXT", 1, 0),
            ("created_at_unix_ms", "INTEGER", 1, 0),
            ("transitioned_at_unix_ms", "INTEGER", 1, 0),
            ("resolution_code", "TEXT", 0, 0),
        ),
        "registry_fence_transitions": (
            ("fence_reference_id", "TEXT", 1, 1),
            ("sequence", "INTEGER", 1, 2),
            ("from_state", "TEXT", 0, 0),
            ("to_state", "TEXT", 1, 0),
            ("transitioned_at_unix_ms", "INTEGER", 1, 0),
            ("resolution_code", "TEXT", 0, 0),
        ),
    }
    fence_indexes = {
        "registry_fence_stores": frozenset(
            {
                (("owner_store",), True),
                (("store_id",), True),
                (("installation_uuid", "owner_store"), False),
            }
        ),
        "registry_fence_references": frozenset(
            {
                (("fence_reference_id",), True),
                (
                    (
                        "owner_store",
                        "store_id",
                        "owner_schema_version",
                        "operation_id",
                        "fence_kind",
                    ),
                    True,
                ),
                (("printer_uuid", "state", "fence_reference_id"), False),
            }
        ),
        "registry_fence_transitions": frozenset({(("fence_reference_id", "sequence"), True)}),
    }
    for name, definition in fence_definitions.items():
        rows = connection.execute(f"PRAGMA table_info({name})").fetchall()
        if (
            any(len(row) < 6 for row in rows)
            or tuple((row[1], row[2], row[3], row[5]) for row in rows) != definition
        ):
            raise RegistryStoreError
        table_rows = connection.execute(f"PRAGMA table_list('{name}')").fetchall()
        if (
            len(table_rows) != 1
            or len(table_rows[0]) < 6
            or table_rows[0][1:6] != (name, "table", len(definition), 0, 1)
        ):
            raise RegistryStoreError
        sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        expected_sql = {
            **dict(
                zip(
                    (
                        "registry_fence_stores",
                        "registry_fence_references",
                        "registry_fence_transitions",
                    ),
                    FENCE_TABLE_SQL,
                    strict=True,
                )
            ),
        }[name]
        if (
            sql_row is None
            or type(sql_row[0]) is not str
            or _normalize_sql(sql_row[0]) != _normalize_sql(expected_sql)
        ):
            raise RegistryStoreError
        indexes: set[tuple[tuple[str, ...], bool]] = set()
        for row in connection.execute(f"PRAGMA index_list({name})").fetchall():
            if len(row) < 5 or row[4] != 0 or type(row[1]) is not str or row[2] not in {0, 1}:
                raise RegistryStoreError
            info = connection.execute(
                "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (row[1],)
            ).fetchall()
            if not info:
                raise RegistryStoreError
            indexes.add((tuple(cast(str, item[0]) for item in info), bool(row[2])))
        if indexes != fence_indexes[name]:
            raise RegistryStoreError
    trigger_names = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
    ).fetchall()
    if trigger_names != [(name,) for name in sorted(_V3_TRIGGER_SQL)]:
        raise RegistryStoreError
    for name, expected_sql in _V3_TRIGGER_SQL.items():
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?", (name,)
        ).fetchone()
        if (
            row is None
            or type(row[0]) is not str
            or _normalize_sql(row[0]) != _normalize_sql(expected_sql)
        ):
            raise RegistryStoreError


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
