from pathlib import Path

import pytest
from pydantic import ValidationError

from klove.config import (
    ApiConfig,
    AppConfig,
    ControlConfig,
    DispatchConfig,
    GroveBridgeConfig,
    OnboardingConfig,
    PrinterConfig,
    RegistryConfig,
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

[dispatch]
enabled = true
journal_file = "/var/lib/klove/start-journal.sqlite3"
request_timeout_seconds = 9.0
confirmation_timeout_seconds = 8.0
poll_interval_seconds = 0.2

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
verify_tls = false
dispatch_enabled = true

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
    assert result.dispatch.enabled is True
    assert result.dispatch.request_timeout_seconds == 9.0
    assert result.dispatch.confirmation_timeout_seconds == 8.0
    assert result.dispatch.poll_interval_seconds == 0.2
    assert result.grove_bridge == GroveBridgeConfig()
    assert result.onboarding == OnboardingConfig()
    assert result.printers[0].id == "voron-24"
    assert result.printers[0].dispatch_enabled is True
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
    assert printer(
        endpoint="http://printer.example.invalid:7125",
        allow_insecure_http=True,
        verify_tls=False,
    )
    assert printer(endpoint="http://localhost:7125", verify_tls=False)
    assert printer(endpoint="http://[::1]:7125", verify_tls=False)


def test_registry_paths_and_probe_allowlist_are_exact() -> None:
    assert RegistryConfig().allowed_probe_cidrs == ("127.0.0.0/8",)
    assert RegistryConfig(allowed_probe_cidrs=("127.0.0.0/8", "::1/128")).allowed_probe_cidrs == (
        "127.0.0.0/8",
        "::1/128",
    )
    for values in (
        {"database_file": Path("relative.sqlite3")},
        {"secret_directory": Path("relative-secrets")},
        {"allowed_probe_cidrs": ("0.0.0.0/0",)},
        {"allowed_probe_cidrs": (" 127.0.0.0/8",)},
        {"allowed_probe_cidrs": ("tést",)},
        {"allowed_probe_cidrs": ("192.168.1.1/24",)},
        {"allowed_probe_cidrs": ("2001:DB8::/32",)},
        {"allowed_probe_cidrs": ("192.168.1.0/24", "192.168.1.0/24")},
        {"allowed_probe_cidrs": tuple(f"10.{index}.0.0/16" for index in range(65))},
    ):
        with pytest.raises(ValidationError):
            RegistryConfig.model_validate(values)

    with pytest.raises(ValidationError, match="probe CIDR allowlist"):
        AppConfig(
            api=ApiConfig(token_file=Path("token")),
            registry=RegistryConfig(allowed_probe_cidrs=()),
            printers=(printer(),),
        )


@pytest.mark.parametrize(
    ("registry", "journal"),
    [
        (
            RegistryConfig(
                database_file=Path("/state/secrets/registry.sqlite3"),
                secret_directory=Path("/state/secrets"),
            ),
            Path("/state/start.sqlite3"),
        ),
        (
            RegistryConfig(database_file=Path("/state/shared.sqlite3")),
            Path("/state/shared.sqlite3"),
        ),
        (
            RegistryConfig(secret_directory=Path("/state/secrets")),
            Path("/state/secrets/start.sqlite3"),
        ),
    ],
)
def test_registry_storage_boundaries_cannot_overlap(
    registry: RegistryConfig,
    journal: Path,
) -> None:
    with pytest.raises(ValidationError):
        AppConfig(
            api=ApiConfig(token_file=Path("token")),
            registry=registry,
            dispatch=DispatchConfig(journal_file=journal),
        )


def test_grove_bridge_is_disabled_by_default_and_enabled_policy_is_complete() -> None:
    assert GroveBridgeConfig().enabled is False
    assert GroveBridgeConfig(ftps_advertised_ipv4=None).ftps_advertised_ipv4 is None
    configured = GroveBridgeConfig(
        enabled=True,
        listen_host="0.0.0.0",  # noqa: S104 -- explicit container bridge bind under test.
        ftps_advertised_ipv4="192.168.1.20",
        tls_certificate_file=Path("/run/secrets/bridge.crt"),
        tls_private_key_file=Path("/run/secrets/bridge.key"),
    )

    assert configured.mqtt_port == 8883
    assert configured.ftps_control_port == 990
    assert tuple(
        range(configured.ftps_passive_port_min, configured.ftps_passive_port_max + 1)
    ) == tuple(range(50000, 50010))
    assert configured.max_sessions_per_printer == 2
    assert configured.staging_ttl_seconds == 3600

    for values in (
        {"enabled": True},
        {
            "enabled": True,
            "ftps_advertised_ipv4": "192.168.1.20",
            "tls_certificate_file": Path("/run/secrets/shared"),
            "tls_private_key_file": Path("/run/secrets/shared"),
        },
        {"tls_certificate_file": Path("/run/secrets/bridge.crt")},
    ):
        with pytest.raises(ValidationError):
            GroveBridgeConfig.model_validate(values)


def test_enabled_grove_bridge_reports_completeness_before_topology() -> None:
    with pytest.raises(ValidationError, match="distinct TLS files and FTPS address"):
        GroveBridgeConfig(enabled=True, ftps_advertised_ipv4="8.8.8.8")


@pytest.mark.parametrize(
    ("listen_host", "advertised_ipv4"),
    [
        ("127.0.0.1", "127.0.0.1"),
        ("10.20.30.40", "10.20.30.40"),
        ("172.16.30.40", "172.16.30.40"),
        ("192.168.1.20", "192.168.1.20"),
        ("0.0.0.0", "192.168.1.20"),  # noqa: S104 -- explicit container bridge bind.
    ],
)
def test_enabled_grove_bridge_accepts_only_direct_private_ftps_topology(
    listen_host: str, advertised_ipv4: str
) -> None:
    configured = GroveBridgeConfig(
        enabled=True,
        listen_host=listen_host,
        ftps_advertised_ipv4=advertised_ipv4,
        tls_certificate_file=Path("/run/secrets/bridge.crt"),
        tls_private_key_file=Path("/run/secrets/bridge.key"),
    )

    assert configured.listen_host == listen_host
    assert configured.ftps_advertised_ipv4 == advertised_ipv4


@pytest.mark.parametrize(
    ("listen_host", "advertised_ipv4"),
    [
        ("0.0.0.0", "8.8.8.8"),  # noqa: S104 -- rejected public topology input.
        ("0.0.0.0", "255.255.255.255"),  # noqa: S104 -- rejected broadcast input.
        ("0.0.0.0", "169.254.1.1"),  # noqa: S104 -- rejected link-local input.
        ("0.0.0.0", "240.0.0.1"),  # noqa: S104 -- rejected reserved input.
        ("0.0.0.0", "192.0.2.1"),  # noqa: S104 -- rejected documentation input.
        ("0.0.0.0", "100.64.0.1"),  # noqa: S104 -- rejected shared-space input.
        ("0.0.0.0", "127.0.0.1"),  # noqa: S104 -- wildcard must not publish loopback.
        ("172.15.255.255", "172.15.255.255"),
        ("172.32.0.0", "172.32.0.0"),
        ("192.168.1.20", "192.168.1.21"),
    ],
)
def test_enabled_grove_bridge_rejects_unsupported_ftps_topology(
    listen_host: str, advertised_ipv4: str
) -> None:
    with pytest.raises(ValidationError, match="direct private FTPS topology"):
        GroveBridgeConfig(
            enabled=True,
            listen_host=listen_host,
            ftps_advertised_ipv4=advertised_ipv4,
            tls_certificate_file=Path("/run/secrets/bridge.crt"),
            tls_private_key_file=Path("/run/secrets/bridge.key"),
        )


@pytest.mark.parametrize(
    ("listen_host", "advertised_ipv4"),
    [
        (1, "192.168.1.20"),
        ("127.0.0.1", 1),
        ("not-an-ip-address", "192.168.1.20"),
        ("127.0.0.1", "not-an-ip-address"),
    ],
)
def test_grove_bridge_topology_check_fails_closed_for_forged_values(
    listen_host: object, advertised_ipv4: object
) -> None:
    bridge = GroveBridgeConfig.model_construct(
        listen_host=listen_host,
        ftps_advertised_ipv4=advertised_ipv4,
    )

    assert bridge.has_supported_ftps_topology() is False


@pytest.mark.parametrize(
    "values",
    [
        {"listen_host": "localhost"},
        {"listen_host": "127.0.0.01"},
        {"listen_host": "::1"},
        {"ftps_advertised_ipv4": "0.0.0.0"},  # noqa: S104 -- rejected policy input.
        {"ftps_advertised_ipv4": "224.0.0.1"},
        {"ftps_advertised_ipv4": "192.0.2.020"},
        {"mqtt_journal_file": Path("relative.sqlite3")},
        {"staging_directory": Path("relative")},
        {"tls_certificate_file": Path("relative.crt")},
        {"ftps_passive_port_min": 50010, "ftps_passive_port_max": 50000},
        {"ftps_passive_port_min": 50000, "ftps_passive_port_max": 50064},
        {"mqtt_port": 990},
        {"mqtt_port": 50000},
        {"ftps_control_port": 50009},
        {"max_sessions": 1, "max_sessions_per_printer": 2},
        {
            "enabled": True,
            "listen_host": "192.168.1.20",
            "ftps_advertised_ipv4": "192.168.1.20",
            "tls_certificate_file": Path("/run/secrets/bridge.crt"),
            "tls_private_key_file": Path("/run/secrets/bridge.key"),
            "max_commands_per_session": 6,
        },
        {"staging_ttl_seconds": 59},
        {"staging_ttl_seconds": 86_401},
        {
            "mqtt_journal_file": Path("/var/lib/klove/ftps-staging/ingress.sqlite3"),
        },
    ],
)
def test_grove_bridge_rejects_ambiguous_or_overlapping_policy(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        GroveBridgeConfig.model_validate(values)


@pytest.mark.parametrize(
    "bridge",
    [
        GroveBridgeConfig(mqtt_journal_file=Path("/state/registry.sqlite3")),
        GroveBridgeConfig(staging_directory=Path("/state/start.sqlite3")),
        GroveBridgeConfig(mqtt_journal_file=Path("/state/secrets/ingress.sqlite3")),
        GroveBridgeConfig(staging_directory=Path("/state/secrets/staging")),
    ],
)
def test_bridge_storage_cannot_overlap_other_recovery_components(
    bridge: GroveBridgeConfig,
) -> None:
    with pytest.raises(ValidationError):
        AppConfig(
            api=ApiConfig(token_file=Path("token")),
            registry=RegistryConfig(
                database_file=Path("/state/registry.sqlite3"),
                secret_directory=Path("/state/secrets"),
            ),
            dispatch=DispatchConfig(journal_file=Path("/state/start.sqlite3")),
            grove_bridge=bridge,
        )


def test_onboarding_is_disabled_by_default_and_enabled_policy_is_complete() -> None:
    assert OnboardingConfig() == OnboardingConfig(
        enabled=False,
        allowed_grove_origins=(),
        session_capacity=128,
        session_inactivity_seconds=900,
        session_absolute_seconds=1800,
    )
    configured = OnboardingConfig(
        enabled=True,
        owner_credential_file=Path("/run/secrets/klove-owner"),
        allowed_grove_origins=("https://grove.example.test",),
    )
    assert configured.owner_credential_file == Path("/run/secrets/klove-owner")
    assert configured.allowed_grove_origins == ("https://grove.example.test",)
    assert configured.cookie_secure is True

    for values in (
        {"enabled": True, "allowed_grove_origins": ("https://grove.example.test",)},
        {"enabled": True, "owner_credential_file": Path("/run/secrets/owner")},
        {"owner_credential_file": Path("/run/secrets/owner")},
        {"allowed_grove_origins": ("https://grove.example.test",)},
        {"compatibility_host": "klove.example.test"},
        {"allow_loopback_http": True},
    ):
        with pytest.raises(ValidationError):
            OnboardingConfig.model_validate(values)


@pytest.mark.parametrize(
    "origin",
    [
        "",
        " https://grove.example.test",
        "https://gr\N{LATIN SMALL LETTER O WITH DIAERESIS}ve.example.test",
        "HTTPS://grove.example.test",
        "https://GROVE.example.test",
        "https://grove.example.test.",
        "https://grove.example.test/",
        "https://grove.example.test/path",
        "https://owner@grove.example.test",
        "https://grove.example.test?query=true",
        "https://grove.example.test#fragment",
        "https://grove.example.test:443",
        "https://grove.example.test:0",
        "https://grove.example.test:not-a-port",
        "https://[::ffff:127.0.0.1]",
        "https://[::1%25lo]",
        "https://*.example.test",
        "ftp://grove.example.test",
    ],
)
def test_grove_origins_require_one_exact_canonical_spelling(origin: str) -> None:
    with pytest.raises(ValidationError):
        OnboardingConfig(
            enabled=True,
            owner_credential_file=Path("/run/secrets/owner"),
            allowed_grove_origins=(origin,),
        )


def test_grove_origins_are_unique_and_bounded() -> None:
    common = {
        "enabled": True,
        "owner_credential_file": Path("/run/secrets/owner"),
    }
    with pytest.raises(ValidationError, match="unique and bounded"):
        OnboardingConfig.model_validate(
            {
                **common,
                "allowed_grove_origins": (
                    "https://grove.example.test",
                    "https://grove.example.test",
                ),
            }
        )
    with pytest.raises(ValidationError, match="unique and bounded"):
        OnboardingConfig.model_validate(
            {
                **common,
                "allowed_grove_origins": tuple(
                    f"https://grove-{index}.example.test" for index in range(33)
                ),
            }
        )


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost",
        "http://127.0.0.1:8080",
        "http://[::1]:8080",
    ],
)
def test_loopback_http_requires_the_explicit_development_exception(origin: str) -> None:
    values = {
        "enabled": True,
        "owner_credential_file": Path("/run/secrets/owner"),
        "allowed_grove_origins": (origin,),
    }
    with pytest.raises(ValidationError, match="development exception"):
        OnboardingConfig.model_validate(values)
    configured = OnboardingConfig.model_validate({**values, "allow_loopback_http": True})
    assert configured.allowed_grove_origins == (origin,)
    assert configured.cookie_secure is False


