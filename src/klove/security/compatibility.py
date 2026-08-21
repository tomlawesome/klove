"""Fail-closed authentication for Grove compatibility protocols."""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from typing import Final

from klove.domain.onboarding import PrinterLifecycle, RegisteredPrinter
from klove.orchestration.admission import PrinterAdmissionGates
from klove.persistence.printer_registry import PrinterStore
from klove.persistence.secret_store import SecretStore

_ACCESS_CODE_PATTERN: Final = re.compile(r"^[A-Za-z0-9_-]{20}$")


@dataclass(frozen=True, slots=True)
class CompatibilityPrincipal:
    """Secret-free identity established from one current canonical record."""

    printer_uuid: str
    proxy_serial: str
    record_revision: int
    control_enabled: bool
    dispatch_enabled: bool


class CompatibilityAuthenticator:
    """Resolve Grove MQTT and FTPS credentials without an identity oracle."""

    def __init__(
        self,
        store: PrinterStore,
        secrets: SecretStore,
        admissions: PrinterAdmissionGates,
    ) -> None:
        self._store = store
        self._secrets = secrets
        self._admissions = admissions

    async def authenticate_mqtt(
        self,
        proxy_serial: str,
        access_code: str,
    ) -> CompatibilityPrincipal | None:
        """Authenticate one exact MQTT serial and access-code pair."""
        if type(proxy_serial) is not str:
            return None
        return await self._authenticate(access_code, proxy_serial=proxy_serial)

    async def authenticate_ftps(self, access_code: str) -> CompatibilityPrincipal | None:
        """Resolve one unique active printer from its exact FTPS access code."""
        return await self._authenticate(access_code, proxy_serial=None)

    async def revalidate(self, principal: CompatibilityPrincipal) -> bool:
        """Confirm that a session principal still names the exact active revision."""
        if type(principal) is not CompatibilityPrincipal or not self._principal_is_shaped(
            principal
        ):
            return False
        async with self._admissions.hold(principal.printer_uuid):
            try:
                record = self._store.get(principal.printer_uuid)
            except Exception:
                return False
            if record is None or not self._principal_matches(record, principal):
                return False
            reference = record.compatibility_credential_ref
            if reference is None:
                return False
            return self._read_valid_secret(reference) is not None

    async def _authenticate(
        self,
        access_code: str,
        *,
        proxy_serial: str | None,
    ) -> CompatibilityPrincipal | None:
        candidate = (
            self._unique_match(access_code, proxy_serial=proxy_serial)
            if self._valid_access_code(access_code)
            else None
        )
        if candidate is None:
            return None
        async with self._admissions.hold(candidate.printer_uuid):
            return self._current_principal(
                candidate,
                access_code,
                proxy_serial=proxy_serial,
            )

    def _current_principal(
        self,
        candidate: RegisteredPrinter,
        access_code: str,
        *,
        proxy_serial: str | None,
    ) -> CompatibilityPrincipal | None:
        if self._unique_match(access_code, proxy_serial=proxy_serial) != candidate:
            return None
        try:
            current = self._store.get(candidate.printer_uuid)
        except Exception:
            return None
        if current != candidate or not self._eligible(current, proxy_serial=proxy_serial):
            return None
        reference = current.compatibility_credential_ref
        if reference is None:
            return None
        stored = self._read_valid_secret(reference)
        if stored is None or not hmac.compare_digest(access_code, stored):
            return None
        return CompatibilityPrincipal(
            printer_uuid=current.printer_uuid,
            proxy_serial=current.proxy_serial,
            record_revision=current.revision,
            control_enabled=current.control_enabled,
            dispatch_enabled=current.dispatch_enabled,
        )

    def _unique_match(
        self,
        access_code: str,
        *,
        proxy_serial: str | None,
    ) -> RegisteredPrinter | None:
        try:
            records = self._store.list(include_removed=True)
        except Exception:
            return None
        if any(type(record) is not RegisteredPrinter for record in records):
            return None
        if self._records_collide(records):
            return None
        matches: list[RegisteredPrinter] = []
        for record in records:
            if not self._eligible(record, proxy_serial=proxy_serial):
                continue
            reference = record.compatibility_credential_ref
            if reference is None:
                return None
            stored = self._read_valid_secret(reference)
            if stored is None:
                return None
            if hmac.compare_digest(access_code, stored):
                matches.append(record)
        return matches[0] if len(matches) == 1 else None

    def _read_valid_secret(self, reference: str) -> str | None:
        try:
            stored = self._secrets.read(reference, minimum_length=20)
        except Exception:
            return None
        return stored if self._valid_access_code(stored) else None

    @staticmethod
    def _records_collide(records: tuple[RegisteredPrinter, ...]) -> bool:
        printer_ids: set[str] = set()
        credential_refs: set[str] = set()
        for record in records:
            if record.printer_uuid in printer_ids:
                return True
            printer_ids.add(record.printer_uuid)
            reference = record.compatibility_credential_ref
            if reference is not None:
                if reference in credential_refs:
                    return True
                credential_refs.add(reference)
        return False

    @staticmethod
    def _eligible(record: RegisteredPrinter, *, proxy_serial: str | None) -> bool:
        return record.lifecycle is PrinterLifecycle.ACTIVE and (
            proxy_serial is None or hmac.compare_digest(record.proxy_serial, proxy_serial)
        )

    @staticmethod
    def _principal_matches(
        record: RegisteredPrinter,
        principal: CompatibilityPrincipal,
    ) -> bool:
        return (
            record.lifecycle is PrinterLifecycle.ACTIVE
            and hmac.compare_digest(record.printer_uuid, principal.printer_uuid)
            and hmac.compare_digest(record.proxy_serial, principal.proxy_serial)
            and record.revision == principal.record_revision
            and record.control_enabled is principal.control_enabled
            and record.dispatch_enabled is principal.dispatch_enabled
        )

    @staticmethod
    def _principal_is_shaped(principal: CompatibilityPrincipal) -> bool:
        return (
            type(principal.printer_uuid) is str
            and type(principal.proxy_serial) is str
            and type(principal.record_revision) is int
            and type(principal.control_enabled) is bool
            and type(principal.dispatch_enabled) is bool
        )

    @staticmethod
    def _valid_access_code(access_code: object) -> bool:
        return type(access_code) is str and _ACCESS_CODE_PATTERN.fullmatch(access_code) is not None
