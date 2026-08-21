"""Exact transactional schema evolution for the canonical printer registry."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Final, cast
from uuid import uuid4

from klove.domain.artifacts import SafetyProfile, safety_profile_fingerprint
from klove.domain.onboarding import PrinterIdentityEvidence, RegisteredPrinter
from klove.persistence.printer_registry_codec import _decode_operation, _decode_printer
from klove.persistence.printer_registry_errors import RegistryStoreError
from klove.persistence.printer_registry_fences import (
    _validate_fence_catalogue,
    initialize_fence_catalogue,
)
from klove.persistence.printer_registry_schema import (
    _MAPPING_HISTORY_DELETE_TRIGGER_SQL,
    _MAPPING_HISTORY_TABLE_SQL,
    _MAPPING_HISTORY_UPDATE_TRIGGER_SQL,
    _PROFILE_HISTORY_DELETE_TRIGGER_SQL,
    _PROFILE_HISTORY_TABLE_SQL,
    _PROFILE_HISTORY_UPDATE_TRIGGER_SQL,
    _SCHEMA_VERSION_V1,
    _SCHEMA_VERSION_V2,
    _SCHEMA_VERSION_V3,
    _validate_metadata_v3,
    _validate_schema,
    _validate_schema_v2_structure,
    _validate_schema_v3_structure,
)

SchemaValidator = Callable[[sqlite3.Connection], None]
SchemaInitializer = Callable[[sqlite3.Connection], None]


@dataclass(frozen=True, slots=True)
class RegistryMigrationStep:
    """One reviewed, contiguous schema transition."""

    source_version: int
    target_version: int
    apply: Callable[[sqlite3.Connection], None]


def validate_v1_source(connection: sqlite3.Connection) -> None:
    """Validate v1 structure and every secret-free canonical registry row."""
    _validate_schema(connection)
    _read_v1_records(connection)


def validate_v2(connection: sqlite3.Connection) -> None:
    """Validate the exact v2 structure, canonical rows, and history coverage."""
    _validate_schema_v2_structure(connection)
    printers = _read_v1_records(connection)
    _validate_history(connection, printers)


def validate_v3(connection: sqlite3.Connection) -> None:
    """Validate v3 structure, registry history, metadata, and fence rows."""
    _validate_schema_v3_structure(connection)
    printers = _read_v1_records(connection)
    _validate_history(connection, printers)
    _validate_metadata_v3(connection, _metadata_key_identity(connection))
    _validate_fence_catalogue(connection, {printer.printer_uuid for printer in printers})


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    """Backfill immutable history from the complete v1 source deterministically."""
    validate_v1_source(connection)
    connection.execute(_MAPPING_HISTORY_TABLE_SQL)
    connection.execute(_PROFILE_HISTORY_TABLE_SQL)
    connection.execute(_MAPPING_HISTORY_UPDATE_TRIGGER_SQL)
    connection.execute(_MAPPING_HISTORY_DELETE_TRIGGER_SQL)
    connection.execute(_PROFILE_HISTORY_UPDATE_TRIGGER_SQL)
    connection.execute(_PROFILE_HISTORY_DELETE_TRIGGER_SQL)
    printers = _read_v1_records(connection)
    for printer in printers:
        mapping_json = _canonical_identity_json(printer.identity)
        connection.execute(
            """
            INSERT INTO registry_mapping_history (
                printer_uuid, registry_revision, observed_at_unix_ms,
                mapping_fingerprint, mapping_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                printer.printer_uuid,
                printer.revision,
                printer.identity.observed_at_unix_ms,
                _mapping_fingerprint(printer.identity),
                mapping_json,
            ),
        )
        for profile in sorted(printer.safety_profiles, key=lambda value: value.slicer_profile_id):
            connection.execute(
                """
                INSERT INTO registry_profile_history (
                    printer_uuid, registry_revision, slicer_profile_id,
                    generation, profile_fingerprint, event, profile_json
                ) VALUES (?, ?, ?, ?, ?, 'baseline', ?)
                """,
                (
                    printer.printer_uuid,
                    printer.revision,
                    profile.slicer_profile_id,
                    profile.generation,
                    safety_profile_fingerprint(profile),
                    _canonical_model_json(profile),
                ),
            )


