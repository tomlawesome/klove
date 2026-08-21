from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import klove

ROOT = Path(__file__).parents[2]


def test_suite_imports_the_working_tree_package() -> None:
    package_file = Path(klove.__file__).resolve()
    assert package_file.is_relative_to((ROOT / "src" / "klove").resolve())


def test_actuators_are_exactly_confined_to_typed_adapters() -> None:
    forbidden_methods = {
        "machine.reboot",
        "machine.shutdown",
        "printer.emergency_stop",
        "printer.gcode.script",
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
    start_files = {
        path.relative_to(ROOT).as_posix()
        for path in production_files
        if "printer.print.start" in path.read_text(encoding="utf-8")
    }
    assert start_files == {"src/klove/adapters/moonraker/start.py"}


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


def test_grove_observation_manifest_gate_accepts_only_the_tracked_directory() -> None:
    result = subprocess.run(  # noqa: S603 -- test invokes the interpreter with a repository-owned validator.
        [sys.executable, str(ROOT / "scripts" / "validate_grove_observations.py")],
        cwd=ROOT,
        capture_output=True,
        check=False,
        encoding="utf-8",
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "" and result.stderr == ""


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
    browser = job("browser")
    assert "name: Secure embedded-frame browser boundary" in browser
    assert "needs: fast" in browser
    assert "permissions:\n      contents: read" in browser
    assert 'PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: "1"' in browser
    assert "actions/setup-node@a0853c24544627f65ddf259abe73b1d18a591444 # v5" in browser
    assert 'node-version: "22.17.0"' in browser
    assert "cache: npm" in browser and "cache-dependency-path: package-lock.json" in browser
    assert "run: npm ci" in browser
    assert "run: npx playwright install --with-deps chromium" in browser
    assert "run: sh scripts/test-browser.sh" in browser
    exact_container = job("exact-container")
    assert "if: github.event_name == 'push' && github.ref_name == 'preview'" in exact_container
    assert "needs: [fast, browser, moonraker-sim]" in exact_container
    assert 'candidate="preview-${GITHUB_RUN_NUMBER}-${GITHUB_RUN_ATTEMPT}"' in exact_container
    assert 'docker push "${image}:${candidate}"' in exact_container
    assert 'imagetools create --tag "${image}:preview" "${image}@${digest}"' in exact_container
    assert "needs: [fast, browser, moonraker-sim]" in job("verify-preview")


def test_container_and_compose_preserve_runtime_confinement() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    compose = (ROOT / "compose.example.yml").read_text(encoding="utf-8")
    from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
    assert len(from_lines) == 2
    assert all(re.search(r"@sha256:[0-9a-f]{64}(?:\s|$)", line) for line in from_lines)
    assert "USER 10001:10001" in dockerfile
    assert "install -d -o 10001 -g 10001 -m 0700 /var/lib/klove" in dockerfile
    assert dockerignore.startswith("*\n")
    assert "!Dockerfile" in dockerignore
    assert "!requirements.lock" in dockerignore
    assert "!src/**" in dockerignore
    assert "!config.toml" not in dockerignore
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "cap_drop:\n      - ALL" in compose
    assert "klove-state:/var/lib/klove" in compose
    assert "volumes:\n  klove-state:" in compose


def test_native_moonraker_simulation_is_confined_and_test_only() -> None:  # noqa: PLR0915 -- explicit safety-policy assertions are auditable.
    fixture = ROOT / "tests" / "integration" / "moonraker-sim"
    compose = (fixture / "compose.yml").read_text(encoding="utf-8")
    dockerfile = (fixture / "Dockerfile").read_text(encoding="utf-8")
    dispatch_dockerfile = (fixture / "dispatch.Dockerfile").read_text(encoding="utf-8")
    dispatch_dockerignore = (fixture / "dispatch.Dockerfile.dockerignore").read_text(
        encoding="utf-8"
    )
    host = (fixture / "fixture" / "host.py").read_text(encoding="utf-8")
    contract = (fixture / "fixture" / "exercise_contract.py").read_text(encoding="utf-8")
    dispatch = (fixture / "fixture" / "dispatch_contract.py").read_text(encoding="utf-8")
    dispatch_reconnect = (fixture / "fixture" / "dispatch_reconnect_contract.py").read_text(
        encoding="utf-8"
    )
    sdcard_reset = (fixture / "fixture" / "sdcard_reset.py").read_text(encoding="utf-8")
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
    assert compose.count("read_only: true") == 7
    assert compose.count("cap_drop:\n      - ALL") == 7
    assert compose.count("no-new-privileges:true") == 7
    assert compose.count("pids_limit:") == 7
    assert compose.count("mem_limit:") == 7
    assert compose.count("cpus:") == 7
    assert compose.count("runtime-state:/run/printer-state") == 1
    assert "runtime-secrets:/run/klove-secrets:ro" in compose
    assert compose.count("dispatch-state:/var/lib/klove") == 2

    from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
    assert len(from_lines) == 2
    assert all(re.search(r"@sha256:[0-9a-f]{64}(?:\s|$)", line) for line in from_lines)
    assert re.search(r"ARG KLIPPER_COMMIT=[0-9a-f]{40}$", dockerfile, re.MULTILINE)
    assert re.search(r"ARG MOONRAKER_COMMIT=[0-9a-f]{40}$", dockerfile, re.MULTILINE)
    assert "git -C /opt/klipper rev-parse HEAD" in dockerfile
    assert "git -C /opt/moonraker rev-parse HEAD" in dockerfile
    assert "sha256sum --check /fixture/SHA256SUMS" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "FROM klove-moonraker-sim-klove:local" in dispatch_dockerfile
    assert "USER 10001:10001" in dispatch_dockerfile
    assert "/fixture/dispatch_contract.py" in dispatch_dockerfile
    assert "/fixture/dispatch_reconnect_contract.py" in dispatch_dockerfile
    assert "/fixture/sdcard_reset.py" in dispatch_dockerfile
    assert dispatch_dockerignore.startswith("*\n")
    assert "!tests/integration/moonraker-sim/fixture/dispatch_contract.py" in dispatch_dockerignore
    assert (
        "!tests/integration/moonraker-sim/fixture/dispatch_reconnect_contract.py"
        in dispatch_dockerignore
    )
    assert "!tests/integration/moonraker-sim/fixture/sdcard_reset.py" in dispatch_dockerignore

    assert 'MOONRAKER = "http://127.0.0.1:7125"' in host
    assert 'STATE_DIR = Path("/run/printer-state")' in host
    assert "os.O_WRONLY | os.O_CREAT | os.O_EXCL" in host
    assert "0o600" in host
    assert "printer/print/start" not in host
    assert "printer/print/start" in contract
    assert "DispatchCoordinator" in dispatch
    assert "MoonrakerUploadTransport" in dispatch
    assert "MoonrakerStartTransport" in dispatch
    assert "printer.gcode.script" not in dispatch
    assert "coordinator.submit" not in dispatch_reconnect
    assert "printer.gcode.script" not in dispatch_reconnect
    assert sdcard_reset.count("printer/gcode/script") == 1
    assert sdcard_reset.count("SDCARD_RESET_FILE") == 1
    assert "script: str" not in sdcard_reset
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


def test_ratos_emulation_toolchain_is_exact() -> None:
    fixture = ROOT / "tests" / "integration" / "ratos-emulation"
    dockerfile = (fixture / "Dockerfile").read_text(encoding="utf-8")
    debian_sources = (fixture / "debian.sources").read_text(encoding="utf-8")
    tool = (fixture / "tool.py").read_text(encoding="utf-8")

    assert re.search(
        r"^# syntax=docker/dockerfile:1\.7@sha256:[0-9a-f]{64}$", dockerfile, re.MULTILINE
    )
    from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
    assert len(from_lines) == 1
    assert re.search(r"@sha256:[0-9a-f]{64}$", from_lines[0])
    for package in (
        "mtools=4.0.33-1+really4.0.32-1",
        "qemu-system-arm=1:7.2+dfsg-7+deb12u18+b3",
        "qemu-utils=1:7.2+dfsg-7+deb12u18+b3",
        "xz-utils=5.4.1-1+deb12u1",
    ):
        assert package in dockerfile
    assert "QEMU emulator version 7.2.22" in dockerfile
    assert "tests/integration/ratos-emulation/tool.py /opt/klove-ratos/tool.py" in dockerfile
    assert "ARG KLOVE_SOURCE_REVISION=unknown" in dockerfile
    assert "ARG KLOVE_SOURCE_DIGEST=unknown" in dockerfile
    assert 'io.klove.source-revision="$KLOVE_SOURCE_REVISION"' in dockerfile
    assert 'io.klove.source-digest="$KLOVE_SOURCE_DIGEST"' in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert debian_sources.count("snapshot.debian.org/archive/debian/20260803T000000Z") == 1
    assert debian_sources.count("snapshot.debian.org/archive/debian-security/20260803T000000Z") == 1
    assert debian_sources.count("Check-Valid-Until: no") == 2
    assert "deb.debian.org" not in debian_sources

    assert "ASSET_SIZE: Final = 2_125_243_800" in tool
    assert "513465cf6b233d73e5c9ed048568493894d77c7c67e93586609f9b7fbc182153" in tool
    assert "63e95ea4f7a362a1fa9adf210d8f069c671341fb8531a4595f35e2ff6e4a1f51" in tool
    assert "6cc7aad62b32a1efa774955edc10ea302ae015a58abbea683c23287839b097f8" in tool
    assert "83eefb232fe362a75a8cced8d3f5f17af4398e1d121d486e45eff8937a86b0a1" in tool
    assert '_run([QEMU_IMG, "check", str(OVERLAY)])' in tool
    assert "each evidence run must start fresh" in tool
    assert "EXPECTED_MOONRAKER_VERSION" in tool
    assert "EXPECTED_DISTRIBUTION" in tool
    assert "EXPECTED_RATOS_VERSION" in tool
    assert "EXPECTED_KERNEL" in tool
    assert "EXPECTED_MODEL" in tool
    assert "EXPECTED_SERVICES" in tool
    assert 'service.get("active_state") != "active"' in tool
    assert 'service.get("sub_state") != "running"' in tool
    assert 'HTTPConnection("127.0.0.1", port' in tool
    assert tool.count("_http_request(\n        18080,") >= 2
    assert 'create_connection(("127.0.0.1", 12222)' in tool


def test_ratos_contract_fixture_is_exact_and_non_actuating() -> None:
    fixture = ROOT / "tests" / "integration" / "ratos-emulation"
    dockerfile = (fixture / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (fixture / "Dockerfile.dockerignore").read_text(encoding="utf-8")
    tool = (fixture / "tool.py").read_text(encoding="utf-8")
    printer_config = (fixture / "contract" / "printer.cfg").read_text(encoding="utf-8")
    klove_config = (fixture / "contract" / "klove.toml").read_text(encoding="utf-8")

    assert "tests/integration/moonraker-sim/fixture/proxy.py" in dockerfile
    assert "tests/integration/moonraker-sim/fixture/exercise_contract.py" in dockerfile
    assert "tests/integration/ratos-emulation/contract/ratos_exercise_contract.py" in dockerfile
    assert "tests/integration/moonraker-sim/fixture/contract.gcode" in dockerfile
    assert "sha256sum --check /opt/klove-ratos/contract/SHA256SUMS" in dockerfile
    assert dockerignore.startswith("*\n")
    assert "!tests/integration/ratos-emulation/tool.py" in dockerignore
    assert "!tests/integration/moonraker-sim/fixture/proxy.py" in dockerignore
    assert "EXPECTED_MCU_VERSION" in tool
    assert "EXPECTED_CONTRACT_COUNTS" in tool
    assert "def init_secrets()" in tool
    assert "contract secret volume must be empty" in tool
    assert "contract secret volume must be empty before preparation" in tool
    assert "identity != (10001, 10001, 0o600)" in tool
    assert 'expected_phase="standby"' in tool
    assert 'expected_phase="cancelled"' in tool
    assert 'filename="contract.gcode"' in tool
    assert "_untrust_moonraker_clients" in tool
    assert "RatOS did not reject an invalid Moonraker API key" in tool
    assert "terminal Moonraker history identity differs from faulted-pause evidence" in tool
    assert "secrets.compare_digest(actual, expected)" in tool
    assert "os.O_NOFOLLOW" in tool
    assert "serial: /tmp/klipper_host_mcu" in printer_config
    assert "kinematics: none" in printer_config
    assert "[pause_resume]" in printer_config
    assert "printer.gcode.script" not in printer_config
    assert 'endpoint = "http://127.0.0.1:27125"' in klove_config
    assert "request_timeout_seconds = 20.0" in klove_config
    assert 'token_file = "/run/klove-secrets/klove-token"' in klove_config


def test_ratos_emulation_lifecycle_is_confined() -> None:
    script_library = (ROOT / "scripts" / "ratos-emulation-lib.sh").read_text(encoding="utf-8")
    up = (ROOT / "scripts" / "ratos-emulation-up.sh").read_text(encoding="utf-8")
    probe = (ROOT / "scripts" / "ratos-emulation-probe.sh").read_text(encoding="utf-8")
    down = (ROOT / "scripts" / "ratos-emulation-down.sh").read_text(encoding="utf-8")

    assert "name=rootless" in script_library
    assert "GNU coreutils on Linux" in script_library
    assert "docker context show" in script_library
    assert "docker info --format '{{.ID}}'" in script_library
    assert "ratos_require_private_dir" in script_library
    assert "ratos_require_private_file" in script_library
    assert "ratos_require_absent" in script_library
    assert "ratos_actual_image" in script_library
    assert "ratos_require_container_confinement" in script_library
    assert "ratos_require_mount" in script_library
    assert "ratos_require_container_command" in script_library
    assert "ratos_require_active" in probe
    assert "ratos_require_active" in down
    for confinement in (
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--pids-limit 256",
        "--memory 3g",
        "--cpus 4",
    ):
        assert confinement in up
    assert "--privileged" not in up
    assert "--user 0:0" not in up
    assert "--publish" not in up
    assert "/var/run/docker.sock" not in up
    assert "--device" not in up
    assert "--log-driver local" in up
    assert "--log-opt max-size=1m" in up
    assert "--log-opt max-file=2" in up
    assert "restrict=on" in up
    assert up.count("hostfwd=tcp:127.0.0.1:") == 3
    assert up.count(',readonly"') == 3
    assert "target=/work" not in up
    assert 'target=/run-state/$ratos_overlay_name"' in up
    assert "-no-reboot" in up
    assert "-sandbox on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny" in up
    assert "firstboot-required" in up
    assert "firstboot-required" in probe
    assert "firstboot-restarted" in probe
    assert probe.count("docker start") == 1
    assert "firstboot_timeout_seconds=600" in probe
    assert "service_timeout_seconds=1200" in probe
    assert "deadline=$(( $(date +%s) + firstboot_timeout_seconds ))" in probe
    assert "deadline=$(( $(date +%s) + service_timeout_seconds ))" in probe
    assert 'if ! timeout 15 docker logs "$ratos_container" > "$reboot_log" 2>&1' in probe
    assert 'docker logs "$ratos_container" 2>&1 |' not in probe
    assert "reboot: Restarting system" in probe
    assert "did not complete first boot within the fixed deadline" in probe
    assert "did not expose the service contract within the fixed post-reboot deadline" in probe
    assert "retained guarded runtime state" in up
    assert "ratos_run_overlay_tool check-overlay" in down
    assert "docker logs" not in down
    assert "console" not in down


def test_ratos_cleanup_traps_exit_on_signals() -> None:
    script_paths = (
        ROOT / "scripts" / "ratos-emulation-prepare.sh",
        ROOT / "scripts" / "ratos-emulation-up.sh",
        ROOT / "scripts" / "ratos-emulation-probe.sh",
        ROOT / "scripts" / "ratos-emulation-contract.sh",
        ROOT / "scripts" / "test-ratos-emulation.sh",
    )

    for script_path in script_paths:
        script = script_path.read_text(encoding="utf-8")
        combined_traps = [
            line
            for line in script.splitlines()
            if line.startswith("trap ") and line.endswith(" EXIT HUP INT TERM")
        ]
        assert "trap 'exit 1' HUP INT TERM" in script
        assert all(line == "trap - EXIT HUP INT TERM" for line in combined_traps)


def test_ratos_contract_runner_command_paths_match_and_are_exact() -> None:
    contract_script = (ROOT / "scripts" / "ratos-emulation-contract.sh").read_text(encoding="utf-8")
    library = (ROOT / "scripts" / "ratos-emulation-lib.sh").read_text(encoding="utf-8")

    expected_command = "/opt/klove-ratos/contract/ratos_exercise_contract.py"
    launcher_command = f"/usr/local/bin/python {expected_command}"
    verifier_command = f'"/usr/local/bin/python","{expected_command}"'

    assert launcher_command in contract_script
    assert verifier_command in library


def test_ratos_contract_lifecycle_is_confined_and_exactly_torn_down() -> None:
    up = (ROOT / "scripts" / "ratos-emulation-up.sh").read_text(encoding="utf-8")
    contract = (ROOT / "scripts" / "ratos-emulation-contract.sh").read_text(encoding="utf-8")
    down = (ROOT / "scripts" / "ratos-emulation-down.sh").read_text(encoding="utf-8")

    assert contract.count("--log-opt max-file=2") == 3
    assert "--env KLOVE_TEST_MOONRAKER_AUTH_EXPECTATION=trusted" not in contract
    assert "/opt/klove-ratos/contract/ratos_exercise_contract.py" in contract
    assert 'docker exec --interactive "$ratos_container"' in contract
    wrapper = (ROOT / "scripts" / "test-ratos-emulation.sh").read_text(encoding="utf-8")

    assert "ratos_require_active" in contract
    assert 'target=/run/klove-secrets"' in up
    assert "io.klove.ratos.purpose=contract-secrets" in up
    assert "--user 10001:10001" in up
    assert "/opt/klove-ratos/tool.py init-secrets" in up
    assert up.index("--cap-drop ALL") < up.index("/opt/klove-ratos/tool.py init-secrets")
    assert 'docker volume rm "$ratos_secret_volume"' in down
    assert "secrets and COW destroyed; bounded evidence retained" in down
    assert "ratos_require_recovery_origin" in down
    assert ">/dev/null 2>&1 || true" not in wrapper
    assert "RatOS automatic teardown failed" in wrapper

    assert contract.count('--network "container:$ratos_container"') == 3
    assert contract.count("--cap-drop ALL") == 3
    assert contract.count("--read-only") == 3
    assert "--privileged" not in contract
    assert "--publish" not in contract
    assert "/var/run/docker.sock" not in contract
    assert 'target=/run/klove-secrets,readonly"' in contract
    assert "KLOVE_TEST_PROXY_UPSTREAM_URL=http://127.0.0.1:18080" in contract
    assert "KLOVE_TEST_MOONRAKER_HOST_HEADER=ratos.local" in contract
    assert "/opt/klove-ratos/tool.py contract-prepare" in contract
    assert "/opt/klove-ratos/tool.py contract-evidence" in contract
    assert "/opt/klove-ratos/contract/ratos_exercise_contract.py" in contract
    assert "ratos_require_contract_container \\" in contract
    assert '"$repo_root/scripts/ratos-emulation-contract.sh"' in wrapper


def test_ratos_emulation_evidence_is_self_binding() -> None:
    script_library = (ROOT / "scripts" / "ratos-emulation-lib.sh").read_text(encoding="utf-8")
    prepare = (ROOT / "scripts" / "ratos-emulation-prepare.sh").read_text(encoding="utf-8")
    probe = (ROOT / "scripts" / "ratos-emulation-probe.sh").read_text(encoding="utf-8")
    contract = (ROOT / "scripts" / "ratos-emulation-contract.sh").read_text(encoding="utf-8")
    down = (ROOT / "scripts" / "ratos-emulation-down.sh").read_text(encoding="utf-8")

    assert "ratos_capture_source_state" in script_library
    assert "ratos_stored_source_digest" in script_library
    assert "io.klove.source-digest" in script_library
    assert '--build-arg "KLOVE_SOURCE_REVISION=$ratos_source_revision"' in prepare
    assert '--build-arg "KLOVE_SOURCE_DIGEST=$ratos_source_digest"' in prepare
    assert '--build-arg "SOURCE_DIGEST=$ratos_source_digest"' in prepare
    assert "ratos_klove_image_id=$(timeout 15 docker image inspect" in prepare
    assert "ratos_archive_runtime" in script_library
    assert 'cp -- "$ratos_origin" "$ratos_archived_run/origin"' in script_library
    assert 'identities.json" "$ratos_archived_run/identities.json"' in script_library
    assert "origin_sha256=%s" in script_library
    assert "identities_sha256=%s" in script_library
    assert "overlay_retained=false" in script_library
    assert 'rm -f -- "$ratos_runtime_dir/$ratos_overlay_name"' in script_library
    assert "overlay.qcow2" not in script_library
    assert "ratos_secret_volume=klove-ratos-v2-1-0-secrets" in script_library
    assert "ratos_require_secret_volume" in script_library
    assert "ratos_require_contract_container" in script_library
    assert "ratos_require_recovery_origin" in script_library
    assert 'mv -- "$probe_success_partial" "$ratos_probe_succeeded"' in probe
    assert probe.index('cat "$ratos_probe"') < probe.index(
        'mv -- "$probe_success_partial" "$ratos_probe_succeeded"'
    )
    assert 'if [ -e "$ratos_probe_succeeded" ]' in down
    assert 'ratos_require_private_file "$ratos_probe_succeeded" 600' in down
    assert 'mv -- "$prepared_partial" "$ratos_contract_prepared"' in contract
    assert 'mv -- "$contract_partial" "$ratos_contract_evidence"' in contract
    assert contract.index('cat "$ratos_contract_evidence"') < contract.index(
        'mv -- "$contract_success_partial" "$ratos_contract_succeeded"'
    )


def test_ratos_emulation_remains_supplemental() -> None:
    fixture = ROOT / "tests" / "integration" / "ratos-emulation"
    readme = (fixture / "README.md").read_text(encoding="utf-8")
    workflows = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    )

    assert "A later run may reuse" not in readme
    assert "Linux" in readme
    assert "GNU coreutils" in readme
    assert "Linux-process host MCU" in readme
    assert "not a physical-printer acceptance test" in readme
    assert "test-ratos-emulation" not in workflows


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
