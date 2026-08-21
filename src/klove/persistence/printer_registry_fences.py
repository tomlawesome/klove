"""SQLite catalogue for durable actuator-fence references.

The catalogue stores admission evidence only.  Owner journals remain the
authority for operation outcomes and are deliberately not opened here.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import Final
from uuid import uuid4

from pydantic import ValidationError

from klove.domain.fence import (
    ActiveFenceReference,
    FenceKind,
    FenceOwnerStore,
    FencePageCursor,
    FenceReference,
    FenceResolutionCode,
    FenceState,
    FenceStoreMetadata,
    FenceTransition,
    PreparedFenceReference,
    ResolvedFenceReference,
    reference_subtype,
)
from klove.persistence.printer_registry_errors import (
    RegistryBusyError,
    RegistryConflictError,
    RegistryStoreError,
    RegistryTransitionError,
)

FENCE_CONFLICT_MESSAGE: Final = "registry printer has unresolved actuator fence"
_MAX_PAGE_SIZE: Final = 1_000
_SUPPORTED_OWNER_SCHEMA_VERSIONS: Final = {
    FenceOwnerStore.CONTROL_JOURNAL: frozenset({1}),
    FenceOwnerStore.START_JOURNAL: frozenset({1}),
    FenceOwnerStore.DISPATCH_JOURNAL: frozenset({1}),
}
_UUID4_CHECK = (
    "length({column}) = 36 AND {column} NOT GLOB '*[^0-9a-f-]*' "
    "AND substr({column}, 9, 1) = '-' AND substr({column}, 14, 1) = '-' "
    "AND substr({column}, 19, 1) = '-' AND substr({column}, 24, 1) = '-' "
    "AND substr({column}, 15, 1) = '4' "
    "AND substr({column}, 20, 1) IN ('8', '9', 'a', 'b')"
)

_FENCE_STORE_TABLE_SQL: Final = f"""
CREATE TABLE registry_fence_stores (
    owner_store TEXT PRIMARY KEY NOT NULL CHECK (
        owner_store IN ('control_journal', 'start_journal', 'dispatch_journal')
    ),
    installation_uuid TEXT NOT NULL CHECK ({_UUID4_CHECK.format(column="installation_uuid")}),
    store_id TEXT UNIQUE NOT NULL CHECK ({_UUID4_CHECK.format(column="store_id")}),
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1)
) STRICT
"""
_FENCE_REFERENCE_TABLE_SQL: Final = f"""
CREATE TABLE registry_fence_references (
    fence_reference_id TEXT PRIMARY KEY NOT NULL CHECK (
        {_UUID4_CHECK.format(column="fence_reference_id")}
    ),
    printer_uuid TEXT NOT NULL CHECK ({_UUID4_CHECK.format(column="printer_uuid")}),
    fence_kind TEXT NOT NULL CHECK (
        fence_kind IN ('control', 'print_start', 'coordinator')
    ),
    owner_store TEXT NOT NULL CHECK (
        owner_store IN ('control_journal', 'start_journal', 'dispatch_journal')
    ),
    store_id TEXT NOT NULL CHECK ({_UUID4_CHECK.format(column="store_id")}),
    owner_schema_version INTEGER NOT NULL CHECK (owner_schema_version >= 1),
    operation_id TEXT NOT NULL CHECK ({_UUID4_CHECK.format(column="operation_id")}),
    state TEXT NOT NULL CHECK (state IN ('prepared', 'active', 'resolved')),
    created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),
    transitioned_at_unix_ms INTEGER NOT NULL CHECK (
        transitioned_at_unix_ms >= created_at_unix_ms
    ),
    resolution_code TEXT CHECK (
        resolution_code IS NULL OR resolution_code IN (
            'never_dispatched', 'confirmed', 'denied_before_dispatch',
            'completed', 'cancelled', 'failed'
        )
    ),
    CHECK (
        (state = 'resolved' AND resolution_code IS NOT NULL)
        OR (state IN ('prepared', 'active') AND resolution_code IS NULL)
    ),
    UNIQUE (owner_store, store_id, owner_schema_version, operation_id, fence_kind)
) STRICT
"""
_FENCE_TRANSITION_TABLE_SQL: Final = """
CREATE TABLE registry_fence_transitions (
    fence_reference_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    from_state TEXT CHECK (from_state IS NULL OR from_state IN ('prepared', 'active')),
    to_state TEXT NOT NULL CHECK (to_state IN ('prepared', 'active', 'resolved')),
    transitioned_at_unix_ms INTEGER NOT NULL CHECK (transitioned_at_unix_ms >= 0),
    resolution_code TEXT CHECK (
        resolution_code IS NULL OR resolution_code IN (
            'never_dispatched', 'confirmed', 'denied_before_dispatch',
            'completed', 'cancelled', 'failed'
        )
    ),
    PRIMARY KEY (fence_reference_id, sequence),
    FOREIGN KEY (fence_reference_id) REFERENCES registry_fence_references(fence_reference_id),
    CHECK (
        (sequence = 1 AND from_state IS NULL AND to_state = 'prepared')
        OR (sequence > 1 AND from_state IS NOT NULL AND from_state != to_state)
    ),
    CHECK (
        (to_state = 'resolved' AND resolution_code IS NOT NULL)
        OR (to_state IN ('prepared', 'active') AND resolution_code IS NULL)
    )
) STRICT
"""
_FENCE_REFERENCE_PRINTER_INDEX_SQL: Final = (
    "CREATE INDEX registry_fence_references_printer_idx "
    "ON registry_fence_references(printer_uuid, state, fence_reference_id)"
)
_FENCE_TRANSITION_REFERENCE_INDEX_SQL: Final = (
    "CREATE INDEX registry_fence_transitions_reference_idx "
    "ON registry_fence_transitions(fence_reference_id, sequence)"
)
_FENCE_STORE_INSTALLATION_INDEX_SQL: Final = (
    "CREATE INDEX registry_fence_stores_installation_idx "
    "ON registry_fence_stores(installation_uuid, owner_store)"
)
_FENCE_REFERENCE_NO_DELETE_TRIGGER_SQL: Final = """
CREATE TRIGGER registry_fence_references_no_delete
BEFORE DELETE ON registry_fence_references
BEGIN
    SELECT RAISE(ABORT, 'fence references are never deleted');