def test_loopback_exception_never_allows_remote_http() -> None:
    with pytest.raises(ValidationError, match="development exception"):
        OnboardingConfig(
            enabled=True,
            owner_credential_file=Path("/run/secrets/owner"),
            allowed_grove_origins=("http://grove.example.test",),
            allow_loopback_http=True,
        )


def test_secure_frame_origin_is_exact_distinct_and_development_bounded() -> None:
    assert OnboardingConfig.model_validate({"frame_origin": None}).frame_origin is None
    common = {
        "enabled": True,
        "owner_credential_file": Path("/run/secrets/owner"),
        "allowed_grove_origins": ("https://grove.example.test",),
    }
    configured = OnboardingConfig.model_validate(
        {
            **common,
            "frame_origin": "https://grove.example.test:8443",
            "compatibility_host": "klove.example.test",
        }
    )
    assert configured.frame_enabled
    assert configured.frame_origin == "https://grove.example.test:8443"
    assert configured.compatibility_host == "klove.example.test"

    for value in (
        "https://grove.example.test",
        "https://GROVE.example.test:8443",
        "https://grove.example.test:8443/",
        "http://grove.example.test:8443",
        "https://klove.example.test:8443",
    ):
        with pytest.raises(ValidationError):
            OnboardingConfig.model_validate({**common, "frame_origin": value})

    loopback = OnboardingConfig(
        enabled=True,
        owner_credential_file=Path("/run/secrets/owner"),
        allowed_grove_origins=("http://127.0.0.1:9011",),
        frame_origin="http://127.0.0.1:9010",
        compatibility_host="127.0.0.1",
        allow_loopback_http=True,
    )
    assert loopback.frame_enabled and loopback.cookie_secure is False