def _metadata_key_identity(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT key, value FROM registry_metadata WHERE key = 'request_hmac_key_sha256'"
    ).fetchall()
    if len(rows) != 1 or len(rows[0]) != 2 or type(rows[0][1]) is not str:
        raise RegistryStoreError
    if rows[0][0] != "request_hmac_key_sha256":
        raise RegistryStoreError
    return rows[0][1]


def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
    """Install v3 metadata and an empty catalogue without opening owner journals."""
    validate_v2(connection)
    connection.execute(
        "INSERT INTO registry_metadata (key, value) VALUES (?, ?)",
        ("installation_uuid", str(uuid4())),
    )
    connection.execute(
        "INSERT INTO registry_metadata (key, value) VALUES (?, ?)",
        ("store_uuid", str(uuid4())),
    )
    initialize_fence_catalogue(connection)


V1_TO_V2: Final[RegistryMigrationStep] = RegistryMigrationStep(
    _SCHEMA_VERSION_V1,
    _SCHEMA_VERSION_V2,
    _migrate_v1_to_v2,
)
V2_TO_V3: Final[RegistryMigrationStep] = RegistryMigrationStep(
    _SCHEMA_VERSION_V2,
    _SCHEMA_VERSION_V3,
    _migrate_v2_to_v3,
)
MIGRATION_STEPS: Final[tuple[RegistryMigrationStep, ...]] = (V1_TO_V2, V2_TO_V3)


def _read_v1_records(connection: sqlite3.Connection) -> tuple[RegisteredPrinter, ...]:
    try:
        printer_rows = connection.execute(
            """
            SELECT printer_uuid, endpoint, lifecycle, revision,
                   moonraker_ref, compatibility_ref, record_json
            FROM registry_printers
            ORDER BY printer_uuid
            """
        ).fetchall()
        printers = tuple(_decode_printer(row) for row in printer_rows)
        operation_rows = connection.execute(
            """
            SELECT idempotency_key, operation, printer_uuid,
                   request_fingerprint, state, record_json
            FROM registry_operations
            ORDER BY idempotency_key
            """
        ).fetchall()
        for row in operation_rows:
            _decode_operation(row)
        return printers
    except RegistryStoreError:
        raise
    except (sqlite3.Error, TypeError, UnicodeError, ValueError) as exc:
        raise RegistryStoreError from exc


