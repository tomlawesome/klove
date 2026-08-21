"""Lifecycle validation and atomic registry transaction primitives."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Protocol

from pydantic import ValidationError

from klove.domain.onboarding import (
    PrinterLifecycle,
    RegisteredPrinter,
    RegistryOperationKind,
    RegistryOperationRecord,
)
from klove.domain.registry_history import (
    RegistryHistoryAppendPlan,
    RegistryHistoryValidationError,
    build_registry_history_append_plan,
)
from klove.persistence.printer_registry_codec import (
    _decode_operation,
    _decode_printer,
    _operation_update_row,
    _printer_row,
    _printer_update_row,
)
from klove.persistence.printer_registry_errors import (
    RegistryBusyError,
    RegistryConflictError,
    RegistryStoreError,
    RegistryTransitionError,
)
from klove.persistence.secret_store import SecretStore

_COMPATIBILITY_SECRET_PATTERN: Final = re.compile(r"^[A-Za-z0-9_-]{20}$")


class RegistryHistoryWriter(Protocol):
    """Schema-owned adapter for append-only history inside this transaction."""

    def retained_profile_generations(
        self,
        connection: sqlite3.Connection,
        printer_uuid: str,
        profile_ids: tuple[str, ...],
    ) -> Mapping[str, int]:
        """Return each exact profile id's maximum retained generation."""

    def append(
        self,
        connection: sqlite3.Connection,
        plan: RegistryHistoryAppendPlan,
    ) -> None:
        """Append the already validated rows without committing the transaction."""


def _commit_transaction(
    connection: sqlite3.Connection,
    operation: RegistryOperationRecord,
    result: RegisteredPrinter,
    committed: RegistryOperationRecord,
    *,
    history: RegistryHistoryWriter | None = None,
) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        stored_row = connection.execute(
            """
            SELECT idempotency_key, operation, printer_uuid,
                   request_fingerprint, state, record_json
            FROM registry_operations WHERE idempotency_key = ?
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
        history_plan = None
        if history is not None:
            profile_ids = tuple(
                sorted(
                    {
                        profile.slicer_profile_id
                        for printer in (current, result)
                        if printer is not None
                        for profile in printer.safety_profiles
                    }
                )
            )
            retained = history.retained_profile_generations(
                connection, result.printer_uuid, profile_ids
            )
            history_plan = build_registry_history_append_plan(current, result, retained)
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
        if (
            history is not None
            and history_plan is not None
            and (history_plan.mapping is not None or history_plan.profiles)
        ):
            history.append(connection, history_plan)
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
    except RegistryHistoryValidationError as exc:
        connection.execute("ROLLBACK")
        raise RegistryTransitionError from exc
    except (RegistryStoreError, sqlite3.Error, UnicodeError, ValidationError, ValueError):
        connection.execute("ROLLBACK")
        raise


def _validate_cleanup(
    connection: sqlite3.Connection, operation: RegistryOperationRecord, references: tuple[str, ...]
) -> None:
    stored_row = connection.execute(
        """
        SELECT idempotency_key, operation, printer_uuid,
               request_fingerprint, state, record_json
        FROM registry_operations WHERE idempotency_key = ?
        """,
        (operation.idempotency_key,),
    ).fetchone()
    if stored_row is None or _decode_operation(stored_row) != operation:
        raise RegistryConflictError
    rows = connection.execute(
        """
        SELECT printer_uuid, endpoint, lifecycle, revision,
               moonraker_ref, compatibility_ref, record_json
        FROM registry_printers WHERE lifecycle != 'removed'
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
    operation: RegistryOperationRecord, current: RegisteredPrinter | None, result: RegisteredPrinter
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
        if (
            current is not None
            or operation.expected_revision is not None
            or result.revision != 1
            or result.lifecycle is not PrinterLifecycle.ACTIVE
            or result.created_at_unix_ms != result.updated_at_unix_ms
            or new_refs != result_refs
            or retired_refs
        ):
            raise RegistryTransitionError
        return
    if (
        current is None
        or operation.expected_revision != current.revision
        or result.revision != current.revision + 1
        or result.created_at_unix_ms != current.created_at_unix_ms
        or result.updated_at_unix_ms < current.updated_at_unix_ms
    ):
        raise RegistryTransitionError
    validator = _TRANSITION_VALIDATORS.get(operation.operation)
    if validator is None or not validator(
        _TransitionContext(
            current, result, new_refs, retired_refs, _credential_refs(current), result_refs
        )
    ):
        raise RegistryTransitionError


def _credential_refs(printer: RegisteredPrinter) -> set[str]:
    return {
        reference
        for reference in (printer.moonraker_credential_ref, printer.compatibility_credential_ref)
        if reference is not None
    }


def _required_reference(reference: str | None) -> str:
    if reference is None:
        raise RegistryTransitionError
    return reference


def _require_printer_secrets(secrets: SecretStore, printer: RegisteredPrinter) -> None:
    if printer.lifecycle is PrinterLifecycle.REMOVED:
        return
    secrets.read(_required_reference(printer.moonraker_credential_ref), minimum_length=32)
    compatibility_secret = secrets.read(
        _required_reference(printer.compatibility_credential_ref), minimum_length=20
    )
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
            context, {"moonraker_credential_ref", "identity", "revision", "updated_at_unix_ms"}
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
            context, {"compatibility_credential_ref", "revision", "updated_at_unix_ms"}
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
    return context.current.model_dump(mode="python", exclude=excluded) == context.result.model_dump(
        mode="python", exclude=excluded
    )


_TRANSITION_VALIDATORS: Final[dict[RegistryOperationKind, TransitionValidator]] = {
    RegistryOperationKind.UPDATE: _valid_update,
    RegistryOperationKind.ROTATE_MOONRAKER: _valid_moonraker_rotation,
    RegistryOperationKind.ROTATE_COMPATIBILITY: _valid_compatibility_rotation,
    RegistryOperationKind.DISABLE: _valid_disable,
    RegistryOperationKind.REMOVE: _valid_remove,
}


def _require_same_request(
    existing: RegistryOperationRecord, supplied: RegistryOperationRecord
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
