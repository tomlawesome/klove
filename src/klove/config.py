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
    model_validator,
)

from klove.errors import ConfigurationError

Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$"),
]


class PrinterConfig(BaseModel):
    """One explicitly configured Moonraker endpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Identifier
    endpoint: str
    api_key_file: Path
    allow_insecure_http: bool = False
    verify_tls: bool = True
    control_enabled: bool = False

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
        return self


class ApiConfig(BaseModel):
    """Native API configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    listen_host: str = "127.0.0.1"
    listen_port: int = Field(default=8080, ge=1, le=65535)
    token_file: Path


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


class AppConfig(BaseModel):
    """Complete Klove configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api: ApiConfig
    control: ControlConfig = ControlConfig()
    printers: tuple[PrinterConfig, ...] = ()

    @model_validator(mode="after")
    def unique_printers(self) -> AppConfig:
        """Reject ambiguous printer routing."""
        ids = [printer.id for printer in self.printers]
        if len(ids) != len(set(ids)):
            raise ValueError("printer ids must be unique")
        if not self.control.enabled and any(printer.control_enabled for printer in self.printers):
            raise ValueError("per-printer control requires control.enabled=true")
        return self


def _is_loopback(hostname: str) -> bool:
    """Return whether a hostname is an unambiguous loopback address."""
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


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
