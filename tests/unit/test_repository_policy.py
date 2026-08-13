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


def test_preview_publication_requires_native_moonraker_integration() -> None:
    workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
    jobs = workflow.split("\njobs:\n", 1)[1]

    def job(name: str) -> str:
        match = re.search(rf"^  {re.escape(name)}:\n", jobs, re.MULTILINE)
        assert match is not None
        following = re.search(r"^  [a-z0-9-]+:\n", jobs[match.end() :], re.MULTILINE)
        end = len(jobs) if following is None else match.end() + following.start()
        return jobs[match.start() : end]

    integration = job("moonraker-sim")
    assert "name: Native Moonraker integration" in integration
    assert "needs: fast" in integration
    assert "timeout-minutes: 35" in integration
    assert "permissions:\n      contents: read" in integration
    assert 'CI: "true"' in integration
    assert 'KLOVE_SIM_ALLOW_ROOTFUL_CI: "1"' in integration
    assert "run: sh scripts/test-moonraker-sim.sh" in integration
    exact_container = job("exact-container")
    assert "if: github.event_name == 'push' && github.ref_name == 'preview'" in exact_container
    assert "needs: [fast, moonraker-sim]" in exact_container
    assert 'candidate="preview-${GITHUB_RUN_NUMBER}-${GITHUB_RUN_ATTEMPT}"' in exact_container
    assert 'docker push "${image}:${candidate}"' in exact_container
    assert 'imagetools create --tag "${image}:preview" "${image}@${digest}"' in exact_container
    assert "needs: [fast, moonraker-sim]" in job("verify-preview")


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


def test_native_moonraker_simulation_is_confined_and_test_only() -> None:
    fixture = ROOT / "tests" / "integration" / "moonraker-sim"
    compose = (fixture / "compose.yml").read_text(encoding="utf-8")
    dockerfile = (fixture / "Dockerfile").read_text(encoding="utf-8")
    host = (fixture / "fixture" / "host.py").read_text(encoding="utf-8")
    contract = (fixture / "fixture" / "exercise_contract.py").read_text(encoding="utf-8")
    moonraker = (fixture / "fixture" / "moonraker.conf").read_text(encoding="utf-8")
    script_library = (ROOT / "scripts" / "moonraker-sim-lib.sh").read_text(encoding="utf-8")
    up = (ROOT / "scripts" / "moonraker-sim-up.sh").read_text(encoding="utf-8")
    down = (ROOT / "scripts" / "moonraker-sim-down.sh").read_text(encoding="utf-8")

    assert "internal: true" in compose
    assert "\n    ports:" not in compose
    assert "privileged:" not in compose
    assert "network_mode:" not in compose
    assert "container_name:" not in compose
    assert "/var/run/docker.sock" not in compose
    assert "/dev/" not in compose
    assert compose.count("read_only: true") == 5
    assert compose.count("cap_drop:\n      - ALL") == 5
    assert compose.count("no-new-privileges:true") == 5
    assert compose.count("pids_limit:") == 5
    assert compose.count("mem_limit:") == 5
    assert compose.count("cpus:") == 5
    assert compose.count("runtime-state:/run/printer-state") == 1
    assert "runtime-secrets:/run/klove-secrets:ro" in compose

    from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
    assert len(from_lines) == 2
    assert all(re.search(r"@sha256:[0-9a-f]{64}(?:\s|$)", line) for line in from_lines)
    assert re.search(r"ARG KLIPPER_COMMIT=[0-9a-f]{40}$", dockerfile, re.MULTILINE)
    assert re.search(r"ARG MOONRAKER_COMMIT=[0-9a-f]{40}$", dockerfile, re.MULTILINE)
    assert "git -C /opt/klipper rev-parse HEAD" in dockerfile
    assert "git -C /opt/moonraker rev-parse HEAD" in dockerfile
    assert "sha256sum --check /fixture/SHA256SUMS" in dockerfile
    assert "USER 10001:10001" in dockerfile

    assert 'MOONRAKER = "http://127.0.0.1:7125"' in host
    assert 'STATE_DIR = Path("/run/printer-state")' in host
    assert "os.O_WRONLY | os.O_CREAT | os.O_EXCL" in host
    assert "0o600" in host
    assert "printer/print/start" not in host
    assert "printer/print/start" in contract
    assert "  127.0.0.1" in moonraker
    assert "0.0.0.0/0" not in moonraker
    assert "docker context show" in script_library
    assert "docker info --format '{{.ID}}'" in script_library
    assert "moonraker_sim_validate_run_id" in script_library
    assert "KLOVE_SIM_ALLOW_ROOTFUL_CI" in up
    assert '"${CI:-}" != "true"' in up
    assert "timeout 900 docker compose" in up
    assert "retained recovery state for RUN_ID" in up
    assert "--profile contract down" in down


def test_dependency_locks_require_hashes() -> None:
    integration_locks = sorted(
        (ROOT / "tests" / "integration" / "moonraker-sim").glob("requirements-*.lock")
    )
    lock_paths = [
        ROOT / "requirements.lock",
        ROOT / "requirements-build.lock",
        ROOT / "requirements-dev.lock",
        *integration_locks,
    ]
    assert len(integration_locks) == 3
    for path in lock_paths:
        lock = path.read_text(encoding="utf-8")
        requirements = [
            block
            for block in re.split(r"\n(?=[a-zA-Z0-9])", lock)
            if block.strip() and not block.lstrip().startswith(("#", "--"))
        ]
        assert requirements
        assert all("--hash=sha256:" in requirement for requirement in requirements)