@pytest.mark.parametrize(
    "host",
    [
        "",
        "KLOVE.example.test",
        "klove.example.test.",
        "klove.example.test:80",
        "https://klove.example.test",
        "klove.example.test/path",
        "[::1]",
        "::1",
        "192.0.2.001",
        " 192.0.2.1",
    ],
)
def test_compatibility_host_requires_one_canonical_dns_or_ipv4_target(host: str) -> None:
    common = {
        "enabled": True,
        "owner_credential_file": Path("/run/secrets/owner"),
        "allowed_grove_origins": ("https://grove.example.test",),
        "frame_origin": "https://grove.example.test:8443",
    }
    with pytest.raises(ValidationError):
        OnboardingConfig.model_validate({**common, "compatibility_host": host})


def test_compatibility_host_is_required_only_for_embedded_onboarding() -> None:
    assert OnboardingConfig.model_validate({"compatibility_host": None}).compatibility_host is None
    common = {
        "enabled": True,
        "owner_credential_file": Path("/run/secrets/owner"),
        "allowed_grove_origins": ("https://grove.example.test",),
    }
    with pytest.raises(ValidationError, match="compatibility host"):
        OnboardingConfig.model_validate(
            {**common, "frame_origin": "https://grove.example.test:8443"}
        )
    with pytest.raises(ValidationError, match="compatibility host"):
        OnboardingConfig.model_validate({**common, "compatibility_host": "klove.example.test"})

    dns = OnboardingConfig.model_validate(
        {
            **common,
            "frame_origin": "https://grove.example.test:8443",
            "compatibility_host": "klove.example.test",
        }
    )
    ipv4 = OnboardingConfig.model_validate(
        {
            **common,
            "frame_origin": "https://grove.example.test:8443",
            "compatibility_host": "192.0.2.20",
        }
    )
    assert dns.compatibility_host == "klove.example.test"
    assert ipv4.compatibility_host == "192.0.2.20"


