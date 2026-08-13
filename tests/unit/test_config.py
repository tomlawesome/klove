from pathlib import Path

import pytest
from pydantic import ValidationError

from klove.config import (
    ApiConfig,
    AppConfig,
    ControlConfig,
    PrinterConfig,
    load_config,
    read_secret,
)
from klove.domain.artifacts import ArtifactLimits
from klove.errors import ConfigurationError


def printer(**overrides: object) -> PrinterConfig:
    values: dict[str, object] = {
        "id": "voron-24",
        "endpoint": "https://printer.example.invalid:7125",
        "api_key_file": Path("moonraker.key"),
    }
    values.update(overrides)
    return PrinterConfig.model_validate(values)


def test_valid_configuration_loads_and_forbids_unknown_fields(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[api]
token_file = "api.token"

[artifacts]
max_zip_entries = 64
max_archive_compressed_bytes = 1048576
max_archive_expanded_bytes = 4194304
max_compression_ratio = 10
max_gcode_bytes = 2097152
metadata_wait_seconds = 20.0

[[printers]]
id = "voron-24"
endpoint = "http://127.0.0.1:7125"
api_key_file = "moonraker.key"
""".strip(),
        encoding="utf-8",
    )
    result = load_config(config_path)

    assert result.api.listen_host == "127.0.0.1"
    assert result.api.listen_port == 8080
    assert result.artifacts.max_zip_entries == 64
    assert result.artifacts.max_compression_ratio == 10
    assert result.artifacts.metadata_wait_seconds == 20.0
    assert result.printers[0].id == "voron-24"

    with pytest.raises(ValidationError):
        ApiConfig(token_file=Path("token"), mystery=True)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "endpoint",
    [
        "mqtt://printer.example.invalid",
        "https:///missing-host",
        "https://user:pass@printer.example.invalid",
        "https://printer.example.invalid/?query=true",
        "https://printer.example.invalid/#fragment",
        "https://printer.example.invalid/moonraker",
        "http://printer.example.invalid:7125",
    ],
)
def test_ambiguous_or_insecure_endpoints_are_rejected(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        printer(endpoint=endpoint)


def test_insecure_remote_http_requires_explicit_consent() -> None:
    assert printer(endpoint="http://printer.example.invalid:7125", allow_insecure_http=True)
    assert printer(endpoint="http://localhost:7125")
    assert printer(endpoint="http://[::1]:7125")


def test_duplicate_printer_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="must be unique"):
        AppConfig(api=ApiConfig(token_file=Path("token")), printers=(printer(), printer()))


def test_control_is_explicit_per_installation_and_printer() -> None:
    with pytest.raises(ValidationError, match=r"requires control\.enabled"):
        AppConfig(
            api=ApiConfig(token_file=Path("token")),
            printers=(printer(control_enabled=True),),
        )
    assert AppConfig(
        api=ApiConfig(token_file=Path("token")),
        control=ControlConfig(enabled=True),
        printers=(printer(control_enabled=True),),
    )


@pytest.mark.parametrize(
    "values",
    [
        {"request_timeout_seconds": float("nan")},
        {"confirmation_timeout_seconds": float("nan")},
        {"poll_interval_seconds": float("nan")},
        {"confirmation_timeout_seconds": 1, "poll_interval_seconds": 2},
    ],
)
def test_control_timing_is_finite_positive_and_consistent(values: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        ControlConfig(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "values",
    [
        {"max_zip_entries": 0},
        {"max_archive_compressed_bytes": 0},
        {"max_archive_expanded_bytes": 0},
        {"max_compression_ratio": 0},
        {"max_compression_ratio": 10_001},
        {"max_compression_ratio": 1.0},
        {"max_compression_ratio": float("nan")},
        {"max_gcode_bytes": 0},
        {"metadata_wait_seconds": float("nan")},
        {"metadata_wait_seconds": 301.0},
        {"max_archive_expanded_bytes": 1024, "max_gcode_bytes": 1025},
    ],
)
def test_artifact_limits_are_finite_positive_and_consistent(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ArtifactLimits(**values)  # type: ignore[arg-type]


def test_bad_configuration_files_have_one_bounded_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="unreadable"):
        load_config(tmp_path / "missing.toml")

    for index, content in enumerate(
        (b"\xff", b"not = [valid", b"[api]\ntoken_file='x'\nextra=true")
    ):
        path = tmp_path / f"bad-{index}.toml"
        path.write_bytes(content)
        with pytest.raises(ConfigurationError, match="configuration file is invalid"):
            load_config(path)


def test_secret_loading_is_bounded_and_single_token(tmp_path: Path) -> None:
    valid = tmp_path / "valid"
    valid.write_text("a" * 32 + "\n", encoding="utf-8")
    assert read_secret(valid) == "a" * 32

    missing = tmp_path / "missing"
    with pytest.raises(ConfigurationError, match="unreadable"):
        read_secret(missing)

    oversized = tmp_path / "oversized"
    oversized.write_text("x" * 65, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="size limit"):
        read_secret(oversized, maximum_bytes=64)

    bad_encoding = tmp_path / "encoding"
    bad_encoding.write_bytes(b"\xff" * 32)
    with pytest.raises(ConfigurationError, match="unreadable"):
        read_secret(bad_encoding)

    short = tmp_path / "short"
    short.write_text("short", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="shorter"):
        read_secret(short)

    whitespace = tmp_path / "whitespace"
    whitespace.write_text("a" * 31 + " b", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="without whitespace"):
        read_secret(whitespace)
