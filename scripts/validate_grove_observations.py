#!/usr/bin/env python3
"""Fail closed on incomplete Grove black-box observation fixture provenance."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import NoReturn, TypeGuard

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_DIRECTORY = _ROOT / "tests" / "fixtures" / "grove-observations"
_README_NAME = "README.md"
_MANIFEST_SUFFIX = ".manifest.json"
_MAX_MANIFEST_BYTES = 32 * 1024
_MAX_FIXTURE_BYTES = 8 * 1024 * 1024
_UPSTREAM_REPOSITORY = "https://github.com/EdwardChamberlain/grove-control"
_UPSTREAM_COMMIT = "cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4"
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SAFE_MEDIA_TYPE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}")
_SAFE_TOOL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_HEX_REVISION = re.compile(r"[0-9a-f]{40}")
_UTC_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "fixture",
        "upstream",
        "observation",
        "capture",
        "generated_data",
        "sanitization_rules",
        "media_type",
        "byte_count",
        "sha256",
        "review",
        "retained_values",
    }
)
_UPSTREAM_KEYS = frozenset({"repository", "commit"})
_OBSERVATION_KEYS = frozenset({"component", "scenario"})
_CAPTURE_KEYS = frozenset({"harness_revision", "command", "tool_versions", "captured_at_utc"})
_REVIEW_KEYS = frozenset(
    {
        "uses_generated_fake_credentials",
        "contains_no_credentials",
        "contains_no_personal_data",
        "contains_no_source_excerpt",
        "contains_no_authored_error_prose",
        "contains_no_certificate_or_private_key",
        "contains_no_copied_upstream_fixture",
        "raw_trace_destroyed",
    }
)
_RETAINED_VALUE_KEYS = frozenset({"locator", "classification"})
_RETAINED_VALUE_CLASSIFICATIONS = frozenset({"required_protocol_token", "externally_observed_fact"})


class ValidationError(Exception):
    """A bounded error suitable for committed-fixture validation output."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _DuplicateJsonKey(Exception):
    pass


def _raise_invalid_constant(_value: str) -> NoReturn:
    raise ValueError


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _is_regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _is_safe_fixture_name(value: str) -> bool:
    return (
        _SAFE_NAME.fullmatch(value) is not None
        and value != _README_NAME
        and not value.endswith(_MANIFEST_SUFFIX)
    )


def _is_bounded_text(value: object, maximum: int) -> TypeGuard[str]:
    return (
        type(value) is str
        and 1 <= len(value) <= maximum
        and value.isascii()
        and all(32 <= ord(character) <= 126 for character in value)
    )


def _is_exact_mapping(value: object, keys: frozenset[str]) -> TypeGuard[dict[str, object]]:
    return type(value) is dict and set(value) == keys


def _read_manifest(path: Path) -> dict[str, object]:
    try:
        if path.is_symlink() or not _is_regular(path):
            raise ValidationError("manifest_not_regular")
        contents = path.read_bytes()
    except ValidationError:
        raise
    except OSError as exc:
        raise ValidationError("manifest_read_failed") from exc
    if len(contents) > _MAX_MANIFEST_BYTES:
        raise ValidationError("manifest_too_large")
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("manifest_not_utf8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_raise_invalid_constant,
        )
    except _DuplicateJsonKey as exc:
        raise ValidationError("manifest_json_duplicate_key") from exc
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError("manifest_json_invalid") from exc
    if type(value) is not dict:
        raise ValidationError("manifest_schema_invalid")
    return value


def _fixture_metadata(path: Path) -> tuple[int, str]:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ValidationError("fixture_not_regular")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            while chunk := source.read(64 * 1024):
                size += len(chunk)
                if size > _MAX_FIXTURE_BYTES:
                    raise ValidationError("fixture_too_large")
                digest.update(chunk)
        after = path.lstat()
    except ValidationError:
        raise
    except OSError as exc:
        raise ValidationError("fixture_read_failed") from exc
    if (
        not stat.S_ISREG(after.st_mode)
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or size != after.st_size
    ):
        raise ValidationError("fixture_changed_during_validation")
    return size, f"sha256:{digest.hexdigest()}"


def _valid_tool_versions(value: object) -> bool:
    if type(value) is not dict or not 1 <= len(value) <= 32:
        return False
    return all(
        type(name) is str
        and _SAFE_TOOL_NAME.fullmatch(name) is not None
        and _is_bounded_text(version, 256)
        for name, version in value.items()
    )


def _valid_utc_timestamp(value: object) -> bool:
    if type(value) is not str or _UTC_TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _valid_retained_values(value: object) -> bool:
    if type(value) is not list or not 1 <= len(value) <= 256:
        return False
    locators: set[str] = set()
    for item in value:
        if not _is_exact_mapping(item, _RETAINED_VALUE_KEYS):
            return False
        locator = item["locator"]
        classification = item["classification"]
        if (
            not _is_bounded_text(locator, 256)
            or type(classification) is not str
            or classification not in _RETAINED_VALUE_CLASSIFICATIONS
            or locator in locators
        ):
            return False
        locators.add(locator)
    return True