def test_secure_frame_rejects_parent_origin_reuse_or_a_different_trusted_site() -> None:
    common = {
        "enabled": True,
        "owner_credential_file": Path("/run/secrets/owner"),
        "allowed_grove_origins": ("https://grove.example.test",),
        "compatibility_host": "klove.example.test",
    }
    with pytest.raises(ValidationError, match="remain distinct"):
        OnboardingConfig.model_validate({**common, "frame_origin": "https://grove.example.test"})
    with pytest.raises(ValidationError, match="share one exact trusted-site"):
        OnboardingConfig.model_validate(
            {**common, "frame_origin": "https://other.example.test:8443"}
        )


@pytest.mark.parametrize(
    "values",
    [
        {"owner_credential_file": Path("relative-owner-secret")},
        {"session_capacity": 0},
        {"session_capacity": 10_001},
        {"session_inactivity_seconds": 0},
        {"session_inactivity_seconds": 901},
        {"session_absolute_seconds": 0},
        {"session_absolute_seconds": 1801},
        {"session_inactivity_seconds": 500, "session_absolute_seconds": 499},
    ],
)
def test_onboarding_paths_capacity_and_session_limits_are_bounded(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        OnboardingConfig.model_validate(values)


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


def test_dispatch_is_explicit_per_installation_and_printer() -> None:
    with pytest.raises(ValidationError, match=r"requires dispatch\.enabled"):
        AppConfig(
            api=ApiConfig(token_file=Path("token")),
            printers=(printer(dispatch_enabled=True),),
        )
    assert AppConfig(
        api=ApiConfig(token_file=Path("token")),
        dispatch=DispatchConfig(enabled=True),
        printers=(printer(dispatch_enabled=True),),
    )


@pytest.mark.parametrize(
    "values",
    [
        {"journal_file": Path("relative.sqlite3")},
        {"request_timeout_seconds": float("nan")},
        {"confirmation_timeout_seconds": float("nan")},
        {"poll_interval_seconds": float("nan")},
        {"confirmation_timeout_seconds": 1, "poll_interval_seconds": 2},
    ],
)
def test_dispatch_timing_and_journal_path_are_strict(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        DispatchConfig(**values)  # type: ignore[arg-type]


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
