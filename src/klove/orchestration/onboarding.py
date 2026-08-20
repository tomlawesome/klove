"""Typed, crash-safe orchestration for canonical printer lifecycle changes."""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import secrets as secrets_module
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Protocol

from pydantic import ValidationError

from klove.adapters.moonraker.onboarding import MoonrakerProbeError
from klove.domain.onboarding import (
    MoonrakerEndpoint,
    PrinterIdentityEvidence,
    PrinterLifecycle,
    RegisteredPrinter,
    RegistryOperationKind,
    RegistryOperationRecord,
    RegistryOperationState,
)
from klove.domain.onboarding_requests import (
    CreatePrinterRequest,
    DisablePrinterRequest,
    RemovePrinterRequest,
    RotateCompatibilityCredentialRequest,
    RotateMoonrakerCredentialRequest,
    UpdatePrinterRequest,
)
from klove.errors import KloveError
from klove.orchestration.admission import PrinterAdmissionGates
from klove.persistence.printer_registry import (
    PrinterStore,
    RegistryBusyError,
    RegistryConflictError,
    RegistryStoreError,
    RegistryTransitionError,
)
from klove.persistence.secret_store import SecretStore, SecretStoreError

LifecycleMutationRequest = (
    CreatePrinterRequest
    | UpdatePrinterRequest
    | RotateMoonrakerCredentialRequest
    | RotateCompatibilityCredentialRequest
    | DisablePrinterRequest
    | RemovePrinterRequest
)
RevisionedLifecycleRequest = (
    UpdatePrinterRequest
    | RotateMoonrakerCredentialRequest
    | RotateCompatibilityCredentialRequest
    | DisablePrinterRequest
    | RemovePrinterRequest
)


class IdentityProbe(Protocol):
    """Narrow direct-evidence dependency used before identity-bearing commits."""

    async def probe(
        self,
        endpoint: MoonrakerEndpoint,
        api_key: str,
    ) -> PrinterIdentityEvidence: ...


class ActuatorFenceInspector(Protocol):
    """Prove whether one printer has any unresolved actuator outcome."""

    async def clear(self, printer_uuid: str) -> bool: ...


class ControlFenceSource(Protocol):
    """Expose unresolved in-process job-control evidence without mutating it."""

    async def has_unresolved(self, printer_id: str) -> bool: ...


class DispatchFenceSource(Protocol):
    """Expose durable unresolved print-start evidence without reconciling it."""

    def unresolved(self, printer_uuid: str) -> tuple[object, ...]: ...


class FenceInspectionError(KloveError):
    """The complete control/dispatch fence state could not be proven."""


class CompositeActuatorFenceInspector:
    """Require both job-control and durable print-start evidence to be clear."""

    def __init__(self, control: ControlFenceSource, dispatch: DispatchFenceSource) -> None:
        self._control = control
        self._dispatch = dispatch

    async def clear(self, printer_uuid: str) -> bool:
        try:
            control_unresolved = await self._control.has_unresolved(printer_uuid)
            dispatch_unresolved = self._dispatch.unresolved(printer_uuid)
        except Exception as exc:
            raise FenceInspectionError from exc
        if type(control_unresolved) is not bool or type(dispatch_unresolved) is not tuple:
            raise FenceInspectionError
        return not control_unresolved and not dispatch_unresolved


class LifecycleFailureCode(StrEnum):
    """Bounded non-secret failures exposed by registry lifecycle orchestration."""

    CONFLICT = "conflict"
    BUSY = "busy"
    STALE_REVISION = "stale_revision"
    INVALID_TRANSITION = "invalid_transition"
    PROBE_FAILED = "probe_failed"
    PRINTER_FENCED = "printer_fenced"
    FENCE_UNAVAILABLE = "fence_unavailable"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    INTERNAL_FAILURE = "internal_failure"