def _validate_history(  # noqa: PLR0912 - one fail-closed validator covers the exact target rows.
    connection: sqlite3.Connection,
    printers: tuple[RegisteredPrinter, ...],
) -> None:
    by_uuid = {printer.printer_uuid: printer for printer in printers}
    try:
        mapping_rows = connection.execute(
            """
            SELECT printer_uuid, registry_revision, observed_at_unix_ms,
                   mapping_fingerprint, mapping_json
            FROM registry_mapping_history
            ORDER BY printer_uuid, registry_revision
            """
        ).fetchall()
        mappings: dict[str, list[tuple[int, int, str, str]]] = defaultdict(list)
        for row in mapping_rows:
            if len(row) != 5 or not isinstance(row[0], str):
                raise RegistryStoreError
            printer_uuid, revision, observed, fingerprint, mapping_json = row
            if (
                type(revision) is not int
                or type(observed) is not int
                or type(fingerprint) is not str
                or type(mapping_json) is not str
                or printer_uuid not in by_uuid
            ):
                raise RegistryStoreError
            identity = PrinterIdentityEvidence.model_validate_json(mapping_json)
            if _canonical_identity_json(identity) != mapping_json:
                raise RegistryStoreError
            if (
                identity.observed_at_unix_ms != observed
                or _mapping_fingerprint(identity) != fingerprint
                or revision > by_uuid[printer_uuid].revision
            ):
                raise RegistryStoreError
            mappings[printer_uuid].append((revision, observed, fingerprint, mapping_json))
        if set(mappings) != set(by_uuid):
            raise RegistryStoreError
        for printer_uuid, rows in mappings.items():
            if not rows or any(
                left[0] >= right[0] or left[1] >= right[1] or left[2] == right[2]
                for left, right in pairwise(rows)
            ):
                raise RegistryStoreError
            current = by_uuid[printer_uuid].identity
            if rows[-1][2] != _mapping_fingerprint(current):
                raise RegistryStoreError
            if rows[-1][1] > current.observed_at_unix_ms:
                raise RegistryStoreError

        profile_rows = connection.execute(
            """
            SELECT printer_uuid, registry_revision, slicer_profile_id,
                   generation, profile_fingerprint, event, profile_json
            FROM registry_profile_history
            ORDER BY printer_uuid, registry_revision, slicer_profile_id, event
            """
        ).fetchall()
        profiles: dict[tuple[str, str], list[tuple[int, int, str, str, SafetyProfile]]] = (
            defaultdict(list)
        )
        for row in profile_rows:
            if len(row) != 7 or not isinstance(row[0], str):
                raise RegistryStoreError
            printer_uuid, revision, slicer_id, generation, fingerprint, event, profile_json = row
            if (
                type(revision) is not int
                or type(slicer_id) is not str
                or type(generation) is not int
                or type(fingerprint) is not str
                or type(event) is not str
                or type(profile_json) is not str
                or printer_uuid not in by_uuid
                or event not in {"baseline", "bound", "retired"}
                or revision > by_uuid[printer_uuid].revision
            ):
                raise RegistryStoreError
            profile = SafetyProfile.model_validate_json(profile_json)
            if (
                profile.printer_uuid != printer_uuid
                or profile.slicer_profile_id != slicer_id
                or profile.generation != generation
                or safety_profile_fingerprint(profile) != fingerprint
                or _canonical_model_json(profile) != profile_json
            ):
                raise RegistryStoreError
            profiles[(printer_uuid, slicer_id)].append(
                (revision, generation, fingerprint, event, profile)
            )
        if not _validate_profile_history(profiles, by_uuid):
            raise RegistryStoreError
    except RegistryStoreError:
        raise
    except (sqlite3.Error, TypeError, UnicodeError, ValueError) as exc:
        raise RegistryStoreError from exc


def _validate_profile_history(
    histories: dict[tuple[str, str], list[tuple[int, int, str, str, SafetyProfile]]],
    printers: dict[str, RegisteredPrinter],
) -> bool:
    active: dict[tuple[str, str], SafetyProfile] = {}
    for identity, rows in histories.items():
        ordered = sorted(
            rows,
            key=lambda row: (
                row[0],
                {"baseline": 0, "retired": 1, "bound": 2}[row[3]],
            ),
        )
        maximum_generation = 0
        for revision, generation, _fingerprint, event, profile in ordered:
            if event == "baseline":
                if maximum_generation != 0:
                    return False
                active[identity] = profile
                maximum_generation = generation
            elif event == "retired":
                if active.get(identity) != profile:
                    return False
                active.pop(identity)
                maximum_generation = max(maximum_generation, generation)
            else:
                prior = active.get(identity)
                if prior is not None and not any(
                    item[0] == revision and item[3] == "retired" and item[4] == prior
                    for item in ordered
                ):
                    return False
                if generation <= maximum_generation:
                    return False
                active[identity] = profile
                maximum_generation = generation
    expected = {
        (printer.printer_uuid, profile.slicer_profile_id): profile
        for printer in printers.values()
        for profile in printer.safety_profiles
    }
    return active == expected


def _mapping_fingerprint(identity: PrinterIdentityEvidence) -> str:
    document = _identity_document(identity)
    document.pop("observed_at_unix_ms")
    return hashlib.sha256(_canonical_json(document).encode("ascii")).hexdigest()


def _canonical_identity_json(identity: PrinterIdentityEvidence) -> str:
    return _canonical_json(_identity_document(identity))


def _identity_document(identity: PrinterIdentityEvidence) -> dict[str, object]:
    document = identity.model_dump(mode="json")
    capabilities = cast(dict[str, object], document["capabilities"])
    capabilities["objects"] = sorted(cast(list[str], capabilities["objects"]))
    return document


def _canonical_model_json(model: SafetyProfile) -> str:
    return _canonical_json(model.model_dump(mode="json"))


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


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
