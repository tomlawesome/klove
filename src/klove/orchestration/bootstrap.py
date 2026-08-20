"""One-time idempotent import of file-configured printers into the registry."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path

from pydantic import SecretStr

from klove.config import PrinterConfig, read_secret
from klove.domain.onboarding import MoonrakerEndpoint, RegisteredPrinter
from klove.domain.onboarding_requests import CreatePrinterRequest
from klove.errors import KloveError
from klove.orchestration.onboarding import PrinterLifecycleService
from klove.persistence.printer_registry import PrinterStore

_BOOTSTRAP_KEY_DOMAIN = b"klove:file-bootstrap:v1\0"


class FileBootstrapError(KloveError):
    """File bootstrap could not agree exactly with canonical durable state."""


class FileBootstrapImporter:
    """Import each configured printer once without retaining a second registry."""

    def __init__(
        self,
        lifecycle: PrinterLifecycleService,
        store: PrinterStore,
        *,
        secret_loader: Callable[[Path], str] = read_secret,
    ) -> None:
        self._lifecycle = lifecycle
        self._store = store
        self._secret_loader = secret_loader

    async def import_all(
        self,
        printers: Iterable[PrinterConfig],
    ) -> tuple[RegisteredPrinter, ...]:
        """Import stable file input or fail closed on any database disagreement."""
        imported: list[RegisteredPrinter] = []
        try:
            for printer in sorted(printers, key=lambda candidate: str(candidate.uuid)):
                request = _request(printer, self._secret_loader(printer.api_key_file))
                result = await self._lifecycle.bootstrap_import(request)
                if self._store.get(str(printer.uuid)) != result:
                    raise FileBootstrapError
                imported.append(result)
        except FileBootstrapError:
            raise
        except Exception as exc:
            raise FileBootstrapError from exc
        return tuple(imported)


def _request(printer: PrinterConfig, credential: str) -> CreatePrinterRequest:
    return CreatePrinterRequest(
        idempotency_key=_idempotency_key(str(printer.uuid)),
        printer_uuid=str(printer.uuid),
        actor="bootstrap:file-config",
        request_origin="local:file-config",
        display_name=printer.id,
        endpoint=MoonrakerEndpoint(
            url=printer.endpoint,
            allow_insecure_http=printer.allow_insecure_http,
            verify_tls=printer.verify_tls and printer.endpoint.startswith("https://"),
        ),
        moonraker_credential=SecretStr(credential),
        safety_profiles=printer.safety_profiles,
        control_enabled=printer.control_enabled,
        dispatch_enabled=printer.dispatch_enabled,
    )


def _idempotency_key(printer_uuid: str) -> str:
    digest = bytearray(
        hashlib.sha256(_BOOTSTRAP_KEY_DOMAIN + printer_uuid.encode("ascii")).digest()[:16]
    )
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))
