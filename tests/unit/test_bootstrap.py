from __future__ import annotations

from pathlib import Path

import pytest

from klove.config import PrinterConfig
from klove.domain.onboarding import (
    MoonrakerEndpoint,
    PrinterIdentityEvidence,
    RegisteredPrinter,
    RegistryOperationKind,
)
from klove.orchestration.admission import PrinterAdmissionGates
from klove.orchestration.bootstrap import FileBootstrapError, FileBootstrapImporter
from klove.orchestration.onboarding import PrinterLifecycleService

from ..onboarding_helpers import PRINTER_UUID, identity, make_stores


class Probe:
    def __init__(self) -> None:
        self.calls: list[tuple[MoonrakerEndpoint, str]] = []

    async def probe(
        self,
        endpoint: MoonrakerEndpoint,
        api_key: str,
    ) -> PrinterIdentityEvidence:
        self.calls.append((endpoint, api_key))
        return identity()


class Fences:
    async def clear(self, _printer_uuid: str) -> bool:
        raise AssertionError("bootstrap create must not inspect an existing-printer fence")


class Runtime:
    async def reconcile_committed(self, _record: RegisteredPrinter) -> None:
        return None


def configured_printer(secret_file: Path, **updates: object) -> PrinterConfig:
    values: dict[str, object] = {
        "id": "bootstrap-printer",
        "uuid": PRINTER_UUID,
        "endpoint": "http://127.0.0.1:7125",
        "api_key_file": secret_file,
        "verify_tls": False,
    }
    values.update(updates)
    return PrinterConfig.model_validate(values)


@pytest.mark.asyncio
async def test_file_bootstrap_is_exact_idempotent_and_uses_the_create_contract(
    tmp_path: Path,
) -> None:
    credential_file = tmp_path / "moonraker.token"
    credential_file.write_text("m" * 32, encoding="utf-8")
    secrets, store = make_stores(tmp_path / "state")
    probe = Probe()
    lifecycle = PrinterLifecycleService(
        store,
        secrets,
        probe,
        Fences(),
        admissions=PrinterAdmissionGates(),
        runtime=Runtime(),
        clock_ms=lambda: 1_000,
    )
    importer = FileBootstrapImporter(lifecycle, store)
    config = configured_printer(credential_file)

    first = await importer.import_all((config,))
    second = await importer.import_all((config,))

    assert second == first
    assert len(first) == 1 and first[0] == store.get(PRINTER_UUID)
    assert probe.calls == [(first[0].endpoint, "m" * 32)]
    operations = store.operations(PRINTER_UUID)
    assert len(operations) == 1
    assert operations[0].operation is RegistryOperationKind.BOOTSTRAP_IMPORT
    assert operations[0].idempotency_key[14] == "4"


@pytest.mark.asyncio
async def test_file_bootstrap_rejects_config_or_database_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_file = tmp_path / "moonraker.token"
    credential_file.write_text("m" * 32, encoding="utf-8")
    secrets, store = make_stores(tmp_path / "state")
    lifecycle = PrinterLifecycleService(
        store,
        secrets,
        Probe(),
        Fences(),
        admissions=PrinterAdmissionGates(),
        runtime=Runtime(),
        clock_ms=lambda: 1_000,
    )
    importer = FileBootstrapImporter(lifecycle, store)
    config = configured_printer(credential_file)
    await importer.import_all((config,))

    with pytest.raises(FileBootstrapError):
        await importer.import_all((configured_printer(credential_file, id="renamed"),))

    monkeypatch.setattr(store, "get", lambda _printer_uuid: None)
    with pytest.raises(FileBootstrapError):
        await importer.import_all((config,))


@pytest.mark.asyncio
async def test_file_bootstrap_bounds_unreadable_source_credentials(tmp_path: Path) -> None:
    secrets, store = make_stores(tmp_path / "state")
    lifecycle = PrinterLifecycleService(
        store,
        secrets,
        Probe(),
        Fences(),
        admissions=PrinterAdmissionGates(),
        runtime=Runtime(),
        clock_ms=lambda: 1_000,
    )

    with pytest.raises(FileBootstrapError):
        await FileBootstrapImporter(lifecycle, store).import_all(
            (configured_printer(tmp_path / "missing.token"),)
        )
