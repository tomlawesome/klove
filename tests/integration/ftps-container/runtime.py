"""Seed one real bootstrap record, then expose the production FTPS runtime."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import Path

from pydantic import SecretStr

from klove.app import serve
from klove.config import load_config
from klove.domain.discovery import discover_capabilities
from klove.domain.onboarding import MoonrakerEndpoint, PrinterIdentityEvidence, RegisteredPrinter
from klove.domain.onboarding_requests import CreatePrinterRequest
from klove.ftps.server import FtpsTlsServer
from klove.orchestration.admission import PrinterAdmissionGates
from klove.orchestration.onboarding import PrinterLifecycleService
from klove.persistence.printer_registry import PrinterStore
from klove.persistence.secret_store import SecretStore

CONFIG = Path("/run/klove-secrets/config.toml")
CERTIFICATE = Path("/run/klove-secrets/tls-certificate.pem")
PRINTER_UUID = "11111111-1111-4111-8111-111111111111"
IDEMPOTENCY_KEY = "33333333-3333-4333-8333-333333333333"


class _Probe:
    async def probe(
        self,
        _endpoint: MoonrakerEndpoint,
        _api_key: str,
    ) -> PrinterIdentityEvidence:
        objects = {"extruder", "fan", "heater_bed", "pause_resume", "print_stats", "virtual_sdcard"}
        return PrinterIdentityEvidence(
            server_hostname="ftps-contract-moonraker",
            klipper_hostname="ftps-contract-klipper",
            moonraker_version="fixture",
            klipper_version="fixture",
            capabilities=discover_capabilities(objects),
            observed_at_unix_ms=1,
        )


class _Fences:
    async def clear(self, _printer_uuid: str) -> bool:
        raise RuntimeError("bootstrap create must not inspect an existing-printer fence")


class _Runtime:
    async def reconcile_committed(self, _record: RegisteredPrinter) -> None:
        return None


def _access_code() -> str:
    """Derive a run-specific test credential without persisting it in the fixture."""
    material = hashlib.sha256(CERTIFICATE.read_bytes()).digest()[:15]
    return base64.urlsafe_b64encode(material).decode("ascii")


def _compatibility_entropy(length: int) -> bytes:
    material = hashlib.sha256(CERTIFICATE.read_bytes()).digest()
    if length != 15:
        raise RuntimeError("unexpected compatibility entropy request")
    return material[:length]


async def _seed_bootstrap_record() -> None:
    config = load_config(CONFIG)
    secrets = SecretStore(config.registry.secret_directory)
    store = PrinterStore(config.registry.database_file, secrets)
    store.initialize()
    service = PrinterLifecycleService(
        store,
        secrets,
        _Probe(),
        _Fences(),
        admissions=PrinterAdmissionGates(),
        runtime=_Runtime(),
        random_bytes=_compatibility_entropy,
        clock_ms=lambda: 1_000,
    )
    record = await service.bootstrap_import(
        CreatePrinterRequest(
            idempotency_key=IDEMPOTENCY_KEY,
            printer_uuid=PRINTER_UUID,
            actor="bootstrap:ftps-container-contract",
            request_origin="local:ftps-container-contract",
            display_name="FTPS contract printer",
            endpoint=MoonrakerEndpoint(url="http://127.0.0.1:7125", verify_tls=False),
            moonraker_credential=SecretStr("fixture-moonraker-credential-0001"),
        )
    )
    reference = record.compatibility_credential_ref
    if reference is None or secrets.read(reference, minimum_length=20) != _access_code():
        raise RuntimeError("bootstrap compatibility credential mismatch")


original_start = FtpsTlsServer.start


async def delayed_start(server: FtpsTlsServer) -> None:
    await asyncio.sleep(1)
    await original_start(server)


async def main() -> None:
    # Use the production lifecycle to create the canonical registry and secret state.
    await _seed_bootstrap_record()
    FtpsTlsServer.start = delayed_start  # type: ignore[assignment,method-assign]
    await serve(CONFIG)


asyncio.run(main())
