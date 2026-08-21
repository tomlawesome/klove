"""Durable at-most-once translation from Grove MQTT into typed job control."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from klove.domain.control import ControlIntent, ControlOperation, ControlResult, ControlStatus
from klove.domain.mqtt_ingress import MqttIngressRecord, MqttIngressState
from klove.domain.translation import CommandKind, DecodedCommand
from klove.errors import JournalError
from klove.orchestration.admission import PrinterAdmissionGates
from klove.orchestration.control import ControlService
from klove.persistence.mqtt_ingress_journal import (
    MqttIngressCapacityError,
    MqttIngressConflictError,
    MqttIngressJournal,
)
from klove.registry import PrinterRegistry
from klove.security.compatibility import CompatibilityAuthenticator, CompatibilityPrincipal

_OPERATIONS = {
    CommandKind.PAUSE: ControlOperation.PAUSE,
    CommandKind.RESUME: ControlOperation.RESUME,
    CommandKind.CANCEL: ControlOperation.CANCEL,
}


@dataclass(frozen=True, slots=True)
class _InflightRequest:
    principal: CompatibilityPrincipal
    command: DecodedCommand
    task: asyncio.Task[ControlResult]


class MqttControlIngress:
    """Bind authenticated MQTT delivery to one durable canonical control result."""

    def __init__(  # noqa: PLR0913 -- explicit safety-critical dependencies and bounds.
        self,
        authenticator: CompatibilityAuthenticator,
        registry: PrinterRegistry,
        controls: ControlService,
        journal: MqttIngressJournal,
        *,
        capacity: int,
        admissions: PrinterAdmissionGates,
        id_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        if type(capacity) is not int or not 1 <= capacity <= 100_000:
            raise ValueError("MQTT ingress capacity is outside its accepted bounds")
        self._authenticator = authenticator
        self._registry = registry
        self._controls = controls
        self._journal = journal
        self._capacity = capacity
        self._admissions = admissions
        self._id_factory = id_factory
        self._ready = False
        self._inflight: dict[tuple[str, str], _InflightRequest] = {}
        self._lock = asyncio.Lock()

    def initialize(self) -> None:
        """Recover crash gaps before opening control admission."""
        self._ready = False
        try:
            self._journal.initialize()
        except JournalError:
            return
        self._ready = True

    async def execute(
        self,
        principal: CompatibilityPrincipal,
        command: DecodedCommand,
    ) -> ControlResult:
        """Execute once; concurrent exact duplicates share the same task."""
        if type(principal) is not CompatibilityPrincipal or type(command) is not DecodedCommand:
            return _denied(ControlOperation.PAUSE, "invalid_ingress")
        operation = _OPERATIONS[command.kind]
        if not self._ready:
            return _denied(operation, "ingress_storage_unavailable")
        key = (principal.printer_uuid, command.sequence_id)
        async with self._lock:
            existing = self._inflight.get(key)
            if existing is not None:
                if existing.principal != principal or existing.command != command:
                    return _denied(operation, "idempotency_conflict")
                task = existing.task
            else:
                task = asyncio.create_task(self._execute_once(principal, command, operation))
                self._inflight[key] = _InflightRequest(principal, command, task)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                async with self._lock:
                    current = self._inflight.get(key)
                    if current is not None and current.task is task:
                        del self._inflight[key]

    async def _execute_once(  # noqa: PLR0911, PLR0912 -- explicit fail-closed exits.
        self,
        principal: CompatibilityPrincipal,
        command: DecodedCommand,
        operation: ControlOperation,
    ) -> ControlResult:
        async with self._admissions.lease(principal.printer_uuid) as lease:
            if not await self._authenticator.revalidate(principal, admission_lease=lease):
                return _denied(operation, "access_denied")
            if not principal.control_enabled:
                return _denied(operation, "control_disabled")
            try:
                existing = self._journal.lookup(principal.printer_uuid, command.sequence_id)
            except JournalError:
                return _denied(operation, "ingress_storage_unavailable")
            if existing is not None:
                if (
                    existing.printer_record_revision != principal.record_revision
                    or existing.operation is not operation
                    or existing.payload_hash != command.payload_hash
                ):
                    return _denied(operation, "idempotency_conflict")
                return existing.result or _unknown(operation)
            try:
                snapshot = await self._registry.get(principal.printer_uuid)
            except Exception:
                return _denied(operation, "printer_unavailable")
            if snapshot is None:
                return _denied(operation, "printer_unknown")
            try:
                identifier = str(self._id_factory())
                reserved, created = self._journal.reserve(
                    MqttIngressRecord(
                        printer_uuid=principal.printer_uuid,
                        printer_record_revision=principal.record_revision,
                        sequence_id=command.sequence_id,
                        operation=operation,
                        payload_hash=command.payload_hash,
                        state_token=snapshot.state_token,
                        idempotency_key=identifier,
                        state=MqttIngressState.RESERVED,
                    ),
                    capacity=self._capacity,
                )
            except MqttIngressConflictError:
                return _denied(operation, "idempotency_conflict")
            except MqttIngressCapacityError:
                return _denied(operation, "idempotency_capacity")
            except Exception:
                return _denied(operation, "ingress_storage_unavailable")
            if not created:
                if reserved.result is None:
                    return _unknown(operation)
                return reserved.result

            intent = ControlIntent(
                printer_id=principal.printer_uuid,
                operation=operation,
                state_token=reserved.state_token,
                idempotency_key=reserved.idempotency_key,
            )
            try:
                result = await self._controls.execute(intent, admission_lease=lease)
            except asyncio.CancelledError:
                result = _unknown(operation)
            except Exception:
                result = _unknown(operation)
            try:
                return self._journal.complete(reserved, result).result or _unknown(operation)
            except JournalError:
                return _unknown(operation)


def _denied(operation: ControlOperation, code: str) -> ControlResult:
    return ControlResult(operation=operation, status=ControlStatus.DENIED, code=code)


def _unknown(operation: ControlOperation) -> ControlResult:
    return ControlResult(
        operation=operation,
        status=ControlStatus.OUTCOME_UNKNOWN,
        code="outcome_unknown",
    )
