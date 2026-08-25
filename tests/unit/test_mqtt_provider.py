from __future__ import annotations

import json
from typing import Any, cast

import pytest

from klove.domain.discovery import discover_capabilities
from klove.domain.models import PrinterPhase, PrinterSnapshot
from klove.northbound.mqtt.provider import RegistryMqttReportProvider
from klove.registry import PrinterRegistry
from klove.security.compatibility import CompatibilityPrincipal

PRINTER = "11111111-1111-4111-8111-111111111111"
OTHER_PRINTER = "22222222-2222-4222-8222-222222222222"
SERIAL = "KLOVE-11111111-1111-4111-8111-111111111111"
CAPABILITIES = discover_capabilities({"pause_resume", "print_stats", "virtual_sdcard"})
EMPTY_REPORT = b'{"print":{"command":"push_status"}}'


def principal(**updates: object) -> CompatibilityPrincipal:
    values: dict[str, object] = {
        "printer_uuid": PRINTER,
        "proxy_serial": SERIAL,
        "record_revision": 1,
        "control_enabled": True,
        "dispatch_enabled": False,
    }
    values.update(updates)
    return CompatibilityPrincipal(**cast(Any, values))


def snapshot(**updates: object) -> PrinterSnapshot:
    values: dict[str, object] = {
        "printer_id": PRINTER,
        "revision": 1,
        "control_revision": 1,
        "connected": True,
        "phase": PrinterPhase.IDLE,
        "reason": "observed",
        "eventtime": 12.5,
        "capabilities": CAPABILITIES,
        "job": None,
        "status": {
            "print_stats": {"state": "standby", "filename": ""},
            "virtual_sdcard": {"progress": 0.0},
        },
    }
    values.update(updates)
    return PrinterSnapshot.model_construct(**cast(Any, values))


class BrokenSnapshots:
    async def get(self, _printer_id: str) -> PrinterSnapshot | None:
        raise RuntimeError("unavailable")


@pytest.mark.parametrize(
    "authenticated",
    [
        cast(CompatibilityPrincipal, object()),
        principal(printer_uuid=cast(str, object())),
    ],
)
async def test_malformed_principal_never_queries_a_snapshot(
    authenticated: CompatibilityPrincipal,
) -> None:
    registry = PrinterRegistry([PRINTER])

    assert await RegistryMqttReportProvider(registry).report(authenticated) == EMPTY_REPORT


async def test_provider_projects_only_the_authenticated_printers_current_snapshot() -> None:
    registry = PrinterRegistry([PRINTER])
    await registry.replace(snapshot())

    payload = await RegistryMqttReportProvider(registry).report(principal())

    assert json.loads(payload) == {
        "print": {
            "command": "push_status",
            "gcode_file": "",
            "gcode_state": "IDLE",
            "mc_percent": 0,
        }
    }


@pytest.mark.parametrize(
    "current",
    [
        None,
        snapshot(printer_id=OTHER_PRINTER),
        snapshot(reason="stale"),
    ],
)
async def test_missing_mismatched_or_stale_snapshot_never_claims_printer_state(
    current: PrinterSnapshot | None,
) -> None:
    class Snapshots:
        async def get(self, _printer_id: str) -> PrinterSnapshot | None:
            return current

    assert await RegistryMqttReportProvider(Snapshots()).report(principal()) == EMPTY_REPORT


async def test_provider_failure_is_non_enumerating() -> None:
    assert await RegistryMqttReportProvider(BrokenSnapshots()).report(principal()) == EMPTY_REPORT