END
"""
_FENCE_REFERENCE_IMMUTABLE_TRIGGER_SQL: Final = """
CREATE TRIGGER registry_fence_references_immutable_identity
BEFORE UPDATE ON registry_fence_references
WHEN NEW.fence_reference_id != OLD.fence_reference_id
  OR NEW.printer_uuid != OLD.printer_uuid
  OR NEW.fence_kind != OLD.fence_kind
  OR NEW.owner_store != OLD.owner_store
  OR NEW.store_id != OLD.store_id
  OR NEW.owner_schema_version != OLD.owner_schema_version
  OR NEW.operation_id != OLD.operation_id
  OR NEW.created_at_unix_ms != OLD.created_at_unix_ms
BEGIN
    SELECT RAISE(ABORT, 'fence reference identity is immutable');
END
"""
_FENCE_TRANSITION_NO_UPDATE_TRIGGER_SQL: Final = """
CREATE TRIGGER registry_fence_transitions_no_update
BEFORE UPDATE ON registry_fence_transitions
BEGIN
    SELECT RAISE(ABORT, 'fence transition history is immutable');
END
"""
_FENCE_TRANSITION_NO_DELETE_TRIGGER_SQL: Final = """
CREATE TRIGGER registry_fence_transitions_no_delete
BEFORE DELETE ON registry_fence_transitions
BEGIN
    SELECT RAISE(ABORT, 'fence transition history is immutable');
END
"""
_FENCE_STORE_NO_UPDATE_TRIGGER_SQL: Final = """
CREATE TRIGGER registry_fence_stores_no_update
BEFORE UPDATE ON registry_fence_stores
BEGIN
    SELECT RAISE(ABORT, 'fence store metadata is immutable');
