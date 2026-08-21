from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
VALIDATOR = ROOT / "scripts" / "validate_grove_observations.py"
UPSTREAM_REPOSITORY = "https://github.com/EdwardChamberlain/grove-control"
UPSTREAM_COMMIT = "cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4"
HARNESS_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def run_validator(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- test invokes the interpreter with a repository-owned validator.
        [sys.executable, str(VALIDATOR), str(directory)],
        capture_output=True,
        check=False,
        encoding="utf-8",
        text=True,
    )


def write_readme(directory: Path) -> None:
    (directory / "README.md").write_text("observation fixtures\n", encoding="utf-8")


def manifest_for(name: str, payload: bytes) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "fixture": name,
        "upstream": {"repository": UPSTREAM_REPOSITORY, "commit": UPSTREAM_COMMIT},
        "observation": {"component": "MQTT client", "scenario": "connect"},
        "capture": {
            "harness_revision": HARNESS_COMMIT,
            "command": "python observe.py",
            "tool_versions": {"python": "3.12.0"},
            "captured_at_utc": "2026-08-21T12:34:56Z",
        },
        "generated_data": True,
        "sanitization_rules": ["generated fake credentials only"],
        "media_type": "application/json",
        "byte_count": len(payload),
        "sha256": f"sha256:{hashlib.sha256(payload).hexdigest()}",
        "review": {
            "uses_generated_fake_credentials": True,
            "contains_no_credentials": True,
            "contains_no_personal_data": True,
            "contains_no_source_excerpt": True,
            "contains_no_authored_error_prose": True,
            "contains_no_certificate_or_private_key": True,
            "contains_no_copied_upstream_fixture": True,
            "raw_trace_destroyed": True,
        },
        "retained_values": [
            {"locator": "$.connect.client_id", "classification": "required_protocol_token"}
        ],
    }


def write_observation(
    directory: Path, name: str = "connect.json"
) -> tuple[Path, dict[str, object]]:
    payload = b'{"event":"connect"}\n'
    fixture = directory / name
    fixture.write_bytes(payload)
    manifest = manifest_for(name, payload)
    (directory / f"{name}.manifest.json").write_text(
        json.dumps(manifest, separators=(",", ":")), encoding="utf-8"
    )
    return fixture, manifest


def rewrite_manifest(directory: Path, name: str, manifest: dict[str, object]) -> None:
    (directory / f"{name}.manifest.json").write_text(
        json.dumps(manifest, separators=(",", ":")), encoding="utf-8"
    )


def test_validator_accepts_one_complete_observation(tmp_path: Path) -> None:
    write_readme(tmp_path)
    write_observation(tmp_path)

    result = run_validator(tmp_path)

    assert result.returncode == 0
    assert result.stdout == "" and result.stderr == ""


def test_validator_rejects_missing_manifest_or_orphan(tmp_path: Path) -> None:
    write_readme(tmp_path)
    fixture, manifest = write_observation(tmp_path)
    (tmp_path / "connect.json.manifest.json").unlink()

    missing = run_validator(tmp_path)

    assert missing.returncode == 1
    assert missing.stdout == ""
    assert missing.stderr == "grove observation validation failed: missing_manifest\n"

    rewrite_manifest(tmp_path, fixture.name, manifest)
    fixture.unlink()
    orphan = run_validator(tmp_path)

    assert orphan.returncode == 1
    assert orphan.stderr == "grove observation validation failed: orphan_manifest\n"


def test_validator_rejects_duplicate_json_members_and_unsafe_entries(tmp_path: Path) -> None:
    write_readme(tmp_path)
    fixture, _manifest = write_observation(tmp_path)
    (tmp_path / f"{fixture.name}.manifest.json").write_text(
        '{"schema_version":1,"schema_version":1}', encoding="utf-8"
    )

    duplicate = run_validator(tmp_path)

    assert duplicate.returncode == 1
    assert duplicate.stderr == "grove observation validation failed: manifest_json_duplicate_key\n"

    (tmp_path / f"{fixture.name}.manifest.json").unlink()
    fixture.unlink()
    (tmp_path / ".unsafe").write_bytes(b"fixture")
    unsafe = run_validator(tmp_path)

    assert unsafe.returncode == 1
    assert unsafe.stderr == "grove observation validation failed: fixture_name_unsafe\n"