class LifecycleServiceError(KloveError):
    """One bounded lifecycle failure with no submitted secret material."""

    def __init__(self, code: LifecycleFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class PrinterLifecycleService:
    """Reserve, prove, persist, and reconcile one exact printer mutation."""

    def __init__(  # noqa: PLR0913 -- explicit persistence, proof, entropy, and time boundaries.
        self,
        store: PrinterStore,
        secrets: SecretStore,
        probe: IdentityProbe,
        fences: ActuatorFenceInspector,
        *,
        admissions: PrinterAdmissionGates,
        random_bytes: Callable[[int], bytes] = secrets_module.token_bytes,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._store = store
        self._secrets = secrets
        self._probe = probe
        self._fences = fences
        self._random_bytes = random_bytes
        self._clock_ms = clock_ms or _unix_time_ms
        self._admissions = admissions

    async def create(self, request: CreatePrinterRequest) -> RegisteredPrinter:
        """Create one registration only after direct current Moonraker evidence."""
        return await self._create(RegistryOperationKind.CREATE, request)

    async def bootstrap_import(self, request: CreatePrinterRequest) -> RegisteredPrinter:
        """Import one file-configured printer through the exact create contract."""
        return await self._create(RegistryOperationKind.BOOTSTRAP_IMPORT, request)

    async def _create(
        self,
        kind: RegistryOperationKind,
        request: CreatePrinterRequest,
    ) -> RegisteredPrinter:
        fingerprint = self._fingerprint(kind, request)
        duplicate = self._existing_result(kind, request, fingerprint)
        if duplicate is not None:
            return duplicate
        if self._get_printer(request.printer_uuid) is not None:
            raise LifecycleServiceError(LifecycleFailureCode.CONFLICT)
        moonraker_ref = self._allocate_reference()
        compatibility_ref = self._allocate_reference()
        if moonraker_ref == compatibility_ref:
            raise LifecycleServiceError(LifecycleFailureCode.STORAGE_UNAVAILABLE)
        compatibility_secret = self._new_compatibility_secret()
        operation = self._operation(
            kind,
            request,
            fingerprint,
            new_refs=(moonraker_ref, compatibility_ref),
        )

        async def complete(prepared: RegistryOperationRecord) -> RegisteredPrinter:
            credential = request.moonraker_credential.get_secret_value()
            self._secrets.write(moonraker_ref, credential, minimum_length=32)
            self._secrets.write(compatibility_ref, compatibility_secret, minimum_length=20)
            identity = await self._probe.probe(request.endpoint, credential)
            committed_at = self._now_ms()
            result = RegisteredPrinter(
                printer_uuid=request.printer_uuid,
                display_name=request.display_name,
                endpoint=request.endpoint,
                lifecycle=PrinterLifecycle.ACTIVE,
                moonraker_credential_ref=moonraker_ref,
                compatibility_credential_ref=compatibility_ref,
                identity=identity,
                safety_profiles=request.safety_profiles,
                control_enabled=request.control_enabled,
                dispatch_enabled=request.dispatch_enabled,
                revision=1,
                created_at_unix_ms=committed_at,
                updated_at_unix_ms=committed_at,
            )
            return self._commit(prepared, result, committed_at)

        return await self._run_prepared(operation, complete)

    async def update(self, request: UpdatePrinterRequest) -> RegisteredPrinter:
        """Refresh direct evidence and replace one exact mutable registration."""
        fingerprint = self._fingerprint(RegistryOperationKind.UPDATE, request)
        duplicate = self._existing_result(RegistryOperationKind.UPDATE, request, fingerprint)
        if duplicate is not None:
            return duplicate
        current = self._current(request, {PrinterLifecycle.ACTIVE, PrinterLifecycle.DISABLED})
        if (current.lifecycle is PrinterLifecycle.DISABLED) is not request.reactivate:
            raise LifecycleServiceError(LifecycleFailureCode.INVALID_TRANSITION)
        operation = self._operation(RegistryOperationKind.UPDATE, request, fingerprint)

        async def complete(prepared: RegistryOperationRecord) -> RegisteredPrinter:
            await self._require_clear_fences(request.printer_uuid)
            credential = self._secrets.read(
                _required_reference(current.moonraker_credential_ref), minimum_length=32
            )
            identity = await self._probe.probe(request.endpoint, credential)
            committed_at = self._now_ms()
            result = _updated_printer(
                current,
                {
                    "display_name": request.display_name,
                    "endpoint": request.endpoint,
                    "lifecycle": PrinterLifecycle.ACTIVE,
                    "identity": identity,
                    "safety_profiles": request.safety_profiles,
                    "control_enabled": request.control_enabled,
                    "dispatch_enabled": request.dispatch_enabled,
                    "revision": current.revision + 1,
                    "updated_at_unix_ms": committed_at,
                },
            )
            return self._commit(prepared, result, committed_at)

        return await self._run_prepared(operation, complete)

    async def rotate_moonraker(
        self,
        request: RotateMoonrakerCredentialRequest,
    ) -> RegisteredPrinter:
        """Commit a new credential only when it directly proves the same route."""
        fingerprint = self._fingerprint(RegistryOperationKind.ROTATE_MOONRAKER, request)
        duplicate = self._existing_result(
            RegistryOperationKind.ROTATE_MOONRAKER, request, fingerprint
        )
        if duplicate is not None:
            return duplicate
        current = self._current(request, {PrinterLifecycle.ACTIVE})
        old_ref = _required_reference(current.moonraker_credential_ref)
        new_ref = self._allocate_reference()
        if new_ref == old_ref:
            raise LifecycleServiceError(LifecycleFailureCode.STORAGE_UNAVAILABLE)
        operation = self._operation(
            RegistryOperationKind.ROTATE_MOONRAKER,
            request,
            fingerprint,
            new_refs=(new_ref,),
            retired_refs=(old_ref,),
        )

        async def complete(prepared: RegistryOperationRecord) -> RegisteredPrinter:
            await self._require_clear_fences(request.printer_uuid)
            credential = request.moonraker_credential.get_secret_value()
            current_credential = self._secrets.read(old_ref, minimum_length=32)
            if hmac.compare_digest(credential, current_credential):
                raise LifecycleServiceError(LifecycleFailureCode.INVALID_TRANSITION)
            self._secrets.write(new_ref, credential, minimum_length=32)
            identity = await self._probe.probe(current.endpoint, credential)
            committed_at = self._now_ms()
            result = _updated_printer(
                current,
                {
                    "moonraker_credential_ref": new_ref,
                    "identity": identity,
                    "revision": current.revision + 1,
                    "updated_at_unix_ms": committed_at,
                },
            )
            return self._commit(prepared, result, committed_at)

        return await self._run_prepared(operation, complete)

    async def rotate_compatibility(
        self,
        request: RotateCompatibilityCredentialRequest,
    ) -> RegisteredPrinter:
        """Generate a new compatibility copy without returning its active value."""
        fingerprint = self._fingerprint(RegistryOperationKind.ROTATE_COMPATIBILITY, request)
        duplicate = self._existing_result(
            RegistryOperationKind.ROTATE_COMPATIBILITY, request, fingerprint
        )
        if duplicate is not None:
            return duplicate
        current = self._current(request, {PrinterLifecycle.ACTIVE})
        old_ref = _required_reference(current.compatibility_credential_ref)
        new_ref = self._allocate_reference()
        if new_ref == old_ref:
            raise LifecycleServiceError(LifecycleFailureCode.STORAGE_UNAVAILABLE)
        compatibility_secret = self._new_compatibility_secret()
        operation = self._operation(
            RegistryOperationKind.ROTATE_COMPATIBILITY,
            request,
            fingerprint,
            new_refs=(new_ref,),
            retired_refs=(old_ref,),
        )

        async def complete(prepared: RegistryOperationRecord) -> RegisteredPrinter:
            await self._require_clear_fences(request.printer_uuid)
            current_secret = self._secrets.read(old_ref, minimum_length=20)
            if hmac.compare_digest(compatibility_secret, current_secret):
                raise LifecycleServiceError(LifecycleFailureCode.INVALID_TRANSITION)
            self._secrets.write(new_ref, compatibility_secret, minimum_length=20)
            committed_at = self._now_ms()
            result = _updated_printer(
                current,
                {
                    "compatibility_credential_ref": new_ref,
                    "revision": current.revision + 1,
                    "updated_at_unix_ms": committed_at,
                },
            )
            return self._commit(prepared, result, committed_at)

        return await self._run_prepared(operation, complete)

    async def disable(self, request: DisablePrinterRequest) -> RegisteredPrinter:
        """Disable controls and dispatch while retaining exact identity and secrets."""
        fingerprint = self._fingerprint(RegistryOperationKind.DISABLE, request)
        duplicate = self._existing_result(RegistryOperationKind.DISABLE, request, fingerprint)
        if duplicate is not None:
            return duplicate
        current = self._current(request, {PrinterLifecycle.ACTIVE})
        operation = self._operation(RegistryOperationKind.DISABLE, request, fingerprint)

        async def complete(prepared: RegistryOperationRecord) -> RegisteredPrinter:
            await self._require_clear_fences(request.printer_uuid)
            committed_at = self._now_ms()
            result = _updated_printer(
                current,
                {
                    "lifecycle": PrinterLifecycle.DISABLED,
                    "control_enabled": False,
                    "dispatch_enabled": False,
                    "revision": current.revision + 1,
                    "updated_at_unix_ms": committed_at,
                },
            )
            return self._commit(prepared, result, committed_at)

        return await self._run_prepared(operation, complete)

    async def remove(self, request: RemovePrinterRequest) -> RegisteredPrinter:
        """Tombstone one disabled registration and durably retire both secrets."""
        fingerprint = self._fingerprint(RegistryOperationKind.REMOVE, request)
        duplicate = self._existing_result(RegistryOperationKind.REMOVE, request, fingerprint)
        if duplicate is not None:
            return duplicate
        current = self._current(request, {PrinterLifecycle.DISABLED})
        retired = (
            _required_reference(current.moonraker_credential_ref),
            _required_reference(current.compatibility_credential_ref),
        )
        operation = self._operation(
            RegistryOperationKind.REMOVE,
            request,
            fingerprint,
            retired_refs=retired,
        )

        async def complete(prepared: RegistryOperationRecord) -> RegisteredPrinter:
            await self._require_clear_fences(request.printer_uuid)
            committed_at = self._now_ms()
            result = _updated_printer(
                current,
                {
                    "lifecycle": PrinterLifecycle.REMOVED,
                    "moonraker_credential_ref": None,
                    "compatibility_credential_ref": None,
                    "revision": current.revision + 1,
                    "updated_at_unix_ms": committed_at,
                },
            )
            return self._commit(prepared, result, committed_at)

        return await self._run_prepared(operation, complete)

    async def _run_prepared(
        self,
        operation: RegistryOperationRecord,
        complete: Callable[[RegistryOperationRecord], Awaitable[RegisteredPrinter]],
    ) -> RegisteredPrinter:
        try:
            prepared, created = self._store.reserve(operation)
        except Exception as exc:
            raise _service_error(exc) from exc
        if not created:
            return self._durable_result(prepared)
        try:
            async with self._admissions.hold(operation.printer_uuid):
                return await complete(prepared)
        except asyncio.CancelledError:
            self._abort_if_preparing(prepared)
            raise
        except Exception as exc:
            self._abort_if_preparing(prepared)
            if isinstance(exc, LifecycleServiceError):
                raise
            raise _service_error(exc) from exc

    def _existing_result(
        self,
        operation: RegistryOperationKind,
        request: LifecycleMutationRequest,
        fingerprint: str,
    ) -> RegisteredPrinter | None:
        try:
            existing = self._store.lookup_operation(request.idempotency_key)
        except Exception as exc:
            raise _service_error(exc) from exc
        if existing is None or existing.state is RegistryOperationState.ABORTED:
            return None
        if not _same_request(existing, operation, request, fingerprint):
            raise LifecycleServiceError(LifecycleFailureCode.CONFLICT)
        if existing.state is RegistryOperationState.PREPARING:
            raise LifecycleServiceError(LifecycleFailureCode.BUSY)
        return self._durable_result(existing)

    def _durable_result(self, operation: RegistryOperationRecord) -> RegisteredPrinter:
        if operation.state is RegistryOperationState.PREPARING:
            raise LifecycleServiceError(LifecycleFailureCode.BUSY)
        if operation.state is not RegistryOperationState.COMMITTED or operation.result is None:
            raise LifecycleServiceError(LifecycleFailureCode.STORAGE_UNAVAILABLE)
        try:
            finalized = self._store.finalize(operation)
        except Exception as exc:
            raise _service_error(exc) from exc
        if finalized.result is None:
            raise LifecycleServiceError(LifecycleFailureCode.STORAGE_UNAVAILABLE)
        return finalized.result

    def _current(
        self,
        request: RevisionedLifecycleRequest,
        allowed: set[PrinterLifecycle],
    ) -> RegisteredPrinter:
        try:
            current = self._store.get(request.printer_uuid)
        except Exception as exc:
            raise _service_error(exc) from exc
        expected = _expected_revision(request)
        if current is None or current.revision != expected:
            raise LifecycleServiceError(LifecycleFailureCode.STALE_REVISION)
        if current.lifecycle not in allowed:
            raise LifecycleServiceError(LifecycleFailureCode.INVALID_TRANSITION)
        return current

    def _get_printer(self, printer_uuid: str) -> RegisteredPrinter | None:
        try:
            return self._store.get(printer_uuid)
        except Exception as exc:
            raise _service_error(exc) from exc

    def _allocate_reference(self) -> str:
        try:
            return self._secrets.allocate_reference()
        except Exception as exc:
            raise _service_error(exc) from exc

    def _operation(
        self,
        kind: RegistryOperationKind,
        request: LifecycleMutationRequest,
        fingerprint: str,
        *,
        new_refs: tuple[str, ...] = (),
        retired_refs: tuple[str, ...] = (),
    ) -> RegistryOperationRecord:
        try:
            return RegistryOperationRecord(
                idempotency_key=request.idempotency_key,
                operation=kind,
                printer_uuid=request.printer_uuid,
                request_fingerprint=fingerprint,
                state=RegistryOperationState.PREPARING,
                actor=request.actor,
                request_origin=request.request_origin,
                expected_revision=_expected_revision(request),
                new_credential_refs=new_refs,
                retired_credential_refs=retired_refs,
                started_at_unix_ms=self._now_ms(),
            )
        except ValidationError as exc:
            raise LifecycleServiceError(LifecycleFailureCode.INVALID_TRANSITION) from exc

    def _fingerprint(
        self,
        kind: RegistryOperationKind,
        request: LifecycleMutationRequest,
    ) -> str:
        try:
            document = request.model_dump(mode="json")
            if isinstance(request, (CreatePrinterRequest, RotateMoonrakerCredentialRequest)):
                document["moonraker_credential"] = request.moonraker_credential.get_secret_value()
            document["operation"] = kind.value
            encoded = json.dumps(
                document,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            return self._secrets.fingerprint(encoded)
        except Exception as exc:
            raise _service_error(exc) from exc

    async def _require_clear_fences(self, printer_uuid: str) -> None:
        try:
            clear = await self._fences.clear(printer_uuid)
        except FenceInspectionError as exc:
            raise LifecycleServiceError(LifecycleFailureCode.FENCE_UNAVAILABLE) from exc
        except Exception as exc:
            raise LifecycleServiceError(LifecycleFailureCode.FENCE_UNAVAILABLE) from exc
        if type(clear) is not bool:
            raise LifecycleServiceError(LifecycleFailureCode.FENCE_UNAVAILABLE)
        if not clear:
            raise LifecycleServiceError(LifecycleFailureCode.PRINTER_FENCED)

    def _commit(
        self,
        operation: RegistryOperationRecord,
        result: RegisteredPrinter,
        committed_at: int,
    ) -> RegisteredPrinter:
        committed = self._store.commit(
            operation,
            result,
            committed_at_unix_ms=committed_at,
        )
        if committed.result is None:
            raise LifecycleServiceError(LifecycleFailureCode.STORAGE_UNAVAILABLE)
        return committed.result

    def _abort_if_preparing(self, operation: RegistryOperationRecord) -> None:
        try:
            stored = self._store.lookup_operation(operation.idempotency_key)
            if stored is None:
                raise LifecycleServiceError(LifecycleFailureCode.STORAGE_UNAVAILABLE)
            if stored.state is RegistryOperationState.PREPARING:
                self._store.abort(stored)
        except LifecycleServiceError:
            raise
        except Exception as exc:
            raise LifecycleServiceError(LifecycleFailureCode.STORAGE_UNAVAILABLE) from exc

    def _new_compatibility_secret(self) -> str:
        try:
            generated = self._random_bytes(15)
        except Exception as exc:
            raise LifecycleServiceError(LifecycleFailureCode.STORAGE_UNAVAILABLE) from exc
        if type(generated) is not bytes or len(generated) != 15:
            raise LifecycleServiceError(LifecycleFailureCode.STORAGE_UNAVAILABLE)
        try:
            value = base64.urlsafe_b64encode(generated).decode("ascii")
        except Exception as exc:
            raise LifecycleServiceError(LifecycleFailureCode.STORAGE_UNAVAILABLE) from exc
        if len(value) != 20 or "=" in value:
            raise LifecycleServiceError(LifecycleFailureCode.STORAGE_UNAVAILABLE)
        return value

    def _now_ms(self) -> int:
        try:
            value = self._clock_ms()
        except Exception as exc:
            raise LifecycleServiceError(LifecycleFailureCode.INTERNAL_FAILURE) from exc
        if type(value) is not int or not 0 <= value <= 9_223_372_036_854_775_807:
            raise LifecycleServiceError(LifecycleFailureCode.INTERNAL_FAILURE)
        return value


def _same_request(
    existing: RegistryOperationRecord,
    kind: RegistryOperationKind,
    request: LifecycleMutationRequest,
    fingerprint: str,
) -> bool:
    return (
        existing.operation is kind
        and existing.printer_uuid == request.printer_uuid
        and existing.request_fingerprint == fingerprint
        and existing.actor == request.actor
        and existing.request_origin == request.request_origin
        and existing.expected_revision == _expected_revision(request)
    )


def _expected_revision(request: LifecycleMutationRequest) -> int | None:
    if isinstance(request, CreatePrinterRequest):
        return None
    return request.expected_revision


def _required_reference(reference: str | None) -> str:
    if reference is None:
        raise LifecycleServiceError(LifecycleFailureCode.STORAGE_UNAVAILABLE)
    return reference


def _updated_printer(
    current: RegisteredPrinter,
    updates: dict[str, object],
) -> RegisteredPrinter:
    document = current.model_dump(mode="python")
    document.update(updates)
    try:
        return RegisteredPrinter.model_validate(document)
    except ValidationError as exc:
        raise LifecycleServiceError(LifecycleFailureCode.INVALID_TRANSITION) from exc


def _service_error(error: Exception) -> LifecycleServiceError:  # noqa: PLR0911
    if isinstance(error, LifecycleServiceError):
        return error
    if isinstance(error, MoonrakerProbeError):
        return LifecycleServiceError(LifecycleFailureCode.PROBE_FAILED)
    if isinstance(error, FenceInspectionError):
        return LifecycleServiceError(LifecycleFailureCode.FENCE_UNAVAILABLE)
    if isinstance(error, RegistryConflictError):
        return LifecycleServiceError(LifecycleFailureCode.CONFLICT)
    if isinstance(error, RegistryBusyError):
        return LifecycleServiceError(LifecycleFailureCode.BUSY)
    if isinstance(error, RegistryTransitionError):
        return LifecycleServiceError(LifecycleFailureCode.INVALID_TRANSITION)
    if isinstance(error, (RegistryStoreError, SecretStoreError)):
        return LifecycleServiceError(LifecycleFailureCode.STORAGE_UNAVAILABLE)
    if isinstance(error, ValidationError):
        return LifecycleServiceError(LifecycleFailureCode.INVALID_TRANSITION)
    return LifecycleServiceError(LifecycleFailureCode.INTERNAL_FAILURE)


def _unix_time_ms() -> int:
    return time.time_ns() // 1_000_000
