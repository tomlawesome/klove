from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import cast

import pytest

from klove.domain.onboarding import PrinterLifecycle, RegisteredPrinter
from klove.orchestration.admission import PrinterAdmissionGates
from klove.persistence.printer_registry import PrinterStore
from klove.persistence.secret_store import SecretStore
from klove.security.compatibility import (
    CompatibilityAuthenticator,
    CompatibilityPrincipal,
)

from ..onboarding_helpers import (
    COMPATIBILITY_REF,
    OTHER_COMPATIBILITY_REF,
    OTHER_MOONRAKER_REF,
    OTHER_PRINTER_UUID,
    PRINTER_UUID,
    printer,
    safety_profile,
)

ACCESS_CODE = "Abcd_1234-Efgh_56789"
OTHER_ACCESS_CODE = "Other_1234-Code_5678"


class FakeStore:
    def __init__(self, records: tuple[object, ...]) -> None:
        self.records = records
        self.list_failure: Exception | None = None
        self.get_failure: Exception | None = None

    def list(self, *, include_removed: bool = False) -> tuple[RegisteredPrinter, ...]:
        assert include_removed
        if self.list_failure is not None:
            raise self.list_failure
        return cast(tuple[RegisteredPrinter, ...], self.records)

    def get(self, printer_uuid: str) -> RegisteredPrinter | None:
        if self.get_failure is not None:
            raise self.get_failure
        matches = [
            value
            for value in self.records
            if type(value) is RegisteredPrinter and value.printer_uuid == printer_uuid
        ]
        return matches[0] if len(matches) == 1 else None


class FakeSecrets:
    def __init__(self, values: dict[str, object]) -> None:
        self.values = values
        self.reads: list[tuple[str, int]] = []
        self.first_read = asyncio.Event()

    def read(self, reference: str, *, minimum_length: int) -> str:
        self.reads.append((reference, minimum_length))
        self.first_read.set()
        value = self.values[reference]
        if isinstance(value, Exception):
            raise value
        return cast(str, value)


def other_printer(**updates: object) -> RegisteredPrinter:
    values: dict[str, object] = {
        "printer_uuid": OTHER_PRINTER_UUID,
        "display_name": "Other printer",
        "moonraker_credential_ref": OTHER_MOONRAKER_REF,
        "compatibility_credential_ref": OTHER_COMPATIBILITY_REF,
        "safety_profiles": (
            safety_profile(
                printer_uuid=OTHER_PRINTER_UUID,
                slicer_profile_id="other-klipper-profile",
            ),
        ),
    }
    values.update(updates)
    return printer(**values)


def authenticator(
    records: tuple[object, ...] | None = None,
    values: dict[str, object] | None = None,
    *,
    gates: PrinterAdmissionGates | None = None,
) -> tuple[CompatibilityAuthenticator, FakeStore, FakeSecrets]:
    store = FakeStore(records or (printer(),))
    secrets = FakeSecrets(values or {COMPATIBILITY_REF: ACCESS_CODE})
    auth = CompatibilityAuthenticator(
        cast(PrinterStore, store),
        cast(SecretStore, secrets),
        gates or PrinterAdmissionGates(),
    )
    return auth, store, secrets


def expected_principal(record: RegisteredPrinter | None = None) -> CompatibilityPrincipal:
    selected = record or printer()
    return CompatibilityPrincipal(
        printer_uuid=selected.printer_uuid,
        proxy_serial=selected.proxy_serial,
        record_revision=selected.revision,
        control_enabled=selected.control_enabled,
        dispatch_enabled=selected.dispatch_enabled,
    )


@pytest.mark.asyncio
async def test_mqtt_requires_exact_serial_and_secret_and_returns_safe_principal() -> None:
    record = printer()
    auth, _, secrets = authenticator()

    assert await auth.authenticate_mqtt(record.proxy_serial, ACCESS_CODE) == expected_principal()
    assert await auth.authenticate_mqtt(record.proxy_serial.lower(), ACCESS_CODE) is None
    assert await auth.authenticate_mqtt(record.proxy_serial, OTHER_ACCESS_CODE) is None
    assert await auth.authenticate_mqtt(cast(str, b"not-text"), ACCESS_CODE) is None
    assert secrets.reads == [(COMPATIBILITY_REF, 20)] * 4


@pytest.mark.asyncio
async def test_ftps_resolves_exactly_one_active_secret_after_scanning_all_candidates() -> None:
    other = other_printer()
    auth, _, secrets = authenticator(
        (printer(), other),
        {
            COMPATIBILITY_REF: ACCESS_CODE,
            OTHER_COMPATIBILITY_REF: OTHER_ACCESS_CODE,
        },
    )

    assert await auth.authenticate_ftps(OTHER_ACCESS_CODE) == expected_principal(other)
    assert secrets.reads[:2] == [
        (COMPATIBILITY_REF, 20),
        (OTHER_COMPATIBILITY_REF, 20),
    ]