END
"""
_FENCE_STORE_NO_DELETE_TRIGGER_SQL: Final = """
CREATE TRIGGER registry_fence_stores_no_delete
BEFORE DELETE ON registry_fence_stores
BEGIN
    SELECT RAISE(ABORT, 'fence store metadata is immutable');
END
"""
# This trigger is deliberately on UPDATE only: creating a printer remains an
# explicit registry operation, while every existing-printer mutation is
# fenced in the same BEGIN IMMEDIATE transaction as its row update.
_PRINTER_FENCE_CLEAR_TRIGGER_SQL: Final = """
CREATE TRIGGER registry_printers_fence_clear
BEFORE UPDATE OF endpoint, lifecycle, revision, moonraker_ref,
    compatibility_ref, record_json ON registry_printers
WHEN EXISTS (
    SELECT 1 FROM registry_fence_references
    WHERE printer_uuid = OLD.printer_uuid AND state != 'resolved'
)
BEGIN
    SELECT RAISE(ABORT, 'registry printer has unresolved actuator fence');
END
"""

FENCE_TABLE_SQL: Final = (
    _FENCE_STORE_TABLE_SQL,
    _FENCE_REFERENCE_TABLE_SQL,
    _FENCE_TRANSITION_TABLE_SQL,
)
FENCE_INDEX_SQL: Final = (
    _FENCE_REFERENCE_PRINTER_INDEX_SQL,
    _FENCE_STORE_INSTALLATION_INDEX_SQL,
)
FENCE_TRIGGER_SQL: Final = {
    "registry_fence_references_no_delete": _FENCE_REFERENCE_NO_DELETE_TRIGGER_SQL,
    "registry_fence_references_immutable_identity": _FENCE_REFERENCE_IMMUTABLE_TRIGGER_SQL,
    "registry_fence_stores_no_delete": _FENCE_STORE_NO_DELETE_TRIGGER_SQL,
    "registry_fence_stores_no_update": _FENCE_STORE_NO_UPDATE_TRIGGER_SQL,
    "registry_fence_transitions_no_delete": _FENCE_TRANSITION_NO_DELETE_TRIGGER_SQL,
    "registry_fence_transitions_no_update": _FENCE_TRANSITION_NO_UPDATE_TRIGGER_SQL,
    "registry_printers_fence_clear": _PRINTER_FENCE_CLEAR_TRIGGER_SQL,
}


def initialize_fence_catalogue(connection: sqlite3.Connection) -> None:
    """Create the empty v3 catalogue inside the caller's transaction."""
    for statement in FENCE_TABLE_SQL:
        connection.execute(statement)
    for statement in FENCE_INDEX_SQL:
        connection.execute(statement)
    for statement in FENCE_TRIGGER_SQL.values():
        connection.execute(statement)


def _decode_reference(row: tuple[object, ...]) -> FenceReference:
    if len(row) != 11:
        raise RegistryStoreError
    try:
        reference = FenceReference.model_validate(
            {
                "fence_reference_id": row[0],
                "printer_uuid": row[1],
                "fence_kind": FenceKind(_text(row[2])),
                "owner_store": FenceOwnerStore(_text(row[3])),
                "store_id": row[4],
                "owner_schema_version": row[5],
                "operation_id": row[6],
                "state": FenceState(_text(row[7])),
                "created_at_unix_ms": row[8],
                "transitioned_at_unix_ms": row[9],
                "resolution_code": (
                    None if row[10] is None else FenceResolutionCode(_text(row[10]))
                ),
            }
        )
        subtype = reference_subtype(reference)
        return subtype.model_validate(reference.model_dump(mode="python"))
    except (TypeError, ValueError, ValidationError) as exc:
        raise RegistryStoreError from exc