def test_validator_rejects_schema_and_fixture_integrity_changes(tmp_path: Path) -> None:
    write_readme(tmp_path)
    fixture, manifest = write_observation(tmp_path)
    manifest["generated_data"] = False
    rewrite_manifest(tmp_path, fixture.name, manifest)

    invalid_schema = run_validator(tmp_path)

    assert invalid_schema.returncode == 1
    assert invalid_schema.stderr == "grove observation validation failed: manifest_schema_invalid\n"

    valid_manifest = manifest_for(fixture.name, fixture.read_bytes())
    valid_manifest["byte_count"] = 0
    rewrite_manifest(tmp_path, fixture.name, valid_manifest)
    bad_size = run_validator(tmp_path)

    assert bad_size.returncode == 1
    assert bad_size.stderr == "grove observation validation failed: fixture_size_mismatch\n"

    valid_manifest["byte_count"] = len(fixture.read_bytes())
    valid_manifest["sha256"] = "sha256:" + "0" * 64
    rewrite_manifest(tmp_path, fixture.name, valid_manifest)
    bad_digest = run_validator(tmp_path)

    assert bad_digest.returncode == 1
    assert bad_digest.stderr == "grove observation validation failed: fixture_digest_mismatch\n"


def test_validator_rejects_symlink_and_too_large_manifests(tmp_path: Path) -> None:
    write_readme(tmp_path)
    fixture, _manifest = write_observation(tmp_path)
    (tmp_path / "linked.json").symlink_to(fixture)

    symlink = run_validator(tmp_path)

    assert symlink.returncode == 1
    assert symlink.stderr == "grove observation validation failed: fixture_entry_symlink\n"

    (tmp_path / "linked.json").unlink()
    (tmp_path / f"{fixture.name}.manifest.json").write_bytes(b" " * (32 * 1024 + 1))
    oversized = run_validator(tmp_path)

    assert oversized.returncode == 1
    assert oversized.stderr == "grove observation validation failed: manifest_too_large\n"


def test_validator_rejects_fixture_above_hard_byte_limit(tmp_path: Path) -> None:
    write_readme(tmp_path)
    payload = b"x" * (8 * 1024 * 1024 + 1)
    fixture = tmp_path / "large.json"
    fixture.write_bytes(payload)
    rewrite_manifest(tmp_path, fixture.name, manifest_for(fixture.name, payload))

    result = run_validator(tmp_path)

    assert result.returncode == 1
    assert result.stderr == "grove observation validation failed: fixture_too_large\n"


def test_validator_rejects_invalid_arguments_and_fixture_directory(tmp_path: Path) -> None:
    invalid_directory = run_validator(tmp_path / "missing")
    too_many = subprocess.run(  # noqa: S603 -- test invokes the interpreter with a repository-owned validator.
        [sys.executable, str(VALIDATOR), str(tmp_path), str(tmp_path)],
        capture_output=True,
        check=False,
        encoding="utf-8",
        text=True,
    )

    assert invalid_directory.returncode == 1
    assert (
        invalid_directory.stderr
        == "grove observation validation failed: fixture_directory_invalid\n"
    )
    assert too_many.returncode == 1
    assert too_many.stderr == "grove observation validation failed: argument_count_invalid\n"


def test_validator_rejects_malformed_nested_values_without_a_traceback(tmp_path: Path) -> None:
    write_readme(tmp_path)
    fixture, _manifest = write_observation(tmp_path)
    malformed_values: tuple[tuple[str, str | None, object], ...] = (
        ("schema_version", None, True),
        ("upstream", "commit", []),
        ("capture", "harness_revision", {}),
        ("retained_values", None, [{"locator": "$.client", "classification": []}]),
    )

    for parent, key, value in malformed_values:
        manifest = manifest_for(fixture.name, fixture.read_bytes())
        if parent in {"schema_version", "retained_values"}:
            manifest[parent] = value
        else:
            nested = manifest[parent]
            assert type(nested) is dict
            assert key is not None
            nested[key] = value
        rewrite_manifest(tmp_path, fixture.name, manifest)

        result = run_validator(tmp_path)

        assert result.returncode == 1
        assert result.stdout == ""
        assert result.stderr == "grove observation validation failed: manifest_schema_invalid\n"

    manifest = manifest_for(fixture.name, fixture.read_bytes())
    retained_values = manifest["retained_values"]
    assert type(retained_values) is list
    retained_values.append(retained_values[0])
    rewrite_manifest(tmp_path, fixture.name, manifest)

    duplicate_locator = run_validator(tmp_path)

    assert duplicate_locator.returncode == 1
    assert (
        duplicate_locator.stderr == "grove observation validation failed: manifest_schema_invalid\n"
    )