@pytest.mark.asyncio
async def test_duplicate_secret_or_duplicate_record_is_a_denial() -> None:
    other = other_printer()
    shared: dict[str, object] = {
        COMPATIBILITY_REF: ACCESS_CODE,
        OTHER_COMPATIBILITY_REF: ACCESS_CODE,
    }
    auth, _, secrets = authenticator((printer(), other), shared)
    duplicate, _, _ = authenticator((printer(), printer()))

    assert await auth.authenticate_ftps(ACCESS_CODE) is None
    assert await duplicate.authenticate_mqtt(printer().proxy_serial, ACCESS_CODE) is None
    assert len(secrets.reads) == 2

    shared_reference = other.model_copy(update={"compatibility_credential_ref": COMPATIBILITY_REF})
    reference_collision, _, collision_secrets = authenticator((printer(), shared_reference))
    assert await reference_collision.authenticate_ftps(ACCESS_CODE) is None
    assert collision_secrets.reads == []


@pytest.mark.asyncio
async def test_inactive_records_never_authenticate_or_read_secrets() -> None:
    disabled = printer(
        lifecycle=PrinterLifecycle.DISABLED,
        control_enabled=False,
        dispatch_enabled=False,
    )
    removed = printer(
        lifecycle=PrinterLifecycle.REMOVED,
        moonraker_credential_ref=None,
        compatibility_credential_ref=None,
        control_enabled=False,
        dispatch_enabled=False,
    )
    auth, _, secrets = authenticator((disabled, removed))

    assert await auth.authenticate_ftps(ACCESS_CODE) is None
    assert await auth.authenticate_mqtt(disabled.proxy_serial, ACCESS_CODE) is None
    assert secrets.reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "access_code",
    [None, b"a" * 20, "a" * 19, "a" * 21, "a" * 19 + "!", "a" * 19 + "é"],
)
async def test_malformed_access_codes_are_indistinguishable_denials(access_code: object) -> None:
    auth, _, secrets = authenticator()

    assert await auth.authenticate_ftps(cast(str, access_code)) is None
    assert secrets.reads == []


@pytest.mark.asyncio
async def test_store_and_secret_failures_and_invalid_records_are_denials() -> None:
    auth, store, _ = authenticator()
    store.list_failure = RuntimeError("private registry detail")
    assert await auth.authenticate_ftps(ACCESS_CODE) is None

    invalid_auth, _, _ = authenticator((object(),))
    assert await invalid_auth.authenticate_ftps(ACCESS_CODE) is None

    for value in (KeyError(COMPATIBILITY_REF), "short", b"a" * 20):
        failed, _, _ = authenticator(values={COMPATIBILITY_REF: value})
        assert await failed.authenticate_ftps(ACCESS_CODE) is None

    missing_reference = printer().model_copy(update={"compatibility_credential_ref": None})
    missing_auth, _, _ = authenticator((missing_reference,))
    assert await missing_auth.authenticate_ftps(ACCESS_CODE) is None


@pytest.mark.asyncio
async def test_unreadable_active_ftps_candidate_fails_closed_even_when_another_matches() -> None:
    auth, _, secrets = authenticator(
        (printer(), other_printer()),
        {
            COMPATIBILITY_REF: ACCESS_CODE,
            OTHER_COMPATIBILITY_REF: RuntimeError("private secret detail"),
        },
    )

    assert await auth.authenticate_ftps(ACCESS_CODE) is None
    assert len(secrets.reads) == 2


@pytest.mark.asyncio
async def test_mqtt_does_not_inspect_a_different_serials_secret() -> None:
    other = other_printer()
    auth, _, secrets = authenticator(
        (printer(), other),
        {
            COMPATIBILITY_REF: ACCESS_CODE,
            OTHER_COMPATIBILITY_REF: RuntimeError("unrelated secret"),
        },
    )

    assert await auth.authenticate_mqtt(printer().proxy_serial, ACCESS_CODE) is not None
    assert all(reference == COMPATIBILITY_REF for reference, _ in secrets.reads)


@pytest.mark.asyncio
async def test_authentication_rechecks_record_under_shared_gate_after_a_race() -> None:
    gates = PrinterAdmissionGates()
    auth, store, secrets = authenticator(gates=gates)

    async with gates.hold(PRINTER_UUID):
        pending = asyncio.create_task(auth.authenticate_ftps(ACCESS_CODE))
        await secrets.first_read.wait()
        store.records = (printer(revision=2, updated_at_unix_ms=2_000),)
        await asyncio.sleep(0)
        assert not pending.done()

    assert await pending is None


