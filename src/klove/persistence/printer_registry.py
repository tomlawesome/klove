"""Crash-safe canonical printer registry and onboarding operation journal."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from klove.domain.onboarding import (
    RegisteredPrinter,
    RegistryOperationRecord,
    RegistryOperationState,
)
from klove.domain.registry_history import (
    MappingHistoryCursor,
    MappingHistoryRecord,
    ProfileHistoryCursor,
    ProfileHistoryRecord,
)
from klove.persistence.printer_registry_codec import (
    _decode_operation,
    _decode_printer,
    _operation_row,
    _operation_update_row,
    _printer_row,
    _update_operation,
)
from klove.persistence.printer_registry_errors import (
    RegistryBusyError,
    RegistryConflictError,
    RegistryStoreError,
    RegistryTransitionError,
)
from klove.persistence.printer_registry_history import SqliteRegistryHistory
from klove.persistence.printer_registry_migrations import (
    MIGRATION_STEPS,
    ensure_registry_schema,
    validate_v1_source,
    validate_v2,
)
from klove.persistence.printer_registry_reconciliation import (
    _all_operations,
    _cleanup_operation,
    _reconcile,
)
from klove.persistence.printer_registry_schema import (
    _METADATA_SCHEMA,
    _METADATA_TABLE_SQL,
    _SCHEMA_VERSION,
    _SCHEMA_VERSION_V1,
    _SCHEMA_VERSION_V2,
    _initialize_schema_v2,
    _pragma_integer,
    _validate_metadata,
    _validate_schema,
    _validate_table,
)
from klove.persistence.printer_registry_transactions import (
    _commit_transaction,
    _require_printer_available,
    _require_printer_secrets,
    _require_same_request,
    _required_reference,
    _validate_transition,
)
from klove.persistence.private_files import (
    PrivateFileError,
    fsync_directory,
    prepare_private_file,
    require_private_file,
)
from klove.persistence.secret_store import SecretStore, SecretStoreError

__all__ = (
    "_METADATA_SCHEMA",
    "_METADATA_TABLE_SQL",
    "PrinterStore",
    "RegisteredPrinter",
    "RegistryBusyError",
    "RegistryConflictError",
    "RegistryOperationRecord",
    "RegistryOperationState",
    "RegistryStoreError",
    "RegistryTransitionError",
    "_commit_transaction",
    "_decode_operation",
    "_decode_printer",
    "_operation_row",
    "_pragma_integer",
    "_printer_row",
    "_required_reference",
    "_validate_schema",
    "_validate_table",
    "_validate_transition",
)

_HISTORY = SqliteRegistryHistory()


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

                def initialize_current(target: sqlite3.Connection) -> None:
                    _initialize_schema_v2(target, self._secrets.key_identity)

                def validate_v1(target: sqlite3.Connection) -> None:
                    validate_v1_source(target)
                    _validate_metadata(target, self._secrets.key_identity)

                def validate_current(target: sqlite3.Connection) -> None:
                    validate_v2(target)
                    _validate_metadata(target, self._secrets.key_identity)

                ensure_registry_schema(
                    connection,
                    current_version=_SCHEMA_VERSION,
                    initialize_current=initialize_current,
                    validators={
                        _SCHEMA_VERSION_V1: validate_v1,
                        _SCHEMA_VERSION_V2: validate_current,
                    },
                    steps=MIGRATION_STEPS,
                )
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

    def mapping_history(
        self,
        printer_uuid: str,
        *,
        cursor: MappingHistoryCursor | None = None,
        limit: int = 100,
    ) -> tuple[MappingHistoryRecord, ...]:
        """Return one bounded stable audit page for an exact printer."""
        printer_uuid = _require_printer_uuid(printer_uuid)
        connection = self._connect()
        try:
            return _HISTORY.mapping_page(
                connection,
                printer_uuid,
                cursor=cursor,
                limit=limit,
            )
        except RegistryStoreError:
            raise
        except (sqlite3.Error, UnicodeError, ValidationError, ValueError) as exc:
            raise RegistryStoreError from exc
        finally:
            connection.close()

    def profile_history(
        self,
        printer_uuid: str,
        *,
        cursor: ProfileHistoryCursor | None = None,
        limit: int = 100,
    ) -> tuple[ProfileHistoryRecord, ...]:
        """Return one bounded stable profile-audit page for an exact printer."""
        printer_uuid = _require_printer_uuid(printer_uuid)
        connection = self._connect()
        try:
            return _HISTORY.profile_page(
                connection,
                printer_uuid,
                cursor=cursor,
                limit=limit,
            )
        except RegistryStoreError:
            raise
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
            _commit_transaction(connection, operation, result, committed, _HISTORY)
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
        _reconcile(
            self.list(include_removed=False),
            self._all_operations(),
            self._secrets,
            self.abort,
            self.finalize,
        )

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
                    validate_v2(target)
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
        return _all_operations(self._connect)

    def _cleanup_operation(
        self,
        previous: RegistryOperationRecord,
        changed: RegistryOperationRecord,
        references: tuple[str, ...],
    ) -> None:
        _cleanup_operation(self._connect, self._secrets, previous, changed, references)

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


def _require_printer_uuid(value: str) -> str:
    if type(value) is not str:
        raise RegistryStoreError
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise RegistryStoreError from exc
    if parsed.version != 4 or str(parsed) != value:
        raise RegistryStoreError
    return value
