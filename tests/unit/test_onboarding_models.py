from __future__ import annotations

import pytest
from pydantic import ValidationError

from klove.domain.discovery import discover_capabilities
from klove.domain.onboarding import (
    MoonrakerEndpoint,
    PrinterIdentityEvidence,
    PrinterLifecycle,
    RegisteredPrinter,
    RegistryOperationRecord,
    RegistryOperationState,
)

from ..onboarding_helpers import (
    COMPATIBILITY_REF,
    MOONRAKER_REF,
    NEW_MOONRAKER_REF,
    OTHER_PRINTER_UUID,
    identity,
    operation,
    printer,
    safety_profile,
)


def test_endpoint_accepts_explicit_secure_and_loopback_origins() -> None:
    secure = MoonrakerEndpoint(url="https://moonraker.example.test:7125")
    loopback = MoonrakerEndpoint(url="http://localhost:7125/", verify_tls=False)
    insecure = MoonrakerEndpoint(
        url="http://192.0.2.10:7125",
        allow_insecure_http=True,
        verify_tls=False,
    )

    assert secure.url == "https://moonraker.example.test:7125"
    assert loopback.url == "http://localhost:7125/"
    assert insecure.allow_insecure_http


@pytest.mark.parametrize(
    ("url", "updates"),
    [
        ("moonraker.example.test", {}),
        ("ftp://moonraker.example.test", {}),
        ("https://", {}),
        ("https://owner@moonraker.example.test", {}),
        ("https://moonraker.example.test/path", {}),
        ("https://moonraker.example.test?query=1", {}),
        ("https://moonraker.example.test#fragment", {}),
        ("https://moonraker.example.test:0", {}),
        ("https://moonraker.example.test:bad", {}),
        ("https://[invalid", {}),
        (" https://moonraker.example.test", {}),
        ("https://moonraker.example.test\n", {}),
        ("http://moonraker.example.test:7125", {"verify_tls": False}),
        ("http://127.0.0.1:7125", {}),
    ],
)
def test_endpoint_rejects_ambiguous_or_inconsistent_origins(
    url: str,
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        MoonrakerEndpoint(url=url, **updates)  # type: ignore[arg-type]


def test_identity_requires_exact_text_and_canonical_capability_evidence() -> None:
    valid = identity()
    assert valid.capabilities.dispatch_eligible

    for field, value in (
        ("server_hostname", " moonraker"),
        ("klipper_hostname", "klipper\nnode"),
    ):
        document = valid.model_dump(mode="python")
        document[field] = value
        with pytest.raises(ValidationError):
            PrinterIdentityEvidence.model_validate(document)

    inconsistent = valid.capabilities.model_copy(update={"dispatch_eligible": False})
    document = valid.model_dump(mode="python")
    document["capabilities"] = inconsistent
    with pytest.raises(ValidationError):
        PrinterIdentityEvidence.model_validate(document)


@pytest.mark.parametrize(
    "objects",
    [
        {f"object-{index}" for index in range(4_097)},
        {"x" * 256},
        {" object"},
    ],
)
def test_identity_bounds_and_sanitizes_capability_object_evidence(objects: set[str]) -> None:
    document = identity().model_dump(mode="python")
    document["capabilities"] = discover_capabilities(objects)

    with pytest.raises(ValidationError):
        PrinterIdentityEvidence.model_validate(document)


def test_registered_printer_is_complete_immutable_and_grove_compatible() -> None:
    registered = printer()

    assert registered.lifecycle is PrinterLifecycle.ACTIVE
    assert registered.proxy_serial == "KLOVE-11111111-1111-4111-8111-111111111111"
    assert len(registered.proxy_serial) == 42
    with pytest.raises(ValidationError):
        RegisteredPrinter.model_validate(
            {**registered.model_dump(mode="python"), "unknown": "forbidden"}
        )

    document = registered.model_dump(mode="python")
    document["lifecycle"] = "active"
    assert RegisteredPrinter.model_validate(document) == registered


@pytest.mark.parametrize(
    "updates",
    [
        {"moonraker_credential_ref": None},
        {
            "moonraker_credential_ref": MOONRAKER_REF,
            "compatibility_credential_ref": MOONRAKER_REF,
        },
        {"display_name": " Workshop Voron"},
        {"display_name": "Workshop\x7fVoron"},
        {"updated_at_unix_ms": 999},
        {"identity": identity().model_copy(update={"observed_at_unix_ms": 1_001})},
        {"safety_profiles": (safety_profile(printer_uuid=OTHER_PRINTER_UUID),)},
        {"safety_profiles": (safety_profile(), safety_profile())},
        {"safety_profiles": (safety_profile(),) * 257},
    ],
)
def test_registered_printer_rejects_incomplete_or_ambiguous_records(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        printer(**updates)


def test_registered_printer_lifecycle_and_capability_gates_fail_closed() -> None:
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
    assert disabled.lifecycle is PrinterLifecycle.DISABLED
    assert removed.lifecycle is PrinterLifecycle.REMOVED

    invalid_records = (
        {
            "lifecycle": PrinterLifecycle.REMOVED,
            "control_enabled": False,
            "dispatch_enabled": False,
        },
        {
            "lifecycle": PrinterLifecycle.DISABLED,
            "control_enabled": True,
            "dispatch_enabled": False,
        },
        {
            "lifecycle": PrinterLifecycle.DISABLED,
            "control_enabled": False,
            "dispatch_enabled": True,
        },
        {"dispatch_enabled": True, "safety_profiles": ()},
        {
            "identity": identity(eligible=False),
            "dispatch_enabled": True,
            "control_enabled": False,
        },
        {
            "identity": identity(eligible=False),
            "dispatch_enabled": False,
            "control_enabled": True,
        },
    )
    for updates in invalid_records:
        with pytest.raises(ValidationError):
            printer(**updates)

    with pytest.raises(ValidationError):
        printer(lifecycle="unknown")


def test_operation_models_cover_every_terminal_state_and_exact_enum_text() -> None:
    preparing = operation()
    committed = operation(
        state=RegistryOperationState.COMMITTED,
        result=printer(),
        committed_at_unix_ms=1_000,
    )
    aborted = operation(
        state=RegistryOperationState.ABORTED,
        error_code="interrupted",
    )

    assert preparing.state is RegistryOperationState.PREPARING
    assert committed.result == printer()
    assert aborted.error_code == "interrupted"

    document = preparing.model_dump(mode="python")
    document.update({"operation": "create", "state": "preparing"})
    assert RegistryOperationRecord.model_validate(document) == preparing
    with pytest.raises(ValidationError):
        RegistryOperationRecord.model_validate({**document, "operation": "unknown"})
    with pytest.raises(ValidationError):
        RegistryOperationRecord.model_validate({**document, "state": "unknown"})


@pytest.mark.parametrize(
    "updates",
    [
        {"new_credential_refs": (MOONRAKER_REF, MOONRAKER_REF)},
        {"retired_credential_refs": (COMPATIBILITY_REF, COMPATIBILITY_REF)},
        {
            "new_credential_refs": (MOONRAKER_REF,),
            "retired_credential_refs": (MOONRAKER_REF,),
        },
        {
            "new_credential_refs": (
                MOONRAKER_REF,
                COMPATIBILITY_REF,
                NEW_MOONRAKER_REF,
            )
        },
        {"actor": " owner"},
        {"request_origin": "https://grove.example.test\nframe"},
    ],
)
def test_operation_rejects_ambiguous_common_evidence(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        operation(**updates)


@pytest.mark.parametrize(
    "updates",
    [
        {"result": printer()},
        {"committed_at_unix_ms": 1_000},
        {"error_code": "interrupted"},
    ],
)
def test_preparing_operation_rejects_terminal_evidence(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        operation(**updates)


@pytest.mark.parametrize(
    "updates",
    [
        {"result": None, "committed_at_unix_ms": 1_000},
        {"result": printer(), "committed_at_unix_ms": None},
        {
            "result": printer(),
            "committed_at_unix_ms": 1_000,
            "error_code": "interrupted",
        },
    ],
)
def test_committed_operation_requires_only_complete_result(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        operation(state=RegistryOperationState.COMMITTED, **updates)


def test_committed_operation_binds_printer_and_monotonic_times() -> None:
    other = printer(
        printer_uuid=OTHER_PRINTER_UUID,
        safety_profiles=(safety_profile(printer_uuid=OTHER_PRINTER_UUID),),
    )
    for updates in (
        {"result": other, "committed_at_unix_ms": 1_000},
        {
            "result": printer(updated_at_unix_ms=1_001),
            "committed_at_unix_ms": 1_000,
        },
        {"result": printer(), "committed_at_unix_ms": 949},
        {
            "result": printer(),
            "started_at_unix_ms": 1_001,
            "committed_at_unix_ms": 1_001,
        },
    ):
        with pytest.raises(ValidationError):
            operation(state=RegistryOperationState.COMMITTED, **updates)


@pytest.mark.parametrize(
    "updates",
    [
        {"result": printer(), "error_code": "interrupted"},
        {"committed_at_unix_ms": 1_000, "error_code": "interrupted"},
        {"error_code": None},
    ],
)
def test_aborted_operation_has_only_interruption_evidence(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        operation(state=RegistryOperationState.ABORTED, **updates)


def test_capability_evidence_rejects_manufactured_object_claims() -> None:
    incomplete = discover_capabilities({"print_stats"})
    document = identity().model_dump(mode="python")
    document["capabilities"] = incomplete.model_copy(update={"dispatch_eligible": True})

    with pytest.raises(ValidationError):
        PrinterIdentityEvidence.model_validate(document)
