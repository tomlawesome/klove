"""Interrupted-operation cleanup and consistency reconciliation for the registry."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from pydantic import ValidationError

from klove.domain.onboarding import (
    RegisteredPrinter,
    RegistryOperationRecord,
    RegistryOperationState,
)
from klove.persistence.printer_registry_codec import (
    _canonical_json,
    _decode_operation,
    _operation_update_row,
)
from klove.persistence.printer_registry_errors import (
    RegistryConflictError,
    RegistryStoreError,
)
from klove.persistence.printer_registry_transactions import (
    _require_printer_secrets,
    _validate_cleanup,
)
from klove.persistence.secret_store import SecretStore, SecretStoreError


def _all_operations(
    connect: Callable[[], sqlite3.Connection],
) -> tuple[RegistryOperationRecord, ...]:
    connection = connect()
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
    connect: Callable[[], sqlite3.Connection],
    secrets: SecretStore,
    previous: RegistryOperationRecord,
    changed: RegistryOperationRecord,
    references: tuple[str, ...],
) -> None:
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _validate_cleanup(connection, previous, references)
            for reference in references:
                secrets.delete(reference)
            cursor = connection.execute(
                """
                UPDATE registry_operations
                SET operation = ?, printer_uuid = ?, request_fingerprint = ?,
                    state = ?, record_json = ?
                WHERE idempotency_key = ? AND state = ? AND record_json = ?
                """,
                (*_operation_update_row(changed), previous.state.value, _canonical_json(previous)),
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
    except (SecretStoreError, sqlite3.Error, UnicodeError, ValidationError, ValueError) as exc:
        raise RegistryStoreError from exc
    finally:
        connection.close()


def _reconcile(
    printers: tuple[RegisteredPrinter, ...],
    operations: tuple[RegistryOperationRecord, ...],
    secrets: SecretStore,
    abort: Callable[[RegistryOperationRecord], RegistryOperationRecord],
    finalize: Callable[[RegistryOperationRecord], RegistryOperationRecord],
) -> None:
    try:
        expected = {
            reference
            for printer in printers
            for reference in (
                printer.moonraker_credential_ref,
                printer.compatibility_credential_ref,
            )
            if reference is not None
        }
        for operation in operations:
            if operation.state is RegistryOperationState.PREPARING:
                if set(operation.new_credential_refs) & expected:
                    raise RegistryStoreError
                abort(operation)
            elif operation.state is RegistryOperationState.COMMITTED:
                if set(operation.retired_credential_refs) & expected:
                    raise RegistryStoreError
                finalize(operation)
        for printer in printers:
            _require_printer_secrets(secrets, printer)
        if secrets.references() != expected:
            raise RegistryStoreError
    except RegistryStoreError:
        raise
    except SecretStoreError as exc:
        raise RegistryStoreError from exc