def _decode_transition(row: tuple[object, ...]) -> FenceTransition:
    if len(row) != 6:
        raise RegistryStoreError
    try:
        return FenceTransition.model_validate(
            {
                "fence_reference_id": row[0],
                "sequence": row[1],
                "from_state": None if row[2] is None else FenceState(_text(row[2])),
                "to_state": FenceState(_text(row[3])),
                "transitioned_at_unix_ms": row[4],
                "resolution_code": (None if row[5] is None else FenceResolutionCode(_text(row[5]))),
            }
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise RegistryStoreError from exc


def _reference_row(reference: FenceReference) -> tuple[object, ...]:
    return (
        reference.fence_reference_id,
        reference.printer_uuid,
        reference.fence_kind.value,
        reference.owner_store.value,
        reference.store_id,
        reference.owner_schema_version,
        reference.operation_id,
        reference.state.value,
        reference.created_at_unix_ms,
        reference.transitioned_at_unix_ms,
        None if reference.resolution_code is None else reference.resolution_code.value,
    )


def _text(value: object) -> str:
    if type(value) is not str:
        raise RegistryStoreError
    return value


def _transition_row(transition: FenceTransition) -> tuple[object, ...]:
    return (
        transition.fence_reference_id,
        transition.sequence,
        None if transition.from_state is None else transition.from_state.value,
        transition.to_state.value,
        transition.transitioned_at_unix_ms,
        None if transition.resolution_code is None else transition.resolution_code.value,
    )


def _select_reference(connection: sqlite3.Connection, reference_id: str) -> FenceReference | None:
    row = connection.execute(
        """
        SELECT fence_reference_id, printer_uuid, fence_kind, owner_store,
               store_id, owner_schema_version, operation_id, state,
               created_at_unix_ms, transitioned_at_unix_ms, resolution_code
        FROM registry_fence_references WHERE fence_reference_id = ?
        """,
        (reference_id,),
    ).fetchone()
    return None if row is None else _decode_reference(row)


def _validate_page(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= _MAX_PAGE_SIZE:
        raise RegistryStoreError


def _validate_printer_uuid(printer_uuid: str) -> str:
    try:
        return FenceReference.model_validate(
            {
                "fence_reference_id": "00000000-0000-4000-8000-000000000000",
                "printer_uuid": printer_uuid,
                "fence_kind": FenceKind.CONTROL,
                "owner_store": FenceOwnerStore.CONTROL_JOURNAL,
                "store_id": "00000000-0000-4000-8000-000000000001",
                "owner_schema_version": 1,
                "operation_id": "00000000-0000-4000-8000-000000000002",
                "state": FenceState.PREPARED,
                "created_at_unix_ms": 0,
                "transitioned_at_unix_ms": 0,
            }
        ).printer_uuid
    except ValidationError as exc:
        raise RegistryStoreError from exc


def ensure_fence_clear(connection: sqlite3.Connection, printer_uuid: str) -> None:
    """Require no prepared or active reference for one exact printer."""
    _validate_printer_uuid(printer_uuid)
    if (
        connection.execute(
            """
        SELECT 1 FROM registry_fence_references
        WHERE printer_uuid = ? AND state != 'resolved' LIMIT 1
        """,
            (printer_uuid,),
        ).fetchone()
        is not None
    ):
        raise RegistryBusyError


def _registry_installation_uuid(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT value FROM registry_metadata WHERE key = 'installation_uuid'"
    ).fetchone()
    if row is None or len(row) != 1 or type(row[0]) is not str:
        raise RegistryStoreError
    return row[0]


def _validate_owner_store(connection: sqlite3.Connection, reference: FenceReference) -> None:
    row = connection.execute(
        """
        SELECT owner_store, installation_uuid, store_id, schema_version
        FROM registry_fence_stores WHERE owner_store = ?
        """,
        (reference.owner_store.value,),
    ).fetchone()
    if row is None:
        raise RegistryStoreError
    try:
        metadata = FenceStoreMetadata.model_validate(
            {
                "owner_store": FenceOwnerStore(row[0]),
                "installation_uuid": row[1],
                "store_id": row[2],
                "schema_version": row[3],
            }
        )
    except ValidationError as exc:
        raise RegistryStoreError from exc
    if (
        metadata.schema_version not in _SUPPORTED_OWNER_SCHEMA_VERSIONS[metadata.owner_store]
        or metadata.owner_store is not reference.owner_store
        or metadata.installation_uuid != _registry_installation_uuid(connection)
        or metadata.store_id != reference.store_id
        or metadata.schema_version != reference.owner_schema_version
    ):
        raise RegistryConflictError


def _validate_fence_catalogue(  # noqa: PLR0912
    connection: sqlite3.Connection, printer_uuids: set[str]
) -> None:
    """Validate every v3 reference and its complete immutable transition chain."""
    try:
        rows = connection.execute(
            """
            SELECT fence_reference_id, printer_uuid, fence_kind, owner_store,
                   store_id, owner_schema_version, operation_id, state,
                   created_at_unix_ms, transitioned_at_unix_ms, resolution_code
            FROM registry_fence_references ORDER BY printer_uuid, fence_reference_id
            """
        ).fetchall()
        references = tuple(_decode_reference(row) for row in rows)
        seen: set[tuple[str, str, int, str, str]] = set()
        for reference in references:
            if reference.printer_uuid not in printer_uuids:
                raise RegistryStoreError
            identity = (
                reference.owner_store.value,
                reference.store_id,
                reference.owner_schema_version,
                reference.operation_id,
                reference.fence_kind.value,
            )
            if identity in seen:
                raise RegistryStoreError
            seen.add(identity)
            _validate_owner_store(connection, reference)
            transition_rows = connection.execute(
                """
                SELECT fence_reference_id, sequence, from_state, to_state,
                       transitioned_at_unix_ms, resolution_code
                FROM registry_fence_transitions
                WHERE fence_reference_id = ? ORDER BY sequence
                """,
                (reference.fence_reference_id,),
            ).fetchall()
            transitions = tuple(_decode_transition(item) for item in transition_rows)
            if not transitions or transitions[-1].to_state is not reference.state:
                raise RegistryStoreError
            if transitions[-1].transitioned_at_unix_ms != reference.transitioned_at_unix_ms:
                raise RegistryStoreError
            if transitions[-1].resolution_code != reference.resolution_code:
                raise RegistryStoreError
            if tuple(item.sequence for item in transitions) != tuple(
                range(1, len(transitions) + 1)
            ):
                raise RegistryStoreError
            prior: FenceState | None = None
            previous_time = reference.created_at_unix_ms
            for transition in transitions:
                if transition.from_state is not prior:
                    raise RegistryStoreError
                if transition.transitioned_at_unix_ms < previous_time:
                    raise RegistryStoreError
                if transition.to_state is FenceState.PREPARED and transition.sequence != 1:
                    raise RegistryStoreError
                if prior == FenceState.RESOLVED:
                    raise RegistryStoreError
                prior = transition.to_state
                previous_time = transition.transitioned_at_unix_ms
    except RegistryStoreError:
        raise
    except (sqlite3.Error, TypeError, ValueError, ValidationError) as exc:
        raise RegistryStoreError from exc


class SqliteFenceCatalogue:
    """Typed catalogue facade over a registry connection factory."""

    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    def prepare(  # noqa: PLR0913
        self,
        *,
        printer_uuid: str,
        fence_kind: FenceKind,
        owner_store: FenceOwnerStore,
        store_id: str,
        owner_schema_version: int,
        operation_id: str,
        created_at_unix_ms: int,
        fence_reference_id: str | None = None,
        permitted_reference_ids: frozenset[str] = frozenset(),
    ) -> PreparedFenceReference:
        """Insert one prepared reference and its first immutable event."""
        reference = PreparedFenceReference.model_validate(
            {
                "fence_reference_id": fence_reference_id or str(uuid4()),
                "printer_uuid": printer_uuid,
                "fence_kind": fence_kind,
                "owner_store": owner_store,
                "store_id": store_id,
                "owner_schema_version": owner_schema_version,
                "operation_id": operation_id,
                "created_at_unix_ms": created_at_unix_ms,
                "transitioned_at_unix_ms": created_at_unix_ms,
            }
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self.prepare_in_transaction(
                    connection,
                    reference,
                    permitted_reference_ids=permitted_reference_ids,
                )
                connection.execute("COMMIT")
            except (RegistryStoreError, sqlite3.Error):
                connection.execute("ROLLBACK")
                raise
        except RegistryStoreError:
            raise
        except sqlite3.IntegrityError as exc:
            raise RegistryConflictError from exc
        except sqlite3.Error as exc:
            raise RegistryStoreError from exc
        finally:
            connection.close()

        return reference

    @staticmethod
    def prepare_in_transaction(
        connection: sqlite3.Connection,
        reference: PreparedFenceReference,
        *,
        permitted_reference_ids: frozenset[str] = frozenset(),
    ) -> PreparedFenceReference:
        _validate_owner_store(connection, reference)
        if type(permitted_reference_ids) is not frozenset or any(
            type(value) is not str for value in permitted_reference_ids
        ):
            raise RegistryTransitionError
        permitted = tuple(permitted_reference_ids)
        if permitted:
            permitted_rows = connection.execute(
                """
                SELECT fence_reference_id, printer_uuid, state
                FROM registry_fence_references
                WHERE fence_reference_id IN (SELECT value FROM json_each(?))
                """,
                (json.dumps(permitted),),
            ).fetchall()
            if len(permitted_rows) != len(permitted) or any(
                row[1] != reference.printer_uuid or row[2] == FenceState.RESOLVED.value
                for row in permitted_rows
            ):
                raise RegistryTransitionError
        conflict_query = """
            SELECT 1 FROM registry_fence_references
            WHERE printer_uuid = ? AND state != 'resolved'
        """
        params: tuple[object, ...] = (reference.printer_uuid,)
        if permitted:
            conflict_query += " AND fence_reference_id NOT IN (SELECT value FROM json_each(?))"
            params += (json.dumps(permitted),)
        if connection.execute(conflict_query + " LIMIT 1", params).fetchone() is not None:
            raise RegistryBusyError
        try:
            connection.execute(
                """
                INSERT INTO registry_fence_references (
                    fence_reference_id, printer_uuid, fence_kind, owner_store,
                    store_id, owner_schema_version, operation_id, state,
                    created_at_unix_ms, transitioned_at_unix_ms, resolution_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _reference_row(reference),
            )
            connection.execute(
                """
                INSERT INTO registry_fence_transitions (
                    fence_reference_id, sequence, from_state, to_state,
                    transitioned_at_unix_ms, resolution_code
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                _transition_row(
                    FenceTransition(
                        fence_reference_id=reference.fence_reference_id,
                        sequence=1,
                        from_state=None,
                        to_state=FenceState.PREPARED,
                        transitioned_at_unix_ms=reference.created_at_unix_ms,
                    )
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise RegistryConflictError from exc
        except sqlite3.Error as exc:
            raise RegistryStoreError from exc
        return reference

    def activate(
        self, expected: PreparedFenceReference, *, transitioned_at_unix_ms: int
    ) -> ActiveFenceReference:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = self.activate_in_transaction(
                    connection, expected, transitioned_at_unix_ms=transitioned_at_unix_ms
                )
                connection.execute("COMMIT")
                return result
            except (RegistryStoreError, sqlite3.Error):
                connection.execute("ROLLBACK")
                raise
        except RegistryStoreError:
            raise
        except sqlite3.Error as exc:
            raise RegistryStoreError from exc
        finally:
            connection.close()

    @staticmethod
    def activate_in_transaction(
        connection: sqlite3.Connection,
        expected: PreparedFenceReference,
        *,
        transitioned_at_unix_ms: int,
    ) -> ActiveFenceReference:
        current = _select_reference(connection, expected.fence_reference_id)
        if current != expected or current.state is not FenceState.PREPARED:
            raise RegistryTransitionError
        result = ActiveFenceReference.model_validate(
            {
                **expected.model_dump(mode="python"),
                "state": FenceState.ACTIVE,
                "transitioned_at_unix_ms": transitioned_at_unix_ms,
            }
        )
        cursor = connection.execute(
            """
            UPDATE registry_fence_references
            SET state = 'active', transitioned_at_unix_ms = ?
            WHERE fence_reference_id = ? AND state = 'prepared'
              AND transitioned_at_unix_ms = ?
            """,
            (
                transitioned_at_unix_ms,
                expected.fence_reference_id,
                expected.transitioned_at_unix_ms,
            ),
        )
        if cursor.rowcount != 1:
            raise RegistryTransitionError
        _append_transition(connection, expected, result)
        return result

    def resolve(
        self,
        expected: PreparedFenceReference | ActiveFenceReference,
        *,
        resolution_code: FenceResolutionCode,
        transitioned_at_unix_ms: int,
    ) -> ResolvedFenceReference:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = self.resolve_in_transaction(
                    connection,
                    expected,
                    resolution_code=resolution_code,
                    transitioned_at_unix_ms=transitioned_at_unix_ms,
                )
                connection.execute("COMMIT")
                return result
            except (RegistryStoreError, sqlite3.Error):
                connection.execute("ROLLBACK")
                raise
        except RegistryStoreError:
            raise
        except sqlite3.Error as exc:
            raise RegistryStoreError from exc
        finally:
            connection.close()

    @staticmethod
    def resolve_in_transaction(
        connection: sqlite3.Connection,
        expected: PreparedFenceReference | ActiveFenceReference,
        *,
        resolution_code: FenceResolutionCode,
        transitioned_at_unix_ms: int,
    ) -> ResolvedFenceReference:
        current = _select_reference(connection, expected.fence_reference_id)
        if current != expected:
            raise RegistryTransitionError
        result = ResolvedFenceReference.model_validate(
            {
                **expected.model_dump(mode="python"),
                "state": FenceState.RESOLVED,
                "resolution_code": resolution_code,
                "transitioned_at_unix_ms": transitioned_at_unix_ms,
            }
        )
        cursor = connection.execute(
            """
            UPDATE registry_fence_references
            SET state = 'resolved', transitioned_at_unix_ms = ?, resolution_code = ?
            WHERE fence_reference_id = ? AND state = ?
              AND transitioned_at_unix_ms = ?
            """,
            (
                transitioned_at_unix_ms,
                resolution_code.value,
                expected.fence_reference_id,
                expected.state.value,
                expected.transitioned_at_unix_ms,
            ),
        )
        if cursor.rowcount != 1:
            raise RegistryTransitionError
        _append_transition(connection, expected, result)
        return result

    def get(self, fence_reference_id: str) -> FenceReference | None:
        connection = self._connect()
        try:
            return _select_reference(connection, fence_reference_id)
        except (sqlite3.Error, RegistryStoreError) as exc:
            if isinstance(exc, RegistryStoreError):
                raise
            raise RegistryStoreError from exc
        finally:
            connection.close()

    def page(
        self,
        printer_uuid: str,
        *,
        cursor: FencePageCursor | None = None,
        limit: int = 100,
        include_resolved: bool = False,
    ) -> tuple[FenceReference, ...]:
        _validate_printer_uuid(printer_uuid)
        _validate_page(limit)
        if cursor is not None and not isinstance(cursor, FencePageCursor):
            raise RegistryStoreError
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT fence_reference_id, printer_uuid, fence_kind, owner_store,
                       store_id, owner_schema_version, operation_id, state,
                       created_at_unix_ms, transitioned_at_unix_ms, resolution_code
                FROM registry_fence_references
                WHERE printer_uuid = ? AND (? OR state != 'resolved')
                  AND (? IS NULL OR fence_reference_id > ?)
                ORDER BY fence_reference_id LIMIT ?
                """,
                (
                    printer_uuid,
                    include_resolved,
                    None if cursor is None else cursor.fence_reference_id,
                    None if cursor is None else cursor.fence_reference_id,
                    limit,
                ),
            ).fetchall()
            return tuple(_decode_reference(row) for row in rows)
        except (sqlite3.Error, RegistryStoreError) as exc:
            if isinstance(exc, RegistryStoreError):
                raise
            raise RegistryStoreError from exc
        finally:
            connection.close()

    def conflicts(self, printer_uuid: str) -> tuple[FenceReference, ...]:
        """Return bounded conflict evidence for one exact printer."""
        return self.page(printer_uuid, limit=_MAX_PAGE_SIZE)

    def transitions(
        self, fence_reference_id: str, *, after_sequence: int = 0, limit: int = 100
    ) -> tuple[FenceTransition, ...]:
        _validate_page(limit)
        if type(after_sequence) is not int or after_sequence < 0:
            raise RegistryStoreError
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT fence_reference_id, sequence, from_state, to_state,
                       transitioned_at_unix_ms, resolution_code
                FROM registry_fence_transitions
                WHERE fence_reference_id = ? AND sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                (fence_reference_id, after_sequence, limit),
            ).fetchall()
            return tuple(_decode_transition(row) for row in rows)
        except (sqlite3.Error, RegistryStoreError) as exc:
            if isinstance(exc, RegistryStoreError):
                raise
            raise RegistryStoreError from exc
        finally:
            connection.close()

    def register_store_metadata(self, metadata: FenceStoreMetadata) -> None:
        if metadata.schema_version not in _SUPPORTED_OWNER_SCHEMA_VERSIONS[metadata.owner_store]:
            raise RegistryConflictError
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if metadata.installation_uuid != _registry_installation_uuid(connection):
                    raise RegistryConflictError
                existing = connection.execute(
                    """
                    SELECT owner_store, installation_uuid, store_id, schema_version
                    FROM registry_fence_stores WHERE owner_store = ?
                    """,
                    (metadata.owner_store.value,),
                ).fetchone()
                if existing is not None:
                    if existing != (
                        metadata.owner_store.value,
                        metadata.installation_uuid,
                        metadata.store_id,
                        metadata.schema_version,
                    ):
                        raise RegistryConflictError
                    connection.execute("COMMIT")
                    return
                connection.execute(
                    """
                    INSERT INTO registry_fence_stores (
                        owner_store, installation_uuid, store_id, schema_version
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        metadata.owner_store.value,
                        metadata.installation_uuid,
                        metadata.store_id,
                        metadata.schema_version,
                    ),
                )
                connection.execute("COMMIT")
            except (sqlite3.Error, RegistryStoreError):
                connection.execute("ROLLBACK")
                raise
        except sqlite3.IntegrityError as exc:
            raise RegistryConflictError from exc
        except sqlite3.Error as exc:
            raise RegistryStoreError from exc
        finally:
            connection.close()


def _append_transition(
    connection: sqlite3.Connection, previous: FenceReference, current: FenceReference
) -> None:
    row = connection.execute(
        "SELECT MAX(sequence) FROM registry_fence_transitions WHERE fence_reference_id = ?",
        (current.fence_reference_id,),
    ).fetchone()
    if row is None or type(row[0]) is not int:
        raise RegistryStoreError
    transition = FenceTransition(
        fence_reference_id=current.fence_reference_id,
        sequence=row[0] + 1,
        from_state=previous.state,
        to_state=current.state,
        transitioned_at_unix_ms=current.transitioned_at_unix_ms,
        resolution_code=current.resolution_code,
    )
    try:
        connection.execute(
            """
            INSERT INTO registry_fence_transitions (
                fence_reference_id, sequence, from_state, to_state,
                transitioned_at_unix_ms, resolution_code
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            _transition_row(transition),
        )
    except sqlite3.IntegrityError as exc:
        raise RegistryTransitionError from exc


FenceCatalogue = SqliteFenceCatalogue


__all__ = (
    "FENCE_CONFLICT_MESSAGE",
    "FENCE_INDEX_SQL",
    "FENCE_TABLE_SQL",
    "FENCE_TRIGGER_SQL",
    "FenceCatalogue",
    "SqliteFenceCatalogue",
    "ensure_fence_clear",
    "initialize_fence_catalogue",
)
