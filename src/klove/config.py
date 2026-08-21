"""Strict application configuration and secret-file loading."""

from __future__ import annotations

import ipaddress
import tomllib
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from klove.domain.artifacts import ArtifactLimits, CanonicalUuid4, SafetyProfile
from klove.errors import ConfigurationError

Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$"),
]


class PrinterConfig(BaseModel):
    """One explicitly configured Moonraker endpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Identifier
    uuid: CanonicalUuid4
    endpoint: str
    api_key_file: Path
    allow_insecure_http: bool = False
    verify_tls: bool = True
    control_enabled: bool = False
    dispatch_enabled: bool = False
    safety_profiles: tuple[SafetyProfile, ...] = ()

    @model_validator(mode="after")
    def validate_endpoint(self) -> PrinterConfig:
        """Require an absolute HTTP(S) URL and explicit insecure-network consent."""
        parsed = urlsplit(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("endpoint must be an absolute HTTP or HTTPS URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("endpoint must not contain credentials, a query, or a fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("endpoint must not contain a path")
        if (
            parsed.scheme == "http"
            and not self.allow_insecure_http
            and not _is_loopback(parsed.hostname)
        ):
            raise ValueError("non-loopback HTTP requires allow_insecure_http=true")
        if any(profile.printer_uuid != self.uuid for profile in self.safety_profiles):
            raise ValueError("safety profile printer_uuid must match its printer")
        profile_ids = [profile.slicer_profile_id for profile in self.safety_profiles]
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("slicer profile ids must be unique per printer")
        return self


def _is_loopback(hostname: str) -> bool:
    """Return whether a hostname is an unambiguous loopback address."""
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


class ApiConfig(BaseModel):
    """Native API configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    listen_host: str = "127.0.0.1"
    listen_port: int = Field(default=8080, ge=1, le=65535)
    token_file: Path


