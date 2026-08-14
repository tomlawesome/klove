"""Crash-safe canonical printer registry and onboarding operation journal."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from pydantic import ValidationError

from klove.domain.onboarding import (
    PrinterLifecycle,
    RegisteredPrinter,
    RegistryOperationKind,
    RegistryOperationRecord,
    RegistryOperationState,
)
from klove.errors import KloveError
from klove.persistence.private_files import (
    PrivateFileError,
    fsync_directory,
    prepare_private_file,
    require_private_file,
)
from klove.persistence.secret_store import SecretStore, SecretStoreError

_SCHEMA_VERSION: Final = 1
_COMPATIBILITY_SECRET_PATTERN: Final = re.compile(r"^[A-Za-z0-9_-]{20}$")
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


class RegistryStoreError(KloveError):
    """The canonical registry is unavailable, invalid, or contradictory."""


class RegistryConflictError(RegistryStoreError):
    """An idempotency key, UUID, endpoint, or credential is bound elsewhere."""


class RegistryBusyError(RegistryStoreError):
    """Another preparing operation already serializes this printer."""


class RegistryTransitionError(RegistryStoreError):
    """A requested persistent lifecycle transition is not exact or current."""


class PrinterStore:
    """Persist exact printer records and secret-free idempotent mutation evidence."""

    def __init__(self, path: Path, secrets: SecretStore) -> None:
        self._path = path
        self._secrets = secrets

    def initialize(self) -> None:
        """Create or validate the schema, then reconcile interrupted secret work."""
        try:
            self._secrets.initialize()
            prepare_private_file(self._path)
            connection = self._connect()
            try:
                version = _pragma_integer(connection, "user_version")
                if version == 0:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        connection.execute(_PRINTER_TABLE_SQL)
                        connection.execute(_OPERATION_TABLE_SQL)
                        connection.execute(_OPERATION_INDEX_SQL)
                        connection.execute(_METADATA_TABLE_SQL)
                        connection.execute(
                            "INSERT INTO registry_metadata (key, value) VALUES (?, ?)",
                            ("request_hmac_key_sha256", self._secrets.key_identity),
                        )
                        connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                        connection.execute("COMMIT")
                    except sqlite3.Error:
                        connection.execute("ROLLBACK")
                        raise
                elif version != _SCHEMA_VERSION:
                    raise RegistryStoreError
                _validate_schema(connection)
                _validate_metadata(connection, self._secrets.key_identity)
            finally:
                connection.close()
            self.reconcile()
        except RegistryStoreError:
            raise
        except (OSError, PrivateFileError, SecretStoreError, sqlite3.Error, ValueError) as exc:
            raise RegistryStoreError from exc

    def get(self, printer_uuid: str) -> RegisteredPrinter | None:
        """Return one exact durable record or absence."""
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT printer_uuid, endpoint, lifecycle, revision,
                       moonraker_ref, compatibility_ref, record_json
                FROM registry_printers
                WHERE printer_uuid = ?
                """,
                (printer_uuid,),
            ).fetchone()
            return None if row is None else _decode_printer(row)
        except (sqlite3.Error, UnicodeError, ValidationError, ValueError) as exc:
            raise RegistryStoreError from exc
        finally:
            connection.close()

    def list(self, *, include_removed: bool = False) -> tuple[RegisteredPrinter, ...]:
        """Return records in stable UUID order, excluding tombstones by default."""
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT printer_uuid, endpoint, lifecycle, revision,
                       moonraker_ref, compatibility_ref, record_json
                FROM registry_printers
                WHERE ? OR lifecycle != 'removed'
                ORDER BY printer_uuid
                """,
                (include_removed,),
            ).fetchall()
            return tuple(_decode_printer(row) for row in rows)
        except (sqlite3.Error, UnicodeError, ValidationError, ValueError) as exc:
            raise RegistryStoreError from exc
        finally:
            connection.close()

    def lookup_operation(self, idempotency_key: str) -> RegistryOperationRecord | None:
        """Return one durable operation result without exposing request material."""
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT idempotency_key, operation, printer_uuid,
                       request_fingerprint, state, record_json
                FROM registry_operations
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            return None if row is None else _decode_operation(row)
        except (sqlite3.Error, UnicodeError, ValidationError, ValueError) as exc:
            raise RegistryStoreError from exc
        finally:
            connection.close()

    def operations(self, printer_uuid: str) -> tuple[RegistryOperationRecord, ...]:
        """Return stable audit evidence for one exact printer UUID."""
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT idempotency_key, operation, printer_uuid,
                       request_fingerprint, state, record_json
                FROM registry_operations
                WHERE printer_uuid = ?
                ORDER BY idempotency_key
                """,
                (printer_uuid,),
            ).fetchall()
            return tuple(_decode_operation(row) for row in rows)
        except (sqlite3.Error, UnicodeError, ValidationError, ValueError) as exc:
            raise RegistryStoreError from exc
        finally:
            connection.close()

    def reserve(self, operation: RegistryOperationRecord) -> tuple[RegistryOperationRecord, bool]:
        """Atomically reserve one exact mutation or return its durable duplicate."""
        if operation.state is not RegistryOperationState.PREPARING:
            raise RegistryTransitionError
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT idempotency_key, operation, printer_uuid,
                           request_fingerprint, state, record_json
                    FROM registry_operations
                    WHERE idempotency_key = ?
                    """,
                    (operation.idempotency_key,),
                ).fetchone()
                if row is not None:
                    existing = _decode_operation(row)
                    _require_same_request(existing, operation)
                    if existing.state is not RegistryOperationState.ABORTED:
                        connection.execute("COMMIT")
                        return existing, False
                    _require_printer_available(
                        connection,
                        operation.printer_uuid,
                        excluding_idempotency_key=operation.idempotency_key,
                    )
                    operation = _update_operation(operation, {"attempt": existing.attempt + 1})
                    connection.execute(
                        """
                        UPDATE registry_operations
                        SET operation = ?, printer_uuid = ?, request_fingerprint = ?,
                            state = ?, record_json = ?
                        WHERE idempotency_key = ? AND state = 'aborted'
                        """,
                        _operation_update_row(operation),
                    )
                    connection.execute("COMMIT")
                    return operation, True
                _require_printer_available(connection, operation.printer_uuid)
                connection.execute(
                    """
                    INSERT INTO registry_operations (
                        idempotency_key, operation, printer_uuid,
                        request_fingerprint, state, record_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    _operation_row(operation),
                )
                connection.execute("COMMIT")
                return operation, True
            except (
                RegistryStoreError,
                sqlite3.Error,
                UnicodeError,
                ValidationError,
                ValueError,
            ):
                connection.execute("ROLLBACK")
                raise
        except RegistryStoreError:
            raise
        except (sqlite3.Error, UnicodeError, ValidationError, ValueError) as exc:
            raise RegistryStoreError from exc
        finally:
            connection.close()

    def commit(
        self,
        operation: RegistryOperationRecord,
        result: RegisteredPrinter,
        *,
        committed_at_unix_ms: int,
    ) -> RegistryOperationRecord:
        """Atomically apply one exact prepared transition and persist its public result."""
        if operation.state is not RegistryOperationState.PREPARING:
            raise RegistryTransitionError
        try:
            for reference in operation.new_credential_refs:
                if not self._secrets.contains(reference):
                    raise RegistryTransitionError
            _require_printer_secrets(self._secrets, result)
        except SecretStoreError as exc:
            raise RegistryStoreError from exc
        connection = self._connect()
        try:
            try:
                committed = _update_operation(
                    operation,
                    {
                        "state": RegistryOperationState.COMMITTED,
                        "committed_at_unix_ms": committed_at_unix_ms,
                        "result": result,
                        "error_code": None,
                    },
                )
            except ValidationError as exc:
                raise RegistryTransitionError from exc
            _commit_transaction(connection, operation, result, committed)
        except RegistryStoreError:
            raise
        except sqlite3.IntegrityError as exc:
            raise RegistryConflictError from exc
        except (sqlite3.Error, UnicodeError, ValidationError, ValueError) as exc:
            raise RegistryStoreError from exc
        finally:
            connection.close()
        return self.finalize(committed)

    def abort(self, operation: RegistryOperationRecord) -> RegistryOperationRecord:
        """Remove uncommitted new secrets and durably retain an interrupted attempt."""
        if operation.state is not RegistryOperationState.PREPARING:
            raise RegistryTransitionError
        aborted = _update_operation(
            operation,
            {"state": RegistryOperationState.ABORTED, "error_code": "interrupted"},
        )
        self._cleanup_operation(operation, aborted, operation.new_credential_refs)
        return aborted

    def finalize(self, operation: RegistryOperationRecord) -> RegistryOperationRecord:
        """Finish post-commit local credential retirement idempotently."""
        if operation.state is not RegistryOperationState.COMMITTED:
            raise RegistryTransitionError
        if not operation.retired_credential_refs:
            return operation
        finalized = _update_operation(operation, {"retired_credential_refs": ()})
        self._cleanup_operation(operation, finalized, operation.retired_credential_refs)
        return finalized

    def reconcile(self) -> None:
        """Recover interrupted operations and require exact DB/secret consistency."""
        try:
            printers = self.list(include_removed=False)
            expected = {
                reference
                for printer in printers
                for reference in (
                    printer.moonraker_credential_ref,
                    printer.compatibility_credential_ref,
                )
                if reference is not None
            }
            operations = self._all_operations()
            for operation in operations:
                if operation.state is RegistryOperationState.PREPARING:
                    if set(operation.new_credential_refs) & expected:
                        raise RegistryStoreError
                    self.abort(operation)
                elif operation.state is RegistryOperationState.COMMITTED:
                    if set(operation.retired_credential_refs) & expected:
                        raise RegistryStoreError
                    self.finalize(operation)
            for printer in printers:
                _require_printer_secrets(self._secrets, printer)
            if self._secrets.references() != expected:
                raise RegistryStoreError
        except RegistryStoreError:
            raise
        except SecretStoreError as exc:
            raise RegistryStoreError from exc

    def backup(self, destination: Path) -> None:
        """Create one private consistent SQLite snapshot without copying secrets."""
        if destination == self._path:
            raise RegistryStoreError
        try:
            if destination.exists() or destination.is_symlink():
                raise RegistryStoreError
            prepare_private_file(destination)
            source = self._connect()
            try:
                target = sqlite3.connect(destination, timeout=5, isolation_level=None)
                try:
                    source.backup(target)
                    _validate_schema(target)
                    _validate_metadata(target, self._secrets.key_identity)
                finally:
                    target.close()
            finally:
                source.close()
            require_private_file(destination)
            fsync_directory(destination.parent)
        except RegistryStoreError:
            raise
        except (OSError, PrivateFileError, SecretStoreError, sqlite3.Error, ValueError) as exc:
            raise RegistryStoreError from exc

    def _all_operations(self) -> tuple[RegistryOperationRecord, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT idempotency_key, operation, printer_uuid,
                       request_fingerprint, state, record_json
                FROM registry_operations ORDER BY idempotency_key
                """
            ).fetchall()
            return tuple(_decode_operation(row) for row in rows)
        except (sqlite3.Error, UnicodeError, ValidationError, ValueError) as exc:
            raise RegistryStoreError from exc
        finally:
            connection.close()

    def _cleanup_operation(
        self,
        previous: RegistryOperationRecord,
        changed: RegistryOperationRecord,
        references: tuple[str, ...],
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _validate_cleanup(connection, previous, references)
                for reference in references:
                    self._secrets.delete(reference)
                cursor = connection.execute(
                    """
                    UPDATE registry_operations
                    SET operation = ?, printer_uuid = ?, request_fingerprint = ?,
                        state = ?, record_json = ?
                    WHERE idempotency_key = ? AND state = ? AND record_json = ?
                    """,
                    (
                        *_operation_update_row(changed),
                        previous.state.value,
                        _canonical_json(previous),
                    ),
                )
                if cursor.rowcount != 1:
                    raise RegistryConflictError
                connection.execute("COMMIT")
            except (
                RegistryStoreError,
                SecretStoreError,
                sqlite3.Error,
                UnicodeError,
                ValidationError,
                ValueError,
            ):
                connection.execute("ROLLBACK")
                raise
        except RegistryStoreError:
            raise
        except (
            SecretStoreError,
            sqlite3.Error,
            UnicodeError,
            ValidationError,
            ValueError,
        ) as exc:
            raise RegistryStoreError from exc
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
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            if mode != ("wal",):
                raise RegistryStoreError
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except (OSError, PrivateFileError, RegistryStoreError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            if isinstance(exc, RegistryStoreError):
                raise
            raise RegistryStoreError from exc


def _commit_transaction(
    connection: sqlite3.Connection,
    operation: RegistryOperationRecord,
    result: RegisteredPrinter,
    committed: RegistryOperationRecord,
) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        stored_row = connection.execute(
            """
            SELECT idempotency_key, operation, printer_uuid,
                   request_fingerprint, state, record_json
            FROM registry_operations
            WHERE idempotency_key = ?
            """,
            (operation.idempotency_key,),
        ).fetchone()
        if stored_row is None or _decode_operation(stored_row) != operation:
            raise RegistryConflictError
        current_row = connection.execute(
            """
            SELECT printer_uuid, endpoint, lifecycle, revision,
                   moonraker_ref, compatibility_ref, record_json
            FROM registry_printers WHERE printer_uuid = ?
            """,
            (operation.printer_uuid,),
        ).fetchone()
        current = None if current_row is None else _decode_printer(current_row)
        _validate_transition(operation, current, result)
        if current is None:
            connection.execute(
                """
                INSERT INTO registry_printers (
                    printer_uuid, endpoint, lifecycle, revision,
                    moonraker_ref, compatibility_ref, record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                _printer_row(result),
            )
        else:
            cursor = connection.execute(
                """
                UPDATE registry_printers
                SET endpoint = ?, lifecycle = ?, revision = ?,
                    moonraker_ref = ?, compatibility_ref = ?, record_json = ?
                WHERE printer_uuid = ? AND revision = ?
                """,
                _printer_update_row(result, current.revision),
            )
            if cursor.rowcount != 1:
                raise RegistryConflictError
        cursor = connection.execute(
            """
            UPDATE registry_operations
            SET operation = ?, printer_uuid = ?, request_fingerprint = ?,
                state = ?, record_json = ?
            WHERE idempotency_key = ? AND state = 'preparing'
            """,
            _operation_update_row(committed),
        )
        if cursor.rowcount != 1:
            raise RegistryConflictError
        connection.execute("COMMIT")
    except (
        RegistryStoreError,
        sqlite3.Error,
        UnicodeError,
        ValidationError,
        ValueError,
    ):
        connection.execute("ROLLBACK")
        raise


def _validate_cleanup(
    connection: sqlite3.Connection,
    operation: RegistryOperationRecord,
    references: tuple[str, ...],
) -> None:
    stored_row = connection.execute(
        """
        SELECT idempotency_key, operation, printer_uuid,
               request_fingerprint, state, record_json
        FROM registry_operations
        WHERE idempotency_key = ?
        """,
        (operation.idempotency_key,),
    ).fetchone()
    if stored_row is None or _decode_operation(stored_row) != operation:
        raise RegistryConflictError
    rows = connection.execute(
        """
        SELECT printer_uuid, endpoint, lifecycle, revision,
               moonraker_ref, compatibility_ref, record_json
        FROM registry_printers
        WHERE lifecycle != 'removed'
        """
    ).fetchall()
    active_references = {
        reference for row in rows for reference in _credential_refs(_decode_printer(row))
    }
    if set(references) & active_references:
        raise RegistryTransitionError


def _require_printer_available(
    connection: sqlite3.Connection,
    printer_uuid: str,
    *,
    excluding_idempotency_key: str | None = None,
) -> None:
    busy = connection.execute(
        """
        SELECT 1 FROM registry_operations
        WHERE printer_uuid = ? AND state = 'preparing'
          AND (? IS NULL OR idempotency_key != ?)
        LIMIT 1
        """,
        (printer_uuid, excluding_idempotency_key, excluding_idempotency_key),
    ).fetchone()
    if busy is not None:
        raise RegistryBusyError


def _validate_transition(
    operation: RegistryOperationRecord,
    current: RegisteredPrinter | None,
    result: RegisteredPrinter,
) -> None:
    if result.printer_uuid != operation.printer_uuid:
        raise RegistryTransitionError
    new_refs = set(operation.new_credential_refs)
    retired_refs = set(operation.retired_credential_refs)
    result_refs = _credential_refs(result)
    if operation.operation in {
        RegistryOperationKind.CREATE,
        RegistryOperationKind.BOOTSTRAP_IMPORT,
    }:
        if current is not None or operation.expected_revision is not None:
            raise RegistryTransitionError
        if result.revision != 1 or result.lifecycle is not PrinterLifecycle.ACTIVE:
            raise RegistryTransitionError
        if result.created_at_unix_ms != result.updated_at_unix_ms:
            raise RegistryTransitionError
        if new_refs != result_refs or retired_refs:
            raise RegistryTransitionError
        return
    if current is None or operation.expected_revision != current.revision:
        raise RegistryTransitionError
    if result.revision != current.revision + 1:
        raise RegistryTransitionError
    if result.created_at_unix_ms != current.created_at_unix_ms:
        raise RegistryTransitionError
    if result.updated_at_unix_ms < current.updated_at_unix_ms:
        raise RegistryTransitionError
    current_refs = _credential_refs(current)
    validator = _TRANSITION_VALIDATORS.get(operation.operation)
    valid = validator is not None and validator(
        _TransitionContext(current, result, new_refs, retired_refs, current_refs, result_refs)
    )
    if not valid:
        raise RegistryTransitionError


def _credential_refs(printer: RegisteredPrinter) -> set[str]:
    return {
        reference
        for reference in (
            printer.moonraker_credential_ref,
            printer.compatibility_credential_ref,
        )
        if reference is not None
    }


def _required_reference(reference: str | None) -> str:
    if reference is None:
        raise RegistryTransitionError
    return reference


def _require_printer_secrets(secrets: SecretStore, printer: RegisteredPrinter) -> None:
    if printer.lifecycle is PrinterLifecycle.REMOVED:
        return
    moonraker_ref = _required_reference(printer.moonraker_credential_ref)
    compatibility_ref = _required_reference(printer.compatibility_credential_ref)
    secrets.read(moonraker_ref, minimum_length=32)
    compatibility_secret = secrets.read(compatibility_ref, minimum_length=20)
    if _COMPATIBILITY_SECRET_PATTERN.fullmatch(compatibility_secret) is None:
        raise RegistryTransitionError


@dataclass(frozen=True, slots=True)
class _TransitionContext:
    current: RegisteredPrinter
    result: RegisteredPrinter
    new_refs: set[str]
    retired_refs: set[str]
    current_refs: set[str]
    result_refs: set[str]


TransitionValidator = Callable[[_TransitionContext], bool]


def _valid_update(context: _TransitionContext) -> bool:
    return (
        context.current.lifecycle is not PrinterLifecycle.REMOVED
        and context.result.lifecycle is PrinterLifecycle.ACTIVE
        and context.new_refs == context.retired_refs == set()
        and context.result_refs == context.current_refs
    )


def _valid_moonraker_rotation(context: _TransitionContext) -> bool:
    return (
        context.current.lifecycle is PrinterLifecycle.ACTIVE
        and context.result.lifecycle is PrinterLifecycle.ACTIVE
        and _same_record_fields(
            context,
            {"moonraker_credential_ref", "revision", "updated_at_unix_ms"},
        )
        and context.result.compatibility_credential_ref
        == context.current.compatibility_credential_ref
        and context.new_refs == {_required_reference(context.result.moonraker_credential_ref)}
        and context.retired_refs == {_required_reference(context.current.moonraker_credential_ref)}
    )


def _valid_compatibility_rotation(context: _TransitionContext) -> bool:
    return (
        context.current.lifecycle is PrinterLifecycle.ACTIVE
        and context.result.lifecycle is PrinterLifecycle.ACTIVE
        and _same_record_fields(
            context,
            {"compatibility_credential_ref", "revision", "updated_at_unix_ms"},
        )
        and context.result.moonraker_credential_ref == context.current.moonraker_credential_ref
        and context.new_refs == {_required_reference(context.result.compatibility_credential_ref)}
        and context.retired_refs
        == {_required_reference(context.current.compatibility_credential_ref)}
    )


def _valid_disable(context: _TransitionContext) -> bool:
    return (
        context.current.lifecycle is PrinterLifecycle.ACTIVE
        and context.result.lifecycle is PrinterLifecycle.DISABLED
        and _same_record_fields(
            context,
            {"lifecycle", "control_enabled", "dispatch_enabled", "revision", "updated_at_unix_ms"},
        )
        and context.new_refs == context.retired_refs == set()
        and context.result_refs == context.current_refs
    )


def _valid_remove(context: _TransitionContext) -> bool:
    return (
        context.current.lifecycle is PrinterLifecycle.DISABLED
        and context.result.lifecycle is PrinterLifecycle.REMOVED
        and _same_record_fields(
            context,
            {
                "lifecycle",
                "moonraker_credential_ref",
                "compatibility_credential_ref",
                "revision",
                "updated_at_unix_ms",
            },
        )
        and not context.new_refs
        and not context.result_refs
        and context.retired_refs == context.current_refs
    )


def _same_record_fields(context: _TransitionContext, excluded: set[str]) -> bool:
    current = context.current.model_dump(mode="python", exclude=excluded)
    result = context.result.model_dump(mode="python", exclude=excluded)
    return current == result


_TRANSITION_VALIDATORS: Final[dict[RegistryOperationKind, TransitionValidator]] = {
    RegistryOperationKind.UPDATE: _valid_update,
    RegistryOperationKind.ROTATE_MOONRAKER: _valid_moonraker_rotation,
    RegistryOperationKind.ROTATE_COMPATIBILITY: _valid_compatibility_rotation,
    RegistryOperationKind.DISABLE: _valid_disable,
    RegistryOperationKind.REMOVE: _valid_remove,
}


def _require_same_request(
    existing: RegistryOperationRecord,
    supplied: RegistryOperationRecord,
) -> None:
    if (
        existing.idempotency_key != supplied.idempotency_key
        or existing.operation is not supplied.operation
        or existing.printer_uuid != supplied.printer_uuid
        or existing.request_fingerprint != supplied.request_fingerprint
        or existing.actor != supplied.actor
        or existing.request_origin != supplied.request_origin
        or existing.expected_revision != supplied.expected_revision
    ):
        raise RegistryConflictError


def _printer_row(printer: RegisteredPrinter) -> tuple[object, ...]:
    return (
        printer.printer_uuid,
        printer.endpoint.url,
        printer.lifecycle.value,
        printer.revision,
        printer.moonraker_credential_ref,
        printer.compatibility_credential_ref,
        _canonical_json(printer),
    )


def _printer_update_row(printer: RegisteredPrinter, prior_revision: int) -> tuple[object, ...]:
    return (
        printer.endpoint.url,
        printer.lifecycle.value,
        printer.revision,
        printer.moonraker_credential_ref,
        printer.compatibility_credential_ref,
        _canonical_json(printer),
        printer.printer_uuid,
        prior_revision,
    )


def _operation_row(operation: RegistryOperationRecord) -> tuple[str, ...]:
    return (
        operation.idempotency_key,
        operation.operation.value,
        operation.printer_uuid,
        operation.request_fingerprint,
        operation.state.value,
        _canonical_json(operation),
    )


def _operation_update_row(operation: RegistryOperationRecord) -> tuple[str, ...]:
    return (
        operation.operation.value,
        operation.printer_uuid,
        operation.request_fingerprint,
        operation.state.value,
        _canonical_json(operation),
        operation.idempotency_key,
    )


def _update_operation(
    operation: RegistryOperationRecord,
    updates: dict[str, object],
) -> RegistryOperationRecord:
    document = operation.model_dump(mode="python")
    document.update(updates)
    return RegistryOperationRecord.model_validate(document)


def _decode_printer(row: tuple[object, ...]) -> RegisteredPrinter:
    if len(row) != len(_PRINTER_COLUMNS):
        raise RegistryStoreError
    printer_uuid, endpoint, lifecycle, revision, moonraker_ref, compatibility_ref, record_json = row
    if (
        not all(type(value) is str for value in (printer_uuid, endpoint, lifecycle, record_json))
        or type(revision) is not int
        or not all(
            value is None or type(value) is str for value in (moonraker_ref, compatibility_ref)
        )
    ):
        raise RegistryStoreError
    printer = RegisteredPrinter.model_validate_json(cast(str, record_json))
    if (
        printer.printer_uuid != printer_uuid
        or printer.endpoint.url != endpoint
        or printer.lifecycle.value != lifecycle
        or printer.revision != revision
        or printer.moonraker_credential_ref != moonraker_ref
        or printer.compatibility_credential_ref != compatibility_ref
        or _canonical_json(printer) != record_json
    ):
        raise RegistryStoreError
    return printer


def _decode_operation(row: tuple[object, ...]) -> RegistryOperationRecord:
    if len(row) != len(_OPERATION_COLUMNS) or not all(type(value) is str for value in row):
        raise RegistryStoreError
    idempotency_key, operation, printer_uuid, request_fingerprint, state, record_json = row
    record = RegistryOperationRecord.model_validate_json(cast(str, record_json))
    if (
        record.idempotency_key != idempotency_key
        or record.operation.value != operation
        or record.printer_uuid != printer_uuid
        or record.request_fingerprint != request_fingerprint
        or record.state.value != state
        or _canonical_json(record) != record_json
    ):
        raise RegistryStoreError
    return record


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
    quick_check = connection.execute("PRAGMA quick_check").fetchall()
    if quick_check != [("ok",)]:
        raise RegistryStoreError
    table_names = connection.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    if table_names != [
        ("registry_metadata",),
        ("registry_operations",),
        ("registry_printers",),
    ]:
        raise RegistryStoreError
    _validate_table(connection, _METADATA_SCHEMA)
    _validate_table(connection, _PRINTER_SCHEMA)
    _validate_table(connection, _OPERATION_SCHEMA)


def _validate_table(
    connection: sqlite3.Connection,
    schema: _TableSchema,
) -> None:
    rows = connection.execute(f"PRAGMA table_info({schema.name})").fetchall()
    if any(len(row) < 6 for row in rows):
        raise RegistryStoreError
    definition = tuple((row[1], row[2], row[3], row[5]) for row in rows)
    if definition != schema.definition:
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
    if sql_row is None or len(sql_row) != 1 or type(sql_row[0]) is not str:
        raise RegistryStoreError
    if _normalize_sql(sql_row[0]) != _normalize_sql(schema.sql):
        raise RegistryStoreError
    index_rows = connection.execute(f"PRAGMA index_list({schema.name})").fetchall()
    indexes: set[tuple[tuple[str, ...], bool]] = set()
    for row in index_rows:
        if len(row) < 5 or row[4] != 0 or type(row[1]) is not str or row[2] not in {0, 1}:
            raise RegistryStoreError
        name = row[1]
        info = connection.execute(
            "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (name,)
        ).fetchall()
        if not info or any(len(column) != 1 or type(column[0]) is not str for column in info):
            raise RegistryStoreError
        indexes.add((tuple(cast(str, column[0]) for column in info), bool(row[2])))
    if indexes != schema.indexes:
        raise RegistryStoreError


def _normalize_sql(value: str) -> str:
    return " ".join(value.split()).casefold()


def _canonical_json(value: RegisteredPrinter | RegistryOperationRecord) -> str:
    return json.dumps(
        _canonical_value(value.model_dump(mode="python")),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_value(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        normalized = [_canonical_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(item, ensure_ascii=True, sort_keys=True),
        )
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value
