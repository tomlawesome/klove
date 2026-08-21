"""Exact transactional schema evolution for the canonical printer registry."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from klove.persistence.printer_registry_errors import RegistryStoreError

SchemaValidator = Callable[[sqlite3.Connection], None]
SchemaInitializer = Callable[[sqlite3.Connection], None]


@dataclass(frozen=True, slots=True)
class RegistryMigrationStep:
    """One reviewed, contiguous schema transition."""

    source_version: int
    target_version: int
    apply: Callable[[sqlite3.Connection], None]


def ensure_registry_schema(
    connection: sqlite3.Connection,
    *,
    current_version: int,
    initialize_current: SchemaInitializer,
    validators: Mapping[int, SchemaValidator],
    steps: Sequence[RegistryMigrationStep] = (),
) -> None:
    """Initialize an empty registry or transactionally reach the exact current schema."""
    version = _user_version(connection)
    if version == 0:
        _initialize_empty(connection, current_version, initialize_current, validators)
        return
    if version > current_version or version not in validators:
        raise RegistryStoreError
    if version == current_version:
        validators[version](connection)
        return
    plan = _migration_plan(version, current_version, validators, steps)
    _apply_plan(connection, version, validators, plan)


def _initialize_empty(
    connection: sqlite3.Connection,
    current_version: int,
    initialize_current: SchemaInitializer,
    validators: Mapping[int, SchemaValidator],
) -> None:
    validator = validators.get(current_version)
    if current_version < 1 or validator is None:
        raise RegistryStoreError
    _begin_exclusive(connection)
    try:
        if _user_version(connection) != 0 or not _application_catalog_is_empty(connection):
            raise RegistryStoreError
        initialize_current(connection)
        connection.execute(f"PRAGMA user_version = {current_version}")
        validator(connection)
        connection.execute("COMMIT")
    except Exception:
        _rollback(connection)
        raise


def _migration_plan(
    source_version: int,
    current_version: int,
    validators: Mapping[int, SchemaValidator],
    steps: Sequence[RegistryMigrationStep],
) -> tuple[RegistryMigrationStep, ...]:
    by_source: dict[int, RegistryMigrationStep] = {}
    for step in steps:
        if (
            step.source_version < 1
            or step.target_version != step.source_version + 1
            or step.source_version in by_source
        ):
            raise RegistryStoreError
        by_source[step.source_version] = step
    plan: list[RegistryMigrationStep] = []
    version = source_version
    while version < current_version:
        candidate = by_source.get(version)
        if candidate is None or candidate.target_version not in validators:
            raise RegistryStoreError
        plan.append(candidate)
        version = candidate.target_version
    return tuple(plan)


def _apply_plan(
    connection: sqlite3.Connection,
    source_version: int,
    validators: Mapping[int, SchemaValidator],
    plan: tuple[RegistryMigrationStep, ...],
) -> None:
    _begin_exclusive(connection)
    try:
        if _user_version(connection) != source_version:
            raise RegistryStoreError
        version = source_version
        validators[version](connection)
        for step in plan:
            if step.source_version != version:
                raise RegistryStoreError
            step.apply(connection)
            version = step.target_version
            connection.execute(f"PRAGMA user_version = {version}")
            validators[version](connection)
        connection.execute("COMMIT")
    except Exception:
        _rollback(connection)
        raise


def _application_catalog_is_empty(connection: sqlite3.Connection) -> bool:
    rows = connection.execute(
        """
        SELECT type, name FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
          AND type IN ('table', 'index', 'view', 'trigger')
        ORDER BY type, name
        """
    ).fetchall()
    return rows == []


def _user_version(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute("PRAGMA user_version").fetchone()
    except sqlite3.Error as exc:
        raise RegistryStoreError from exc
    if row is None or len(row) != 1 or type(row[0]) is not int:
        raise RegistryStoreError
    return int(row[0])


def _begin_exclusive(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("BEGIN EXCLUSIVE")
    except sqlite3.Error as exc:
        raise RegistryStoreError from exc


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error as exc:
        raise RegistryStoreError from exc