class RegistryConfig(BaseModel):
    """Private canonical registry storage and direct-probe policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    database_file: Path = Path("/var/lib/klove/printer-registry.sqlite3")
    secret_directory: Path = Path("/var/lib/klove/registry-secrets")
    allowed_probe_cidrs: tuple[str, ...] = ("127.0.0.0/8",)

    @field_validator("database_file", "secret_directory")
    @classmethod
    def durable_paths_are_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("registry paths must be absolute")
        return value

    @field_validator("allowed_probe_cidrs")
    @classmethod
    def probe_cidrs_are_exact(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > 64 or len(values) != len(set(values)):
            raise ValueError("probe CIDRs must be unique and bounded")
        for value in values:
            if value != value.strip() or not value.isascii():
                raise ValueError("probe CIDRs must be exact ASCII strings")
            try:
                network = ipaddress.ip_network(value, strict=True)
            except ValueError as exc:
                raise ValueError("probe CIDRs must be canonical networks") from exc
            if str(network) != value or network.prefixlen == 0:
                raise ValueError("probe CIDRs must be canonical and narrower than all addresses")
        return values


class OnboardingConfig(BaseModel):
    """Independent owner authentication and bounded browser-session policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    owner_credential_file: Path | None = None
    allowed_grove_origins: tuple[str, ...] = ()
    frame_origin: str | None = None
    compatibility_host: str | None = None
    allow_loopback_http: bool = False
    session_capacity: int = Field(default=128, ge=1, le=10_000)
    session_inactivity_seconds: int = Field(default=900, ge=1, le=900)
    session_absolute_seconds: int = Field(default=1800, ge=1, le=1800)

    @property
    def cookie_secure(self) -> bool:
        """Use secure cookies except in the explicit loopback HTTP dev mode."""
        return not self.allow_loopback_http

    @property
    def frame_enabled(self) -> bool:
        """Return whether an exact Klove browser origin enables embedded frame routes."""
        return self.frame_origin is not None

    @field_validator("owner_credential_file")
    @classmethod
    def credential_path_is_absolute(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("owner credential file must be absolute")
        return value

    @field_validator("allowed_grove_origins")
    @classmethod
    def grove_origins_are_exact(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or len(values) > 32 or len(values) != len(set(values)):
            if values:
                raise ValueError("Grove origins must be unique and bounded")
            return values
        for value in values:
            _validate_exact_origin(value)
        return values

    @field_validator("frame_origin")
    @classmethod
    def frame_origin_is_exact(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_exact_origin(value)
        return value

    @field_validator("compatibility_host")
    @classmethod
    def compatibility_host_is_exact(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_compatibility_host(value)
        return value

    @model_validator(mode="after")
    def enabled_policy_is_complete(self) -> OnboardingConfig:
        if self.session_inactivity_seconds > self.session_absolute_seconds:
            raise ValueError("session inactivity limit must not exceed its absolute limit")
        if not self.enabled:
            if (
                self.owner_credential_file is not None
                or self.allowed_grove_origins
                or self.frame_origin is not None
                or self.compatibility_host is not None
                or self.allow_loopback_http
            ):
                raise ValueError("disabled onboarding cannot contain active configuration")
            return self
        if self.owner_credential_file is None:
            raise ValueError("enabled onboarding requires an owner credential file")
        if not self.allowed_grove_origins:
            raise ValueError("enabled onboarding requires at least one Grove origin")
        frame_origins = self.allowed_grove_origins
        if self.frame_origin is not None:
            if self.compatibility_host is None:
                raise ValueError("embedded onboarding requires one compatibility host")
            if self.frame_origin in self.allowed_grove_origins:
                raise ValueError("frame origin must remain distinct from Grove parent origins")
            frame = urlsplit(self.frame_origin)
            if any(
                (parent := urlsplit(origin)).scheme != frame.scheme
                or parent.hostname != frame.hostname
                for origin in self.allowed_grove_origins
            ):
                raise ValueError(
                    "frame and Grove parent origins must share one exact trusted-site "
                    "host and scheme"
                )
            frame_origins += (self.frame_origin,)
        elif self.compatibility_host is not None:
            raise ValueError("compatibility host requires an embedded frame origin")
        for origin in frame_origins:
            parsed = urlsplit(origin)
            if parsed.scheme == "http" and (
                not self.allow_loopback_http or not _is_loopback(parsed.hostname or "")
            ):
                raise ValueError(
                    "HTTP Grove origins require the explicit loopback development exception"
                )
        return self


def _validate_exact_origin(value: str) -> None:
    """Require one canonical, uncredentialed HTTP(S) origin serialization."""
    if (
        not value
        or len(value) > 2048
        or value != value.strip()
        or not value.isascii()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("Grove origin must be bounded exact ASCII text")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Grove origin is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ValueError("Grove origin must use HTTP or HTTPS")
    if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("Grove origin must not contain credentials, a path, query, or fragment")
    canonical_host = _canonical_origin_hostname(hostname)
    if port == 0:
        raise ValueError("Grove origin port must be positive")
    if port == (80 if parsed.scheme == "http" else 443):
        raise ValueError("Grove origin must omit its default port")
    rendered_host = f"[{canonical_host}]" if ":" in canonical_host else canonical_host
    rendered_port = "" if port is None else f":{port}"
    if value != f"{parsed.scheme}://{rendered_host}{rendered_port}":
        raise ValueError("Grove origin must use one canonical spelling")


def _canonical_origin_hostname(hostname: str) -> str:
    if "%" in hostname:
        raise ValueError("Grove origin must not contain an IPv6 scope")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if hostname != hostname.casefold() or hostname.endswith(".") or len(hostname) > 253:
            raise ValueError("Grove DNS name must be canonical") from None
        labels = hostname.split(".")
        if any(
            not label
            or len(label) > 63
            or label[0] == "-"
            or label[-1] == "-"
            or any(
                not (character.isascii() and (character.isalnum() or character == "-"))
                for character in label
            )
            for label in labels
        ):
            raise ValueError("Grove DNS name is invalid") from None
        return hostname
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        raise ValueError("Grove IPv4-mapped IPv6 aliases are prohibited")
    return str(address)


def _validate_compatibility_host(value: str) -> None:
    """Require the host-only Grove compatibility target defined by ADR 0006."""
    if (
        not value
        or len(value) > 253
        or value != value.strip()
        or not value.isascii()
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise ValueError("compatibility host must be bounded exact ASCII text")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if all(label.isdecimal() for label in value.split(".")):
            raise ValueError("compatibility host must be canonical") from None
        _canonical_origin_hostname(value)
        return
    if not isinstance(address, ipaddress.IPv4Address) or str(address) != value:
        raise ValueError("compatibility host must be a canonical DNS host or IPv4 address")


class ControlConfig(BaseModel):
    """Explicit, bounded job-control settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    request_timeout_seconds: float = Field(default=10.0, gt=0, le=30, allow_inf_nan=False)
    confirmation_timeout_seconds: float = Field(default=5.0, gt=0, le=30, allow_inf_nan=False)
    poll_interval_seconds: float = Field(default=0.1, gt=0, le=5, allow_inf_nan=False)
    idempotency_capacity: int = Field(default=1024, ge=1, le=100_000)

    @model_validator(mode="after")
    def finite_consistent_timing(self) -> ControlConfig:
        """Reject non-finite limits and polling slower than confirmation."""
        if self.poll_interval_seconds > self.confirmation_timeout_seconds:
            raise ValueError("control poll interval must not exceed confirmation timeout")
        return self


class DispatchConfig(BaseModel):
    """Explicit durable print-start settings; disabled until both gates opt in."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    journal_file: Path = Path("/var/lib/klove/start-journal.sqlite3")
    request_timeout_seconds: float = Field(default=10.0, gt=0, le=30, allow_inf_nan=False)
    confirmation_timeout_seconds: float = Field(default=10.0, gt=0, le=60, allow_inf_nan=False)
    poll_interval_seconds: float = Field(default=0.1, gt=0, le=5, allow_inf_nan=False)

    @field_validator("journal_file")
    @classmethod
    def journal_path_is_absolute(cls, value: Path) -> Path:
        """Keep durable state on one explicit non-relative operator path."""
        if not value.is_absolute():
            raise ValueError("dispatch journal_file must be absolute")
        return value

    @model_validator(mode="after")
    def finite_consistent_timing(self) -> DispatchConfig:
        """Reject polling slower than the complete confirmation window."""
        if self.poll_interval_seconds > self.confirmation_timeout_seconds:
            raise ValueError("dispatch poll interval must not exceed confirmation timeout")
        return self


class AppConfig(BaseModel):
    """Complete Klove configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api: ApiConfig
    registry: RegistryConfig = RegistryConfig()
    onboarding: OnboardingConfig = OnboardingConfig()
    control: ControlConfig = ControlConfig()
    dispatch: DispatchConfig = DispatchConfig()
    artifacts: ArtifactLimits = ArtifactLimits()
    printers: tuple[PrinterConfig, ...] = ()

    @model_validator(mode="after")
    def unique_printers(self) -> AppConfig:
        """Reject ambiguous printer routing."""
        ids = [printer.id for printer in self.printers]
        if len(ids) != len(set(ids)):
            raise ValueError("printer ids must be unique")
        uuids = [printer.uuid for printer in self.printers]
        if len(uuids) != len(set(uuids)):
            raise ValueError("printer UUIDs must be unique")
        if not self.control.enabled and any(printer.control_enabled for printer in self.printers):
            raise ValueError("per-printer control requires control.enabled=true")
        if not self.dispatch.enabled and any(printer.dispatch_enabled for printer in self.printers):
            raise ValueError("per-printer dispatch requires dispatch.enabled=true")
        if self.printers and not self.registry.allowed_probe_cidrs:
            raise ValueError("file bootstrap printers require an exact probe CIDR allowlist")
        if self.registry.database_file.is_relative_to(self.registry.secret_directory):
            raise ValueError("registry database must remain outside the secret directory")
        if self.dispatch.journal_file == self.registry.database_file:
            raise ValueError("registry and print-start journals must be separate files")
        if self.dispatch.journal_file.is_relative_to(self.registry.secret_directory):
            raise ValueError("print-start journal must remain outside the secret directory")
        return self


def load_config(path: Path) -> AppConfig:
    """Load a TOML file with strict schema validation."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigurationError("configuration file is unreadable") from exc
    try:
        document = tomllib.loads(raw.decode("utf-8", errors="strict"))
        return AppConfig.model_validate(document)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise ConfigurationError("configuration file is invalid") from exc


def read_secret(path: Path, *, minimum_length: int = 32, maximum_bytes: int = 4096) -> str:
    """Read one bounded, single-line secret without ever returning partial data."""
    try:
        if path.stat().st_size > maximum_bytes:
            raise ConfigurationError("secret file exceeds the size limit")
        value = path.read_text(encoding="utf-8", errors="strict").rstrip("\r\n")
    except ConfigurationError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError("secret file is unreadable") from exc
    if len(value) < minimum_length:
        raise ConfigurationError("secret is shorter than the minimum length")
    if any(character.isspace() for character in value):
        raise ConfigurationError("secret must be a single token without whitespace")
    return value
