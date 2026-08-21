from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from klove.domain.onboarding import MoonrakerEndpoint, RegistryOperationKind
from klove.domain.onboarding_requests import (
    CreatePrinterRequest,
    DisablePrinterRequest,
    InspectPrinterRequest,
    LifecycleResultRequest,
    RemovePrinterRequest,
    RotateCompatibilityCredentialRequest,
    RotateMoonrakerCredentialRequest,
    UpdatePrinterRequest,
)

from ..onboarding_helpers import (
    IDEMPOTENCY_KEY,
    OTHER_PRINTER_UUID,
    PRINTER_UUID,
    safety_profile,
)

ENDPOINT = MoonrakerEndpoint(url="http://127.0.0.1:7125", verify_tls=False)
MOONRAKER_CREDENTIAL = "m" * 32


def common_request(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "idempotency_key": IDEMPOTENCY_KEY,
        "printer_uuid": PRINTER_UUID,
        "actor": "owner:local",
        "request_origin": "https://grove.example.test",
    }
    values.update(updates)
    return values


def test_every_lifecycle_request_is_strict_frozen_and_secret_safe() -> None:
    create = CreatePrinterRequest(
        **common_request(),  # type: ignore[arg-type]
        display_name="Workshop Voron",
        endpoint=ENDPOINT,
        moonraker_credential=SecretStr(MOONRAKER_CREDENTIAL),
        safety_profiles=(safety_profile(),),
        control_enabled=True,
        dispatch_enabled=True,
    )
    update = UpdatePrinterRequest(
        **common_request(),  # type: ignore[arg-type]
        expected_revision=1,
        display_name="Renamed Voron",
        endpoint=ENDPOINT,
        safety_profiles=(safety_profile(),),
        control_enabled=True,
        dispatch_enabled=True,
    )
    rotate_moonraker = RotateMoonrakerCredentialRequest(
        **common_request(),  # type: ignore[arg-type]
        expected_revision=1,
        moonraker_credential=SecretStr("n" * 32),
    )
    remaining = (
        RotateCompatibilityCredentialRequest,
        DisablePrinterRequest,
        RemovePrinterRequest,
    )

    assert create.moonraker_credential.get_secret_value() == MOONRAKER_CREDENTIAL
    assert create.model_dump(mode="json")["moonraker_credential"] != MOONRAKER_CREDENTIAL
    assert update.display_name == "Renamed Voron"
    assert rotate_moonraker.moonraker_credential.get_secret_value() == "n" * 32
    for model in remaining:
        request = model.model_validate(common_request(expected_revision=1))
        assert request.expected_revision == 1

    with pytest.raises(ValidationError):
        CreatePrinterRequest.model_validate(
            {
                **create.model_dump(mode="python"),
                "unexpected": True,
            }
        )
    with pytest.raises(ValidationError):
        create.display_name = "mutable"


def test_inspect_and_result_requests_are_exact_and_exclude_bootstrap() -> None:
    inspected = InspectPrinterRequest(
        printer_uuid=PRINTER_UUID,
        actor="owner",
        request_origin="https://grove.example.test",
        endpoint=ENDPOINT,
        moonraker_credential=SecretStr(MOONRAKER_CREDENTIAL),
    )
    result = LifecycleResultRequest(
        idempotency_key=IDEMPOTENCY_KEY,
        printer_uuid=PRINTER_UUID,
        operation="create",  # type: ignore[arg-type]
        actor="owner",
        request_origin="https://grove.example.test",
    )
    assert inspected.moonraker_credential.get_secret_value() == MOONRAKER_CREDENTIAL
    assert result.operation is RegistryOperationKind.CREATE

    for values in (
        {**result.model_dump(mode="python"), "operation": "bootstrap_import"},
        {**result.model_dump(mode="python"), "operation": object()},
        {**result.model_dump(mode="python"), "actor": " owner"},
        {**inspected.model_dump(mode="python"), "moonraker_credential": "short"},
    ):
        model = (
            InspectPrinterRequest if "moonraker_credential" in values else LifecycleResultRequest
        )
        with pytest.raises(ValidationError):
            model.model_validate(values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actor", " owner:local"),
        ("actor", "owner\x7flocal"),
        ("request_origin", "https://grove.example.test\n"),
    ],
)
def test_common_audit_text_must_be_exact(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        DisablePrinterRequest.model_validate(common_request(**{field: value}, expected_revision=1))


@pytest.mark.parametrize(
    "credential",
    [
        "x" * 31,
        "x" * 4_097,
        "has space" * 4,
        "x" * 31 + "\x7f",
        "é" * 32,
    ],
)
@pytest.mark.parametrize("model", [CreatePrinterRequest, RotateMoonrakerCredentialRequest])
def test_moonraker_credentials_are_bounded_visible_ascii(
    credential: str,
    model: type[CreatePrinterRequest] | type[RotateMoonrakerCredentialRequest],
) -> None:
    values = common_request(
        moonraker_credential=credential,
        expected_revision=1,
        display_name="Workshop Voron",
        endpoint=ENDPOINT,
    )
    if model is CreatePrinterRequest:
        values.pop("expected_revision")
    else:
        values.pop("display_name")
        values.pop("endpoint")
    with pytest.raises(ValidationError):
        model.model_validate(values)


@pytest.mark.parametrize(
    ("profiles", "dispatch_enabled"),
    [
        ((safety_profile(printer_uuid=OTHER_PRINTER_UUID),), False),
        ((safety_profile(), safety_profile()), False),
        (
            (
                safety_profile(slicer_profile_id="z-last"),
                safety_profile(slicer_profile_id="a-first"),
            ),
            False,
        ),
        ((), True),
    ],
)
@pytest.mark.parametrize("model", [CreatePrinterRequest, UpdatePrinterRequest])
def test_profile_bindings_are_exact_unique_ordered_and_required_for_dispatch(
    profiles: tuple[object, ...],
    dispatch_enabled: bool,
    model: type[CreatePrinterRequest] | type[UpdatePrinterRequest],
) -> None:
    values = common_request(
        display_name="Workshop Voron",
        endpoint=ENDPOINT,
        safety_profiles=profiles,
        dispatch_enabled=dispatch_enabled,
        moonraker_credential=MOONRAKER_CREDENTIAL,
        expected_revision=1,
    )
    if model is CreatePrinterRequest:
        values.pop("expected_revision")
    else:
        values.pop("moonraker_credential")
    with pytest.raises(ValidationError):
        model.model_validate(values)


@pytest.mark.parametrize("display_name", [" Workshop Voron", "Workshop\x7fVoron"])
@pytest.mark.parametrize("model", [CreatePrinterRequest, UpdatePrinterRequest])
def test_display_names_are_exact(
    display_name: str,
    model: type[CreatePrinterRequest] | type[UpdatePrinterRequest],
) -> None:
    values = common_request(
        display_name=display_name,
        endpoint=ENDPOINT,
        moonraker_credential=MOONRAKER_CREDENTIAL,
        expected_revision=1,
    )
    if model is CreatePrinterRequest:
        values.pop("expected_revision")
    else:
        values.pop("moonraker_credential")
    with pytest.raises(ValidationError):
        model.model_validate(values)