def _validate_manifest(fixture: Path, manifest_path: Path) -> None:
    manifest = _read_manifest(manifest_path)
    if not _is_exact_mapping(manifest, _MANIFEST_KEYS):
        raise ValidationError("manifest_schema_invalid")
    upstream = manifest["upstream"]
    observation = manifest["observation"]
    capture = manifest["capture"]
    review = manifest["review"]
    if not _is_exact_mapping(upstream, _UPSTREAM_KEYS):
        raise ValidationError("manifest_schema_invalid")
    if not _is_exact_mapping(observation, _OBSERVATION_KEYS):
        raise ValidationError("manifest_schema_invalid")
    if not _is_exact_mapping(capture, _CAPTURE_KEYS):
        raise ValidationError("manifest_schema_invalid")
    if not _is_exact_mapping(review, _REVIEW_KEYS):
        raise ValidationError("manifest_schema_invalid")
    if (
        type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["fixture"] != fixture.name
        or upstream["repository"] != _UPSTREAM_REPOSITORY
        or upstream["commit"] != _UPSTREAM_COMMIT
        or type(upstream["commit"]) is not str
        or _HEX_REVISION.fullmatch(upstream["commit"]) is None
        or not _is_bounded_text(observation["component"], 160)
        or not _is_bounded_text(observation["scenario"], 160)
        or type(capture["harness_revision"]) is not str
        or _HEX_REVISION.fullmatch(capture["harness_revision"]) is None
        or not _is_bounded_text(capture["command"], 1024)
        or not _valid_tool_versions(capture["tool_versions"])
        or not _valid_utc_timestamp(capture["captured_at_utc"])
        or manifest["generated_data"] is not True
        or type(manifest["sanitization_rules"]) is not list
        or not 1 <= len(manifest["sanitization_rules"]) <= 32
        or not all(_is_bounded_text(rule, 512) for rule in manifest["sanitization_rules"])
        or type(manifest["media_type"]) is not str
        or _SAFE_MEDIA_TYPE.fullmatch(manifest["media_type"]) is None
        or type(manifest["byte_count"]) is not int
        or manifest["byte_count"] < 0
        or type(manifest["sha256"]) is not str
        or _SHA256.fullmatch(manifest["sha256"]) is None
        or not all(value is True for value in review.values())
        or not _valid_retained_values(manifest["retained_values"])
    ):
        raise ValidationError("manifest_schema_invalid")
    size, digest = _fixture_metadata(fixture)
    if manifest["byte_count"] != size:
        raise ValidationError("fixture_size_mismatch")
    if manifest["sha256"] != digest:
        raise ValidationError("fixture_digest_mismatch")


def _read_fixture_entries(directory: Path) -> list[Path]:
    try:
        directory_status = directory.lstat()
        if directory.is_symlink() or not stat.S_ISDIR(directory_status.st_mode):
            raise ValidationError("fixture_directory_invalid")
        return sorted(directory.iterdir(), key=lambda item: item.name)
    except ValidationError:
        raise
    except OSError as exc:
        raise ValidationError("fixture_directory_invalid") from exc


def _classify_fixture_entries(entries: list[Path]) -> tuple[dict[str, Path], dict[str, Path]]:
    fixtures: dict[str, Path] = {}
    manifests: dict[str, Path] = {}
    for entry in entries:
        if entry.is_symlink():
            raise ValidationError("fixture_entry_symlink")
        if not _is_regular(entry):
            raise ValidationError("fixture_entry_not_regular")
        if entry.name == _README_NAME:
            continue
        if entry.name.endswith(_MANIFEST_SUFFIX):
            fixture_name = entry.name[: -len(_MANIFEST_SUFFIX)]
            if not _is_safe_fixture_name(fixture_name):
                raise ValidationError("manifest_name_unsafe")
            manifests[fixture_name] = entry
            continue
        if not _is_safe_fixture_name(entry.name):
            raise ValidationError("fixture_name_unsafe")
        fixtures[entry.name] = entry
    return fixtures, manifests


def validate(directory: Path) -> None:
    """Validate every immediate observation fixture and its sibling manifest."""
    fixtures, manifests = _classify_fixture_entries(_read_fixture_entries(directory))

    for fixture_name in sorted(manifests):
        if fixture_name not in fixtures:
            raise ValidationError("orphan_manifest")
    for fixture_name, fixture in sorted(fixtures.items()):
        manifest = manifests.get(fixture_name)
        if manifest is None:
            raise ValidationError("missing_manifest")
        _validate_manifest(fixture, manifest)


def main(arguments: list[str]) -> int:
    if len(arguments) > 1:
        raise ValidationError("argument_count_invalid")
    directory = _DEFAULT_DIRECTORY if not arguments else Path(arguments[0])
    validate(directory)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except ValidationError as error:
        print(f"grove observation validation failed: {error.code}", file=sys.stderr)
        raise SystemExit(1) from None
