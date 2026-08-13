from __future__ import annotations

import re
from pathlib import Path

import klove

ROOT = Path(__file__).parents[2]


def test_suite_imports_the_working_tree_package() -> None:
    package_file = Path(klove.__file__).resolve()
    assert package_file.is_relative_to((ROOT / "src" / "klove").resolve())


def test_control_slice_contains_only_dedicated_job_actuators() -> None:
    forbidden_methods = {
        "machine.reboot",
        "machine.shutdown",
        "printer.emergency_stop",
        "printer.gcode.script",
        "printer.print.start",
    }
    production_files = sorted((ROOT / "src" / "klove").rglob("*.py"))
    package_text = "\n".join(path.read_text(encoding="utf-8") for path in production_files)
    assert not any(method in package_text for method in forbidden_methods)
    actuator_files = {
        path.relative_to(ROOT).as_posix()
        for path in production_files
        if any(
            method in path.read_text(encoding="utf-8")
            for method in (
                "printer.print.cancel",
                "printer.print.pause",
                "printer.print.resume",
            )
        )
    }
    assert actuator_files == {"src/klove/adapters/moonraker/control.py"}


def test_artifact_contract_and_validation_slices_contain_no_transport_or_extraction() -> None:
    contract_text = (ROOT / "src" / "klove" / "domain" / "artifacts.py").read_text(encoding="utf-8")
    validator_text = (ROOT / "src" / "klove" / "domain" / "artifact_validation.py").read_text(
        encoding="utf-8"
    )

    assert not any(
        token in contract_text
        for token in (
            "aiohttp",
            "httpx",
            "requests",
            "server/files/upload",
            "tarfile",
            "zipfile",
        )
    )
    assert not any(
        token in validator_text
        for token in (
            ".extract(",
            ".extractall(",
            "aiohttp",
            "httpx",
            "requests",
            "server/files/upload",
            "socket",
            "tempfile",
            "urllib",
        )
    )


def test_native_api_exposes_only_get_and_one_typed_post_route() -> None:
    api_text = (ROOT / "src" / "klove" / "northbound" / "api.py").read_text(encoding="utf-8")
    route_methods = set(re.findall(r"app\.router\.add_([a-z]+)\(", api_text))
    assert route_methods == {"get", "post"}
    assert api_text.count("app.router.add_post(") == 1


def test_all_github_actions_are_pinned_to_full_commit_shas() -> None:
    uses_pattern = re.compile(r"^\s*-?\s*uses:\s*[^@\s]+@([^\s#]+)", re.MULTILINE)
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows
    for workflow in workflows:
        references = uses_pattern.findall(workflow.read_text(encoding="utf-8"))
        assert references, workflow
        assert all(re.fullmatch(r"[0-9a-f]{40}", reference) for reference in references), workflow


def test_container_and_compose_preserve_runtime_confinement() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "compose.example.yml").read_text(encoding="utf-8")
    from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
    assert len(from_lines) == 2
    assert all(re.search(r"@sha256:[0-9a-f]{64}(?:\s|$)", line) for line in from_lines)
    assert "USER 10001:10001" in dockerfile
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "cap_drop:\n      - ALL" in compose


def test_dependency_locks_require_hashes() -> None:
    for name in ("requirements.lock", "requirements-build.lock", "requirements-dev.lock"):
        lock = (ROOT / name).read_text(encoding="utf-8")
        requirements = [
            block
            for block in re.split(r"\n(?=[a-zA-Z0-9])", lock)
            if block.strip() and not block.lstrip().startswith(("#", "--"))
        ]
        assert requirements
        assert all("--hash=sha256:" in requirement for requirement in requirements)