@pytest.mark.asyncio
async def test_locked_authentication_denies_get_and_secret_changes() -> None:
    auth, store, secrets = authenticator()
    store.get_failure = RuntimeError("private registry detail")
    assert await auth.authenticate_ftps(ACCESS_CODE) is None

    store.get_failure = None
    original_get = store.get

    def rotate_after_get(printer_uuid: str) -> RegisteredPrinter | None:
        result = original_get(printer_uuid)
        secrets.values[COMPATIBILITY_REF] = OTHER_ACCESS_CODE
        return result

    store.get = rotate_after_get  # type: ignore[method-assign]
    assert await auth.authenticate_ftps(ACCESS_CODE) is None


def test_locked_recheck_defensively_denies_record_and_eligibility_changes() -> None:
    original = printer()
    auth, store, _ = authenticator()
    auth._unique_match = lambda *_args, **_kwargs: original  # type: ignore[method-assign]
    store.records = (printer(revision=2, updated_at_unix_ms=2_000),)
    assert auth._current_principal(original, ACCESS_CODE, proxy_serial=None) is None

    store.records = (original,)
    auth._eligible = lambda *_args, **_kwargs: False  # type: ignore[method-assign]
    assert auth._current_principal(original, ACCESS_CODE, proxy_serial=None) is None


@pytest.mark.asyncio
async def test_locked_authentication_defensively_denies_a_missing_reference() -> None:
    missing_reference = printer().model_copy(update={"compatibility_credential_ref": None})
    auth, store, _ = authenticator((missing_reference,))
    auth._unique_match = lambda *_args, **_kwargs: missing_reference  # type: ignore[method-assign]

    assert await auth.authenticate_ftps(ACCESS_CODE) is None
    assert store.get(PRINTER_UUID) == missing_reference


@pytest.mark.asyncio
async def test_revalidate_accepts_only_the_same_live_revision_and_readable_secret() -> None:
    auth, store, secrets = authenticator()
    principal = expected_principal()

    assert await auth.revalidate(principal)
    secrets.values[COMPATIBILITY_REF] = RuntimeError("private secret detail")
    assert not await auth.revalidate(principal)
    store.get_failure = RuntimeError("private registry detail")
    assert not await auth.revalidate(principal)
    store.get_failure = None
    store.records = ()
    assert not await auth.revalidate(principal)

    store.records = (printer().model_copy(update={"compatibility_credential_ref": None}),)
    assert not await auth.revalidate(principal)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement",
    [
        printer(revision=2, updated_at_unix_ms=2_000),
        printer(
            lifecycle=PrinterLifecycle.DISABLED,
            control_enabled=False,
            dispatch_enabled=False,
            revision=2,
            updated_at_unix_ms=2_000,
        ),
        printer(
            lifecycle=PrinterLifecycle.REMOVED,
            moonraker_credential_ref=None,
            compatibility_credential_ref=None,
            control_enabled=False,
            dispatch_enabled=False,
            revision=2,
            updated_at_unix_ms=2_000,
        ),
        printer(
            compatibility_credential_ref=OTHER_COMPATIBILITY_REF,
            revision=2,
            updated_at_unix_ms=2_000,
        ),
    ],
)
async def test_revalidate_invalidates_revision_disable_remove_and_rotation(
    replacement: RegisteredPrinter,
) -> None:
    auth, store, _ = authenticator()
    store.records = (replacement,)

    assert not await auth.revalidate(expected_principal())


@pytest.mark.asyncio
async def test_revalidate_checks_every_principal_field_and_shape() -> None:
    auth, _, _ = authenticator()
    principal = expected_principal()
    variants = (
        replace(principal, printer_uuid=OTHER_PRINTER_UUID),
        replace(principal, proxy_serial="KLOVE-OTHER"),
        replace(principal, record_revision=2),
        replace(principal, control_enabled=False),
        replace(principal, dispatch_enabled=False),
    )
    malformed = CompatibilityPrincipal(
        printer_uuid=cast(str, 1),
        proxy_serial=principal.proxy_serial,
        record_revision=principal.record_revision,
        control_enabled=principal.control_enabled,
        dispatch_enabled=principal.dispatch_enabled,
    )

    assert not await auth.revalidate(cast(CompatibilityPrincipal, object()))
    assert not await auth.revalidate(malformed)
    for variant in variants:
        assert not await auth.revalidate(variant)


@pytest.mark.asyncio
async def test_revalidate_is_serialized_with_lifecycle_gate() -> None:
    gates = PrinterAdmissionGates()
    auth, store, _ = authenticator(gates=gates)
    principal = expected_principal()

    async with gates.hold(PRINTER_UUID):
        pending = asyncio.create_task(auth.revalidate(principal))
        await asyncio.sleep(0)
        assert not pending.done()
        store.records = (printer(revision=2, updated_at_unix_ms=2_000),)

    assert not await pending


def test_principal_repr_and_shape_contain_no_secret_material() -> None:
    principal = expected_principal()
    rendered = repr(principal)

    assert ACCESS_CODE not in rendered
    assert COMPATIBILITY_REF not in rendered
    assert "credential" not in principal.__dataclass_fields__
    assert "endpoint" not in principal.__dataclass_fields__
    assert hash(principal)
