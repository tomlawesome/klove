from __future__ import annotations

import asyncio
from typing import cast

import pytest

from klove.domain.onboarding import PrinterLifecycle, RegisteredPrinter
from klove.orchestration.admission import PrinterAdmissionGates
from klove.orchestration.completion import CompletionHandoffError, CompletionHandoffService

from ..onboarding_helpers import COMPATIBILITY_REF, NEW_COMPATIBILITY_REF, PRINTER_UUID, printer

ACCESS_CODE = "A" * 20


class FakeStore:
    def __init__(self, record: RegisteredPrinter | None) -> None:
        self.record = record
        self.requested: list[str] = []

    def get(self, printer_uuid: str) -> RegisteredPrinter | None:
        self.requested.append(printer_uuid)
        return self.record


class FakeSecrets:
    def __init__(self, value: object = ACCESS_CODE) -> None:
        self.value = value
        self.requests: list[tuple[str, int]] = []

    def read(self, reference: str, *, minimum_length: int) -> str:
        self.requests.append((reference, minimum_length))
        if isinstance(self.value, Exception):
            raise self.value
        return self.value  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_completion_releases_only_the_exact_active_bound_create_record() -> None:
    record = printer(display_name="Workshop printer", revision=7)
    store = FakeStore(record)
    secrets = FakeSecrets()
    service = CompletionHandoffService(
        store, secrets, "klove.example.test", PrinterAdmissionGates()
    )

    bundle = await service.issue(PRINTER_UUID, 7)

    assert bundle.name == "Workshop printer"
    assert bundle.serial_number == f"KLOVE-{PRINTER_UUID.upper()}"
    assert bundle.ip_address == "klove.example.test"
    assert bundle.access_code == ACCESS_CODE
    assert store.requested == [PRINTER_UUID]
    assert secrets.requests == [(COMPATIBILITY_REF, 20)]
    assert ACCESS_CODE not in repr(bundle)


@pytest.mark.parametrize(
    ("record", "printer_uuid", "revision", "secret"),
    [
        (None, PRINTER_UUID, 1, ACCESS_CODE),
        (printer(), cast(str, True), 1, ACCESS_CODE),
        (printer(), "not-a-printer-uuid", 1, ACCESS_CODE),
        (printer(), "22222222-2222-4222-8222-222222222222", 1, ACCESS_CODE),
        (printer(), PRINTER_UUID, 0, ACCESS_CODE),
        (printer(), PRINTER_UUID, True, ACCESS_CODE),
        (printer(), PRINTER_UUID, 9_223_372_036_854_775_808, ACCESS_CODE),
        (printer(revision=2), PRINTER_UUID, 1, ACCESS_CODE),
        (
            printer(
                lifecycle=PrinterLifecycle.DISABLED,
                control_enabled=False,
                dispatch_enabled=False,
            ),
            PRINTER_UUID,
            1,
            ACCESS_CODE,
        ),
        (
            printer(
                lifecycle=PrinterLifecycle.REMOVED,
                moonraker_credential_ref=None,
                compatibility_credential_ref=None,
                control_enabled=False,
                dispatch_enabled=False,
            ),
            PRINTER_UUID,
            1,
            ACCESS_CODE,
        ),
        (printer(), PRINTER_UUID, 1, "short"),
        (printer(), PRINTER_UUID, 1, "=" * 20),
        (printer(), PRINTER_UUID, 1, RuntimeError("secret backend failure")),
    ],
)
@pytest.mark.asyncio
async def test_completion_fails_closed_without_releasing_a_code(
    record: RegisteredPrinter | None,
    printer_uuid: str,
    revision: int,
    secret: object,
) -> None:
    service = CompletionHandoffService(
        FakeStore(record),
        FakeSecrets(secret),
        "192.0.2.20",
        PrinterAdmissionGates(),
    )

    with pytest.raises(CompletionHandoffError) as denied:
        await service.issue(printer_uuid, revision)

    assert "secret backend failure" not in str(denied.value)


@pytest.mark.parametrize(
    "host",
    [
        "",
        "KLOVE.example.test",
        "klove.example.test.",
        "klove.example.test:80",
        "https://klove.example.test",
        "[::1]",
        "::1",
        "192.168.001.1",
        " 192.0.2.1",
        "klove.example.test/path",
    ],
)
def test_completion_rejects_any_noncanonical_compatibility_host(host: str) -> None:
    with pytest.raises(ValueError, match="compatibility host"):
        CompletionHandoffService(FakeStore(printer()), FakeSecrets(), host, PrinterAdmissionGates())


@pytest.mark.parametrize("host", ["klove.example.test", "192.0.2.20"])
@pytest.mark.asyncio
async def test_completion_accepts_only_canonical_dns_or_ipv4_hosts(host: str) -> None:
    service = CompletionHandoffService(
        FakeStore(printer()), FakeSecrets(), host, PrinterAdmissionGates()
    )

    assert (await service.issue(PRINTER_UUID, 1)).ip_address == host


@pytest.mark.asyncio
async def test_completion_serializes_with_the_shared_printer_gate_and_uses_current_state() -> None:
    store = FakeStore(printer(revision=1))
    secrets = FakeSecrets(ACCESS_CODE)
    admissions = PrinterAdmissionGates()
    service = CompletionHandoffService(store, secrets, "klove.example.test", admissions)

    async with admissions.hold(PRINTER_UUID):
        stale = asyncio.create_task(service.issue(PRINTER_UUID, 1))
        await asyncio.sleep(0)
        assert not stale.done()
        store.record = printer(revision=2, compatibility_credential_ref=NEW_COMPATIBILITY_REF)
        secrets.value = "B" * 20

    with pytest.raises(CompletionHandoffError):
        await stale
    current = await service.issue(PRINTER_UUID, 2)
    assert current.access_code == "B" * 20
    assert secrets.requests[-1] == (NEW_COMPATIBILITY_REF, 20)
