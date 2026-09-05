"""Tests for scripts/licence_policy.py, the shipped-dependency licence gate (#172).

Loaded by file path, like tests/unit/test_ratos_emulation_contract.py loads its
runner: the module lives in scripts/, not the klove package, so pytest's
`pythonpath = ["src"]` does not make it importable by name.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "licence_policy.py"


def _load_licence_policy() -> ModuleType:
    module_name = f"_klove_licence_policy_{uuid.uuid4().hex}"
    specification = importlib.util.spec_from_file_location(module_name, SCRIPT)
    if specification is None or specification.loader is None:
        raise RuntimeError("could not load scripts/licence_policy.py")
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    try:
        specification.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


lp = _load_licence_policy()


def _info(
    name: str,
    version: str = "1.0.0",
    license_expression: str | None = None,
    license_field: str | None = None,
    classifiers: tuple[str, ...] = (),
) -> Any:
    return lp.DistributionLicenceInfo(
        name=name,
        version=version,
        license_expression=license_expression,
        license_field=license_field,
        classifiers=classifiers,
    )


# --- parse_policy ------------------------------------------------------------


def test_parse_policy_reads_both_lists() -> None:
    policy = lp.parse_policy(
        "allow-licenses:\n"
        "  - MIT\n"
        "  - Apache-2.0  # permissive\n"
        "allow-dependencies-licenses:\n"
        "  - pkg:pypi/example@1.2.3\n"
    )
    assert policy.allow_licenses == frozenset({"MIT", "Apache-2.0"})
    assert policy.allow_exact_exceptions == frozenset({("example", "1.2.3")})


def test_parse_policy_accepts_inline_empty_list() -> None:
    policy = lp.parse_policy("allow-licenses:\n  - MIT\nallow-dependencies-licenses: []\n")
    assert policy.allow_exact_exceptions == frozenset()


def test_parse_policy_fails_when_a_key_is_missing() -> None:
    with pytest.raises(ValueError, match="missing required key"):
        lp.parse_policy("allow-licenses:\n  - MIT\n")


def test_parse_policy_fails_on_unrecognised_line() -> None:
    with pytest.raises(ValueError, match="unrecognised line"):
        lp.parse_policy("allow-licenses:\n  - MIT\nallow-dependencies-licenses: []\ngarbage\n")


def test_parse_policy_fails_on_item_outside_a_key() -> None:
    with pytest.raises(ValueError, match="outside a known key"):
        lp.parse_policy("  - MIT\nallow-licenses:\nallow-dependencies-licenses: []\n")


def test_parse_purl_exception_normalizes_the_name() -> None:
    assert lp.parse_purl_exception("pkg:pypi/Some_Package@2.0") == ("some-package", "2.0")


def test_parse_purl_exception_rejects_wrong_ecosystem() -> None:
    with pytest.raises(ValueError, match="unsupported dependency exception PURL"):
        lp.parse_purl_exception("pkg:npm/example@1.0.0")


# --- SPDX expressions --------------------------------------------------------


def test_is_licence_allowed_single_id() -> None:
    assert lp.is_licence_allowed("MIT", {"MIT"}) is True
    assert lp.is_licence_allowed("GPL-3.0-only", {"MIT"}) is False


def test_is_licence_allowed_or_passes_if_any_branch_allowed() -> None:
    assert lp.is_licence_allowed("MIT OR GPL-3.0-only", {"MIT"}) is True
    assert lp.is_licence_allowed("GPL-3.0-only OR AGPL-3.0-only", {"MIT"}) is False


def test_is_licence_allowed_and_needs_every_part_allowed() -> None:
    assert lp.is_licence_allowed("Apache-2.0 AND MIT", {"Apache-2.0", "MIT"}) is True
    assert lp.is_licence_allowed("Apache-2.0 AND GPL-3.0-only", {"Apache-2.0", "MIT"}) is False


def test_is_licence_allowed_handles_parentheses_and_with() -> None:
    assert lp.is_licence_allowed("(MIT OR Apache-2.0) AND ISC", {"MIT", "ISC"}) is True
    assert lp.is_licence_allowed("GPL-2.0-only WITH Classpath-exception-2.0", set()) is False


def test_is_licence_allowed_fails_closed_on_garbage() -> None:
    assert lp.is_licence_allowed("", {"MIT"}) is False
    assert lp.is_licence_allowed("Apache License 2.0", {"Apache-2.0"}) is False
    assert lp.is_licence_allowed("(MIT", {"MIT"}) is False


# --- resolve_licence ----------------------------------------------------------


def test_resolve_licence_prefers_license_expression() -> None:
    info = _info("attrs", license_expression="MIT", license_field="Apache-2.0")
    assert lp.resolve_licence(info) == "MIT"


def test_resolve_licence_uses_recognisable_license_field() -> None:
    info = _info("idna", license_field="BSD-3-Clause")
    assert lp.resolve_licence(info) == "BSD-3-Clause"


def test_resolve_licence_uses_license_field_expression() -> None:
    info = _info("aiohttp", license_field="Apache-2.0 AND MIT")
    assert lp.resolve_licence(info) == "Apache-2.0 AND MIT"


def test_resolve_licence_falls_back_to_classifier_mapping() -> None:
    info = _info(
        "aiosignal",
        license_field="Apache 2.0",  # not a parseable SPDX expression
        classifiers=("License :: OSI Approved :: Apache Software License",),
    )
    assert lp.resolve_licence(info) == "Apache-2.0"


def test_resolve_licence_maps_every_documented_classifier() -> None:
    cases = {
        "License :: OSI Approved :: MIT License": "MIT",
        "License :: OSI Approved :: ISC License (ISCL)": "ISC",
        "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
        "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
    }
    for classifier, expected in cases.items():
        info = _info("pkg", classifiers=(classifier,))
        assert lp.resolve_licence(info) == expected


def test_resolve_licence_disambiguates_bsd_from_license_field() -> None:
    info = _info(
        "example",
        license_field="BSD 3-Clause License",
        classifiers=("License :: OSI Approved :: BSD License",),
    )
    assert lp.resolve_licence(info) == "BSD-3-Clause"


def test_resolve_licence_ambiguous_bsd_classifier_fails_closed() -> None:
    info = _info(
        "example",
        classifiers=("License :: OSI Approved :: BSD License",),
    )
    with pytest.raises(lp.LicenceUnresolved) as excinfo:
        lp.resolve_licence(info)
    assert "License=" in excinfo.value.raw_summary


def test_resolve_licence_missing_metadata_fails_closed() -> None:
    info = _info("example")
    with pytest.raises(lp.LicenceUnresolved) as excinfo:
        lp.resolve_licence(info)
    assert "License-Expression=None" in excinfo.value.raw_summary
    assert "License=None" in excinfo.value.raw_summary


def test_resolve_licence_unparseable_license_expression_fails_closed() -> None:
    info = _info("example", license_expression="not a real SPDX id")
    with pytest.raises(lp.LicenceUnresolved):
        lp.resolve_licence(info)


def test_resolve_licence_normalises_known_free_text_licenses() -> None:
    cases = {
        "Apache License 2.0": "Apache-2.0",  # multidict 6.7.1's actual metadata
        "Apache 2.0": "Apache-2.0",
        "Apache-2.0": "Apache-2.0",
        "Apache Software License 2.0": "Apache-2.0",
        "Apache License, Version 2.0": "Apache-2.0",
        "MIT License": "MIT",
        "MIT": "MIT",
        "BSD 3-Clause": "BSD-3-Clause",
        "BSD-3-Clause": "BSD-3-Clause",
        "3-Clause BSD License": "BSD-3-Clause",
        "ISC License": "ISC",
    }
    for license_field, expected in cases.items():
        info = _info("pkg", license_field=license_field)
        assert lp.resolve_licence(info) == expected, license_field


def test_resolve_licence_free_text_table_is_case_insensitive_and_trims_whitespace() -> None:
    info = _info("pkg", license_field="  apache license 2.0  ")
    assert lp.resolve_licence(info) == "Apache-2.0"


def test_resolve_licence_bare_bsd_or_apache_still_fails_closed() -> None:
    for license_field in ("BSD", "Apache", "GPL"):
        info = _info("pkg", license_field=license_field)
        with pytest.raises(lp.LicenceUnresolved):
            lp.resolve_licence(info)


def test_resolve_licence_free_text_table_ignored_when_license_expression_present() -> None:
    info = _info("pkg", license_expression="GPL-3.0-only", license_field="MIT License")
    assert lp.resolve_licence(info) == "GPL-3.0-only"


# --- check_site ---------------------------------------------------------------


def _write_dist_info(
    site_dir: Path,
    name: str,
    version: str,
    extra_metadata_lines: tuple[str, ...] = (),
) -> None:
    dist_info = site_dir / f"{name}-{version}.dist-info"
    dist_info.mkdir(parents=True)
    lines = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}", *extra_metadata_lines]
    (dist_info / "METADATA").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_check_site_allows_a_clean_tree(tmp_path: Path) -> None:
    _write_dist_info(tmp_path, "attrs", "26.1.0", ("License-Expression: MIT",))
    _write_dist_info(tmp_path, "idna", "3.18", ("License-Expression: BSD-3-Clause",))
    policy = lp.parse_policy(
        "allow-licenses:\n  - MIT\n  - BSD-3-Clause\nallow-dependencies-licenses: []\n"
    )
    checked, offenders = lp.check_site(tmp_path, policy)
    assert checked == 2
    assert offenders == []


def test_check_site_reports_a_disallowed_licence(tmp_path: Path) -> None:
    _write_dist_info(tmp_path, "copyleft_pkg", "1.0.0", ("License-Expression: GPL-3.0-only",))
    policy = lp.parse_policy("allow-licenses:\n  - MIT\nallow-dependencies-licenses: []\n")
    checked, offenders = lp.check_site(tmp_path, policy)
    assert checked == 1
    assert offenders == ["copyleft_pkg==1.0.0  GPL-3.0-only  → not allowed"]


def test_check_site_reports_missing_metadata(tmp_path: Path) -> None:
    _write_dist_info(tmp_path, "unlicensed_pkg", "1.0.0")
    policy = lp.parse_policy("allow-licenses:\n  - MIT\nallow-dependencies-licenses: []\n")
    checked, offenders = lp.check_site(tmp_path, policy)
    assert checked == 1
    assert len(offenders) == 1
    assert offenders[0].startswith("unlicensed_pkg==1.0.0  License-Expression=None")
    assert offenders[0].endswith("→ not allowed")


def test_check_site_exact_exception_matches(tmp_path: Path) -> None:
    _write_dist_info(tmp_path, "copyleft_pkg", "1.0.0", ("License-Expression: GPL-3.0-only",))
    policy = lp.parse_policy(
        "allow-licenses:\n  - MIT\nallow-dependencies-licenses:\n  - pkg:pypi/copyleft_pkg@1.0.0\n"
    )
    checked, offenders = lp.check_site(tmp_path, policy)
    assert checked == 1
    assert offenders == []


def test_check_site_exact_exception_does_not_cover_other_versions(tmp_path: Path) -> None:
    _write_dist_info(tmp_path, "copyleft_pkg", "2.0.0", ("License-Expression: GPL-3.0-only",))
    policy = lp.parse_policy(
        "allow-licenses:\n  - MIT\nallow-dependencies-licenses:\n  - pkg:pypi/copyleft_pkg@1.0.0\n"
    )
    checked, offenders = lp.check_site(tmp_path, policy)
    assert checked == 1
    assert offenders == ["copyleft_pkg==2.0.0  GPL-3.0-only  → not allowed"]


def test_check_site_skips_the_klove_distribution_itself(tmp_path: Path) -> None:
    _write_dist_info(tmp_path, "klove", "0.1.0.dev0", ("License-Expression: GPL-3.0-only",))
    policy = lp.parse_policy("allow-licenses:\n  - MIT\nallow-dependencies-licenses: []\n")
    checked, offenders = lp.check_site(tmp_path, policy)
    assert checked == 0
    assert offenders == []


# --- CLI ------------------------------------------------------------------------


def _write_policy(tmp_path: Path, allow_licenses: tuple[str, ...] = ("MIT",)) -> Path:
    policy_path = tmp_path / "licence-policy.yml"
    body = "allow-licenses:\n" + "".join(f"  - {lic}\n" for lic in allow_licenses)
    body += "allow-dependencies-licenses: []\n"
    policy_path.write_text(body, encoding="utf-8")
    return policy_path


def _run_cli(site_dir: Path, policy_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- test invokes the interpreter with a repository-owned script.
        [sys.executable, str(SCRIPT), "--site", str(site_dir), "--policy", str(policy_path)],
        capture_output=True,
        check=False,
        encoding="utf-8",
        text=True,
    )


def test_cli_fails_on_an_offending_package(tmp_path: Path) -> None:
    _write_dist_info(tmp_path, "evil_pkg", "1.0.0", ("License-Expression: GPL-3.0-only",))
    policy_path = _write_policy(tmp_path)
    result = _run_cli(tmp_path, policy_path)
    assert result.returncode == 1
    assert "evil_pkg==1.0.0  GPL-3.0-only  → not allowed" in result.stdout


def test_cli_passes_on_an_allowed_tree(tmp_path: Path) -> None:
    _write_dist_info(tmp_path, "good_pkg", "1.0.0", ("License-Expression: MIT",))
    policy_path = _write_policy(tmp_path)
    result = _run_cli(tmp_path, policy_path)
    assert result.returncode == 0
    assert "1 distribution(s) checked against the licence policy; all allowed." in result.stdout
