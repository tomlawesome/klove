"""One-flow release of the exact Grove compatibility create bundle."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Protocol

from klove.domain.onboarding import PrinterLifecycle, RegisteredPrinter
from klove.errors import KloveError
from klove.orchestration.admission import PrinterAdmissionGates

_ACCESS_CODE = re.compile(r"[A-Za-z0-9_-]{20}")
_CANONICAL_UUID4 = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_DNS_HOST = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)


class CompletionRecordStore(Protocol):
    """Read only the exact canonical record committed by a framed create."""

    def get(self, printer_uuid: str) -> RegisteredPrinter | None: ...


class CompletionSecretStore(Protocol):
    """Read one owner-only compatibility credential without exposing references."""

    def read(self, reference: str, *, minimum_length: int) -> str: ...


class CompletionHandoffError(KloveError):
    """The one-time compatibility bundle cannot be proven or released."""

    def __init__(self) -> None:
        super().__init__("completion handoff denied")


@dataclass(frozen=True, slots=True, repr=False)
class CompletionBundle:
    """The sole secret-bearing document permitted to cross to Grove once."""

    name: str
    serial_number: str
    ip_address: str
    access_code: str

    def __repr__(self) -> str:
        return "CompletionBundle(<redacted>)"


class CompletionHandoffService:
    """Release one current active printer's compatibility secret after a bound create."""

    def __init__(
        self,
        store: CompletionRecordStore,
        secrets: CompletionSecretStore,
        compatibility_host: str,
        admissions: PrinterAdmissionGates,
    ) -> None:
        if not _valid_compatibility_host(compatibility_host):
            raise ValueError("compatibility host is invalid")
        self._store = store
        self._secrets = secrets
        self._compatibility_host = compatibility_host
        self._admissions = admissions

    async def issue(self, printer_uuid: str, revision: int) -> CompletionBundle:
        """Return only the exact active create result and its one compatibility code."""
        if (
            type(printer_uuid) is not str
            or _CANONICAL_UUID4.fullmatch(printer_uuid) is None
            or type(revision) is not int
            or isinstance(revision, bool)
            or not 1 <= revision <= 9_223_372_036_854_775_807
        ):
            raise CompletionHandoffError
        async with self._admissions.hold(printer_uuid):
            try:
                record = self._store.get(printer_uuid)
                if (
                    record is None
                    or record.printer_uuid != printer_uuid
                    or record.revision != revision
                    or record.lifecycle is not PrinterLifecycle.ACTIVE
                    or record.compatibility_credential_ref is None
                ):
                    raise ValueError
                access_code = self._secrets.read(
                    record.compatibility_credential_ref,
                    minimum_length=20,
                )
                if _ACCESS_CODE.fullmatch(access_code) is None:
                    raise ValueError
            except Exception as exc:
                raise CompletionHandoffError from exc
            return CompletionBundle(
                name=record.display_name,
                serial_number=record.proxy_serial,
                ip_address=self._compatibility_host,
                access_code=access_code,
            )


def _valid_compatibility_host(value: object) -> bool:
    if type(value) is not str or not value or len(value) > 253 or not value.isascii():
        return False
    try:
        return isinstance(ipaddress.ip_address(value), ipaddress.IPv4Address)
    except ValueError:
        if all(label.isdecimal() for label in value.split(".")):
            return False
        return _DNS_HOST.fullmatch(value) is not None
