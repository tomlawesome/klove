"""Strict internal request models for typed registry lifecycle orchestration."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from klove.domain.artifacts import CanonicalUuid4, SafetyProfile
from klove.domain.onboarding import AuditText, DisplayName, MoonrakerEndpoint


class LifecycleRequest(BaseModel):
    """Authenticated audit and idempotency evidence shared by every mutation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    idempotency_key: CanonicalUuid4
    printer_uuid: CanonicalUuid4
    actor: AuditText
    request_origin: AuditText

    @field_validator("actor", "request_origin")
    @classmethod
    def audit_text_is_exact(cls, value: str) -> str:
        return _exact_text(value)


class CreatePrinterRequest(LifecycleRequest):
    """Create one exact canonical printer from direct current evidence."""

    display_name: DisplayName
    endpoint: MoonrakerEndpoint
    moonraker_credential: SecretStr
    safety_profiles: tuple[SafetyProfile, ...] = Field(default=(), max_length=256)
    control_enabled: bool = False
    dispatch_enabled: bool = False

    @field_validator("display_name")
    @classmethod
    def display_name_is_exact(cls, value: str) -> str:
        return _exact_text(value)

    @field_validator("moonraker_credential")
    @classmethod
    def moonraker_credential_is_bounded(cls, value: SecretStr) -> SecretStr:
        _visible_secret(value.get_secret_value())
        return value

    @model_validator(mode="after")
    def profiles_are_exact(self) -> CreatePrinterRequest:
        _validate_profiles(
            self.printer_uuid,
            self.safety_profiles,
            dispatch_enabled=self.dispatch_enabled,
        )
        return self


class UpdatePrinterRequest(LifecycleRequest):
    """Replace mutable registration evidence at one exact current revision."""

    expected_revision: int = Field(ge=1, le=9_223_372_036_854_775_807)
    display_name: DisplayName
    endpoint: MoonrakerEndpoint
    safety_profiles: tuple[SafetyProfile, ...] = Field(default=(), max_length=256)
    control_enabled: bool = False
    dispatch_enabled: bool = False
    reactivate: bool = False

    @field_validator("display_name")
    @classmethod
    def display_name_is_exact(cls, value: str) -> str:
        return _exact_text(value)

    @model_validator(mode="after")
    def profiles_are_exact(self) -> UpdatePrinterRequest:
        _validate_profiles(
            self.printer_uuid,
            self.safety_profiles,
            dispatch_enabled=self.dispatch_enabled,
        )
        return self


class RotateMoonrakerCredentialRequest(LifecycleRequest):
    """Replace one Moonraker credential only after it passes a direct probe."""

    expected_revision: int = Field(ge=1, le=9_223_372_036_854_775_807)
    moonraker_credential: SecretStr

    @field_validator("moonraker_credential")
    @classmethod
    def moonraker_credential_is_bounded(cls, value: SecretStr) -> SecretStr:
        _visible_secret(value.get_secret_value())
        return value


class RotateCompatibilityCredentialRequest(LifecycleRequest):
    """Generate and replace one private Grove-compatibility credential copy."""

    expected_revision: int = Field(ge=1, le=9_223_372_036_854_775_807)


class DisablePrinterRequest(LifecycleRequest):
    """Disable one exact active revision without discarding its identity."""

    expected_revision: int = Field(ge=1, le=9_223_372_036_854_775_807)


class RemovePrinterRequest(LifecycleRequest):
    """Tombstone one exact disabled revision and retire its credentials."""

    expected_revision: int = Field(ge=1, le=9_223_372_036_854_775_807)


def _validate_profiles(
    printer_uuid: str,
    profiles: tuple[SafetyProfile, ...],
    *,
    dispatch_enabled: bool,
) -> None:
    if any(profile.printer_uuid != printer_uuid for profile in profiles):
        raise ValueError("safety profile printer UUIDs must match")
    profile_ids = tuple(profile.slicer_profile_id for profile in profiles)
    if len(profile_ids) != len(set(profile_ids)) or profile_ids != tuple(sorted(profile_ids)):
        raise ValueError("safety profiles must be unique and canonically ordered")
    if dispatch_enabled and not profiles:
        raise ValueError("dispatch requires a safety profile")


def _visible_secret(value: str) -> None:
    try:
        encoded = value.encode("ascii", errors="strict")
    except UnicodeError as exc:
        raise ValueError("Moonraker credentials must be visible ASCII") from exc
    if not 32 <= len(encoded) <= 4_096 or any(
        ord(character) < 33 or ord(character) == 127 for character in value
    ):
        raise ValueError("Moonraker credentials must be bounded visible ASCII")


def _exact_text(value: str) -> str:
    if value != value.strip() or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError("text must not contain surrounding whitespace or control characters")
    return value
