from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_read_only_slice_contains_no_printer_actuator_transport() -> None:
    forbidden_methods = {
        "machine.reboot",
        "machine.shutdown",
        "printer.emergency_stop",
        "printer.gcode.script",
        "printer.print.cancel",
        "printer.print.pause",
        "printer.print.resume",
        "printer.print.start",
    }
    package_text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((ROOT / "src" / "klove").rglob("*.py"))
    )
    assert not forbidden_methods.intersection(package_text.split('"'))


def test_native_api_exposes_only_get_routes() -> None:
    api_text = (ROOT / "src" / "klove" / "northbound" / "api.py").read_text(encoding="utf-8")
    route_methods = set(re.findall(r"app\.router\.add_([a-z]+)\(", api_text))
    assert route_methods == {"get"}


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
