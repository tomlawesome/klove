from __future__ import annotations

from pathlib import Path

from klove.domain.artifacts import (
    ArtifactCompatibility,
    BuildVolume,
    GcodeFlavor,
    SafetyProfile,
)
from klove.domain.discovery import discover_capabilities
from klove.domain.onboarding import (
    MoonrakerEndpoint,
    PrinterIdentityEvidence,
    PrinterLifecycle,
    RegisteredPrinter,
    RegistryOperationKind,
    RegistryOperationRecord,
    RegistryOperationState,
)
from klove.persistence.printer_registry import PrinterStore
from klove.persistence.secret_store import SecretStore

PRINTER_UUID = "11111111-1111-4111-8111-111111111111"
OTHER_PRINTER_UUID = "22222222-2222-4222-8222-222222222222"
IDEMPOTENCY_KEY = "33333333-3333-4333-8333-333333333333"
OTHER_IDEMPOTENCY_KEY = "44444444-4444-4444-8444-444444444444"
MOONRAKER_REF = "credential-55555555-5555-4555-8555-555555555555"
COMPATIBILITY_REF = "credential-66666666-6666-4666-8666-666666666666"
NEW_MOONRAKER_REF = "credential-77777777-7777-4777-8777-777777777777"
NEW_COMPATIBILITY_REF = "credential-88888888-8888-4888-8888-888888888888"
OTHER_MOONRAKER_REF = "credential-99999999-9999-4999-8999-999999999999"
OTHER_COMPATIBILITY_REF = "credential-aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"


def safety_profile(
    *,
    printer_uuid: str = PRINTER_UUID,
    slicer_profile_id: str = "klipper-voron-24-0.4",
) -> SafetyProfile:
    return SafetyProfile(
        printer_uuid=printer_uuid,
        generation=1,
        slicer_profile_id=slicer_profile_id,
        compatibility=ArtifactCompatibility(
            gcode_flavor=GcodeFlavor.KLIPPER,
            nozzle_diameter_micrometres=400,
            build_volume=BuildVolume(
                x_micrometres=350_000,
                y_micrometres=350_000,
                z_micrometres=350_000,
            ),
            build_plate_id="textured-pei",
        ),
    )


def identity(*, eligible: bool = True) -> PrinterIdentityEvidence:
    objects = {"extruder", "heater_bed", "fan"}
    if eligible:
        objects.update({"pause_resume", "print_stats", "virtual_sdcard"})
    return PrinterIdentityEvidence(
        server_hostname="moonraker",
        klipper_hostname="klipper",
        moonraker_version="v0.9.3",
        klipper_version="v0.12.0",
        capabilities=discover_capabilities(objects),
        observed_at_unix_ms=900,
    )


def printer(**updates: object) -> RegisteredPrinter:
    values: dict[str, object] = {
        "printer_uuid": PRINTER_UUID,
        "display_name": "Workshop Voron",
        "endpoint": MoonrakerEndpoint(
            url="http://127.0.0.1:7125",
            verify_tls=False,
        ),
        "lifecycle": PrinterLifecycle.ACTIVE,
        "moonraker_credential_ref": MOONRAKER_REF,
        "compatibility_credential_ref": COMPATIBILITY_REF,
        "identity": identity(),
        "safety_profiles": (safety_profile(),),
        "control_enabled": True,
        "dispatch_enabled": True,
        "revision": 1,
        "created_at_unix_ms": 1_000,
        "updated_at_unix_ms": 1_000,
    }
    values.update(updates)
    return RegisteredPrinter.model_validate(values)


def operation(**updates: object) -> RegistryOperationRecord:
    values: dict[str, object] = {
        "idempotency_key": IDEMPOTENCY_KEY,
        "operation": RegistryOperationKind.CREATE,
        "printer_uuid": PRINTER_UUID,
        "request_fingerprint": "a" * 64,
        "state": RegistryOperationState.PREPARING,
        "actor": "owner:local",
        "request_origin": "https://grove.example.test",
        "expected_revision": None,
        "new_credential_refs": (MOONRAKER_REF, COMPATIBILITY_REF),
        "retired_credential_refs": (),
        "started_at_unix_ms": 950,
    }
    values.update(updates)
    return RegistryOperationRecord.model_validate(values)


def make_stores(tmp_path: Path) -> tuple[SecretStore, PrinterStore]:
    secret_directory = tmp_path / "registry-secrets"
    secret_directory.mkdir(mode=0o700, parents=True)
    secrets = SecretStore(secret_directory, random_bytes=lambda length: b"k" * length)
    store = PrinterStore(tmp_path / "registry.sqlite3", secrets)
    store.initialize()
    return secrets, store


def write_printer_secrets(
    secrets: SecretStore,
    *,
    moonraker_ref: str = MOONRAKER_REF,
    compatibility_ref: str = COMPATIBILITY_REF,
) -> None:
    secrets.write(moonraker_ref, "m" * 32, minimum_length=32)
    secrets.write(compatibility_ref, "c" * 20, minimum_length=20)
