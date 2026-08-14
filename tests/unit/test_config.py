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
from klove.domain.artifacts import (
    ArtifactCompatibility,
    ArtifactLimits,
    BuildVolume,
    GcodeFlavor,
    SafetyProfile,
)
from klove.errors import ConfigurationError


def printer(**overrides: object) -> PrinterConfig:
    values: dict[str, object] = {
        "id": "voron-24",
        "uuid": "11111111-1111-4111-8111-111111111111",
        "endpoint": "https://printer.example.invalid:7125",
        "api_key_file": Path("moonraker.key"),
    }
    values.update(overrides)
    return PrinterConfig.model_validate(values)


def safety_profile(**overrides: object) -> SafetyProfile:
    values: dict[str, object] = {
        "printer_uuid": "11111111-1111-4111-8111-111111111111",
        "generation": 1,
        "slicer_profile_id": "klipper-voron-24-0.4",
        "compatibility": ArtifactCompatibility(
            gcode_flavor=GcodeFlavor.KLIPPER,
            nozzle_diameter_micrometres=400,
            build_volume=BuildVolume(
                x_micrometres=350_000,
                y_micrometres=350_000,
                z_micrometres=350_000,
            ),
            build_plate_id="textured-pei",
        ),
    }
    values.update(overrides)
    return SafetyProfile.model_validate(values)


def test_valid_configuration_loads_and_forbids_unknown_fields(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[api]
token_file = "api.token"

[artifacts]
max_zip_entries = 64
max_zip_metadata_bytes = 131072
max_archive_compressed_bytes = 1048576
max_archive_expanded_bytes = 4194304
max_compression_ratio = 10
max_gcode_bytes = 2097152
max_gcode_header_bytes = 32768
max_gcode_line_bytes = 4096
upload_timeout_seconds = 120.0
metadata_wait_seconds = 20.0
metadata_poll_interval_seconds = 0.25
upload_idempotency_capacity = 512

[[printers]]
id = "voron-24"
uuid = "11111111-1111-4111-8111-111111111111"
endpoint = "http://127.0.0.1:7125"
api_key_file = "moonraker.key"

[[printers.safety_profiles]]
profile_version = "1"
printer_uuid = "11111111-1111-4111-8111-111111111111"
generation = 1
slicer_profile_id = "klipper-voron-24-0.4"

[printers.safety_profiles.compatibility]
gcode_flavor = "klipper"
nozzle_diameter_micrometres = 400
build_plate_id = "textured-pei"

[printers.safety_profiles.compatibility.build_volume]
x_micrometres = 350000
y_micrometres = 350000
z_micrometres = 350000
""".strip(),
        encoding="utf-8",
    )
    result = load_config(config_path)

    assert result.api.listen_host == "127.0.0.1"
    assert result.api.listen_port == 8080
    assert result.artifacts.max_zip_entries == 64
    assert result.artifacts.max_zip_metadata_bytes == 131072
    assert result.artifacts.max_compression_ratio == 10
    assert result.artifacts.max_gcode_header_bytes == 32768
    assert result.artifacts.max_gcode_line_bytes == 4096
    assert result.artifacts.upload_timeout_seconds == 120.0
    assert result.artifacts.metadata_wait_seconds == 20.0
    assert result.artifacts.metadata_poll_interval_seconds == 0.25
    assert result.artifacts.upload_idempotency_capacity == 512
    assert result.printers[0].id == "voron-24"
    assert result.printers[0].safety_profiles == (safety_profile(),)

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


def test_printer_uuid_is_required_and_canonical() -> None:
    with pytest.raises(ValidationError):
        PrinterConfig(
            id="voron-24",
            endpoint="https://printer.example.invalid:7125",
            api_key_file=Path("moonraker.key"),
        )  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        printer(uuid="11111111-1111-4111-8111-11111111111A")


def test_duplicate_printer_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="must be unique"):
        AppConfig(api=ApiConfig(token_file=Path("token")), printers=(printer(), printer()))


def test_duplicate_printer_uuids_are_rejected_even_when_routes_differ() -> None:
    with pytest.raises(ValidationError, match="UUIDs must be unique"):
        AppConfig(
            api=ApiConfig(token_file=Path("token")),
            printers=(printer(), printer(id="other-route")),
        )


def test_safety_profiles_must_match_the_printer_and_be_unique() -> None:
    other = safety_profile(printer_uuid="66666666-6666-4666-8666-666666666666")
    with pytest.raises(ValidationError, match="must match its printer"):
        printer(safety_profiles=(other,))

    current = safety_profile()
    duplicate = current.model_copy(update={"generation": 2})
    with pytest.raises(ValidationError, match="profile ids must be unique"):
        printer(safety_profiles=(current, duplicate))


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
        {"max_zip_metadata_bytes": 21},
        {"max_archive_compressed_bytes": 0},
        {"max_archive_expanded_bytes": 0},
        {"max_compression_ratio": 0},
        {"max_compression_ratio": 10_001},
        {"max_compression_ratio": 1.0},
        {"max_compression_ratio": float("nan")},
        {"max_gcode_bytes": 0},
        {"max_gcode_header_bytes": 63},
        {"max_gcode_line_bytes": 0},
        {"upload_timeout_seconds": float("nan")},
        {"upload_timeout_seconds": 3601.0},
        {"metadata_wait_seconds": float("nan")},
        {"metadata_wait_seconds": 301.0},
        {"metadata_poll_interval_seconds": 0.0},
        {"metadata_wait_seconds": 1.0, "metadata_poll_interval_seconds": 2.0},
        {"upload_idempotency_capacity": 0},
        {"max_archive_expanded_bytes": 1024, "max_gcode_bytes": 1025},
        {"max_gcode_bytes": 64, "max_gcode_header_bytes": 65},
        {
            "max_gcode_bytes": 64,
            "max_gcode_header_bytes": 64,
            "max_gcode_line_bytes": 65,
        },
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
