"""Exact durable models for canonical printer onboarding state."""

from __future__ import annotations

import ipaddress
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from klove.domain.artifacts import CanonicalUuid4, SafetyProfile
from klove.domain.discovery import discover_capabilities
from klove.domain.models import CapabilitySnapshot

CredentialReference = Annotated[
    str,
    StringConstraints(
        pattern=r"^credential-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]
RequestFingerprint = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
DisplayName = Annotated[str, StringConstraints(min_length=1, max_length=100)]
EvidenceText = Annotated[str, StringConstraints(min_length=1, max_length=255)]
AuditText = Annotated[str, StringConstraints(min_length=1, max_length=253)]
_MAX_CAPABILITY_OBJECTS = 4_096
_MAX_CAPABILITY_OBJECT_NAME = 255
_MAX_SAFETY_PROFILES = 256


class PrinterLifecycle(StrEnum):
    """Durable lifecycle states for one canonical printer identity."""

    ACTIVE = "active"
    DISABLED = "disabled"
    REMOVED = "removed"


class RegistryOperationKind(StrEnum):
    """Closed set of durable onboarding mutations."""

    CREATE = "create"
    UPDATE = "update"
    ROTATE_MOONRAKER = "rotate_moonraker"
    ROTATE_COMPATIBILITY = "rotate_compatibility"
    DISABLE = "disable"
    REMOVE = "remove"
    BOOTSTRAP_IMPORT = "bootstrap_import"


class RegistryOperationState(StrEnum):
    """Crash-recovery states for one idempotent registry mutation."""

    PREPARING = "preparing"
    COMMITTED = "committed"
    ABORTED = "aborted"


class MoonrakerEndpoint(BaseModel):
    """Exact endpoint and explicit transport-risk choices stored for one printer."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    url: str = Field(min_length=1, max_length=2048)
    allow_insecure_http: bool = False
    verify_tls: bool = True

    @model_validator(mode="after")
    def exact_http_origin(self) -> MoonrakerEndpoint:
        """Reject credentials, paths, aliases, and implicit remote cleartext consent."""
        _exact_text(self.url)
        try:
            parsed = urlsplit(self.url)
            hostname = parsed.hostname
        except ValueError as exc:
            raise ValueError("endpoint URL is invalid") from exc
        if parsed.scheme not in {"http", "https"} or not hostname:
            raise ValueError("endpoint must be an absolute HTTP or HTTPS URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("endpoint must not contain credentials, a query, or a fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("endpoint must not contain a path")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("endpoint port is invalid") from exc
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("endpoint port is invalid")
        if parsed.scheme == "http" and not self.allow_insecure_http and not _is_loopback(hostname):
            raise ValueError("non-loopback HTTP requires allow_insecure_http=true")
        if parsed.scheme == "http" and self.verify_tls:
            raise ValueError("HTTP endpoints cannot enable TLS verification")
        return self


class PrinterIdentityEvidence(BaseModel):
    """Bounded direct Moonraker/Klipper identity and capability evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    evidence_version: Literal["1"] = "1"
    server_hostname: EvidenceText
    klipper_hostname: EvidenceText
    moonraker_version: EvidenceText
    klipper_version: EvidenceText
    capabilities: CapabilitySnapshot
    observed_at_unix_ms: int = Field(ge=0, le=9_223_372_036_854_775_807)

    @field_validator(
        "server_hostname",
        "klipper_hostname",
        "moonraker_version",
        "klipper_version",
    )
    @classmethod
    def identity_text_is_exact(cls, value: str) -> str:
        """Reject normalized aliases and control characters in remote evidence."""
        return _exact_text(value)

    @model_validator(mode="after")
    def capability_evidence_is_canonical(self) -> PrinterIdentityEvidence:
        """Require every derived capability field to match the exact object set."""
        objects = self.capabilities.objects
        if len(objects) > _MAX_CAPABILITY_OBJECTS or any(
            len(name) > _MAX_CAPABILITY_OBJECT_NAME for name in objects
        ):
            raise ValueError("capability object evidence exceeds its bound")
        for name in objects:
            _exact_text(name)
        if discover_capabilities(objects) != self.capabilities:
            raise ValueError("capability evidence is internally inconsistent")
        return self


class RegisteredPrinter(BaseModel):
    """One complete durable printer record without any secret value."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    record_version: Literal["1"] = "1"
    printer_uuid: CanonicalUuid4
    display_name: DisplayName
    endpoint: MoonrakerEndpoint
    lifecycle: PrinterLifecycle
    moonraker_credential_ref: CredentialReference | None
    compatibility_credential_ref: CredentialReference | None
    identity: PrinterIdentityEvidence
    safety_profiles: tuple[SafetyProfile, ...] = Field(default=(), max_length=_MAX_SAFETY_PROFILES)
    control_enabled: bool = False
    dispatch_enabled: bool = False
    revision: int = Field(ge=1, le=9_223_372_036_854_775_807)
    created_at_unix_ms: int = Field(ge=0, le=9_223_372_036_854_775_807)
    updated_at_unix_ms: int = Field(ge=0, le=9_223_372_036_854_775_807)

    @field_validator("display_name")
    @classmethod
    def display_name_is_exact(cls, value: str) -> str:
        """Keep display text bounded without silently normalizing identities."""
        return _exact_text(value)

    @field_validator("lifecycle", mode="before")
    @classmethod
    def lifecycle_is_exact_known_text(cls, value: object) -> object:
        """Parse exact enum text without relaxing strict validation elsewhere."""
        if type(value) is str:
            return PrinterLifecycle(value)
        return value

    @model_validator(mode="after")
    def complete_consistent_record(self) -> RegisteredPrinter:
        """Reject incomplete secrets, mismatched profiles, and active tombstones."""
        references = (self.moonraker_credential_ref, self.compatibility_credential_ref)
        if self.lifecycle is PrinterLifecycle.REMOVED:
            references_match = all(reference is None for reference in references)
        else:
            references_match = all(reference is not None for reference in references)
        if not references_match:
            raise ValueError("credential references must match lifecycle")
        if references[0] is not None and references[0] == references[1]:
            raise ValueError("credential references must be distinct")
        if self.lifecycle is not PrinterLifecycle.ACTIVE and (
            self.control_enabled or self.dispatch_enabled
        ):
            raise ValueError("inactive printers cannot opt in to controls or dispatch")
        if self.dispatch_enabled and not self.safety_profiles:
            raise ValueError("dispatch requires at least one safety profile")
        if self.dispatch_enabled and not self.identity.capabilities.dispatch_eligible:
            raise ValueError("dispatch requires direct positive capability evidence")
        if self.control_enabled and not self.identity.capabilities.dispatch_eligible:
            raise ValueError("control requires direct positive capability evidence")
        if any(profile.printer_uuid != self.printer_uuid for profile in self.safety_profiles):
            raise ValueError("safety profile printer_uuid must match the registry printer")
        profile_ids = [profile.slicer_profile_id for profile in self.safety_profiles]
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("slicer profile ids must be unique per registry printer")
        if self.updated_at_unix_ms < self.created_at_unix_ms:
            raise ValueError("updated time cannot precede created time")
        if self.identity.observed_at_unix_ms > self.updated_at_unix_ms:
            raise ValueError("identity evidence cannot postdate the record")
        return self

    @property
    def proxy_serial(self) -> str:
        """Return the stable current-Grove serial accepted by ADR 0006."""
        return f"KLOVE-{self.printer_uuid.upper()}"


class RegistryOperationRecord(BaseModel):
    """Secret-free durable reservation, audit evidence, and idempotent result."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operation_version: Literal["1"] = "1"
    idempotency_key: CanonicalUuid4
    operation: RegistryOperationKind
    printer_uuid: CanonicalUuid4
    request_fingerprint: RequestFingerprint
    state: RegistryOperationState
    actor: AuditText
    request_origin: AuditText
    expected_revision: int | None = Field(default=None, ge=1, le=9_223_372_036_854_775_807)
    attempt: int = Field(default=1, ge=1, le=1_000_000)
    new_credential_refs: tuple[CredentialReference, ...] = Field(default=(), max_length=2)
    retired_credential_refs: tuple[CredentialReference, ...] = Field(default=(), max_length=2)
    started_at_unix_ms: int = Field(ge=0, le=9_223_372_036_854_775_807)
    committed_at_unix_ms: int | None = Field(default=None, ge=0, le=9_223_372_036_854_775_807)
    result: RegisteredPrinter | None = None
    error_code: Literal["interrupted"] | None = None

    @field_validator("operation", mode="before")
    @classmethod
    def operation_is_exact_known_text(cls, value: object) -> object:
        """Parse exact enum text without coercing other operation fields."""
        if type(value) is str:
            return RegistryOperationKind(value)
        return value

    @field_validator("state", mode="before")
    @classmethod
    def state_is_exact_known_text(cls, value: object) -> object:
        """Parse exact enum text without coercing other operation fields."""
        if type(value) is str:
            return RegistryOperationState(value)
        return value

    @field_validator("actor", "request_origin")
    @classmethod
    def audit_text_is_exact(cls, value: str) -> str:
        """Prevent control characters or normalization aliases in audit evidence."""
        return _exact_text(value)

    @model_validator(mode="after")
    def operation_state_is_consistent(self) -> RegistryOperationRecord:
        """Require exact payloads for preparing, committed, and aborted states."""
        _validate_operation_references(self)
        _validate_operation_times(self)
        _validate_operation_terminal_state(self)
        return self


def _validate_operation_references(operation: RegistryOperationRecord) -> None:
    if len(set(operation.new_credential_refs)) != len(operation.new_credential_refs):
        raise ValueError("new credential references must be unique")
    if len(set(operation.retired_credential_refs)) != len(operation.retired_credential_refs):
        raise ValueError("retired credential references must be unique")
    if set(operation.new_credential_refs) & set(operation.retired_credential_refs):
        raise ValueError("new and retired credential references must be disjoint")


def _validate_operation_times(operation: RegistryOperationRecord) -> None:
    if operation.committed_at_unix_ms is not None and (
        operation.committed_at_unix_ms < operation.started_at_unix_ms
    ):
        raise ValueError("committed time cannot precede started time")


def _validate_operation_terminal_state(operation: RegistryOperationRecord) -> None:
    if operation.state is RegistryOperationState.PREPARING:
        if (
            operation.result is not None
            or operation.committed_at_unix_ms is not None
            or operation.error_code
        ):
            raise ValueError("preparing operations cannot contain a terminal result")
    elif operation.state is RegistryOperationState.COMMITTED:
        if (
            operation.result is None
            or operation.committed_at_unix_ms is None
            or operation.error_code
        ):
            raise ValueError("committed operations require only a result")
        if operation.result.printer_uuid != operation.printer_uuid:
            raise ValueError("operation result must match its printer")
        if operation.result.updated_at_unix_ms > operation.committed_at_unix_ms:
            raise ValueError("operation result cannot postdate its commit")
        if operation.result.updated_at_unix_ms < operation.started_at_unix_ms:
            raise ValueError("operation result cannot predate its mutation")
    elif operation.result is not None or operation.committed_at_unix_ms is not None:
        raise ValueError("aborted operations cannot contain a committed result")
    elif operation.error_code != "interrupted":
        raise ValueError("aborted operations require an interruption code")


def _exact_text(value: str) -> str:
    if value != value.strip() or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError("text must not contain surrounding whitespace or control characters")
    return value


def _is_loopback(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False
