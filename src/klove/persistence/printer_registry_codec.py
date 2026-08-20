"""Canonical JSON and strict SQLite row codecs for the printer registry."""

from __future__ import annotations

import json
from typing import cast

from klove.domain.onboarding import RegisteredPrinter, RegistryOperationRecord
from klove.persistence.printer_registry_errors import RegistryStoreError
from klove.persistence.printer_registry_schema import _OPERATION_COLUMNS, _PRINTER_COLUMNS


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
    operation: RegistryOperationRecord, updates: dict[str, object]
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
            normalized, key=lambda item: json.dumps(item, ensure_ascii=True, sort_keys=True)
        )
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value
