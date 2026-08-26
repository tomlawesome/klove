from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEST = ROOT / "scripts" / "test-ftps-container.sh"
DOWN = ROOT / "scripts" / "ftps-container-down.sh"


def test_ftps_container_run_id_rejects_embedded_invalid_character() -> None:
    result = subprocess.run(  # noqa: S603 -- fixed repository script, no shell.
        [str(TEST)],
        check=False,
        env={**os.environ, "KLOVE_FTPS_RUN_ID": "valid-prefix!invalid-suffix"},
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 2
    assert result.stderr == "invalid FTPS container RUN_ID\n"


def test_ftps_recovery_refuses_blank_container_labels(
    tmp_path: Path,
) -> None:
    run_id = f"fake-{uuid.uuid4().hex[:12]}"
    project = f"klove-ftps-{run_id}"
    state = ROOT / ".klove-integration" / "ftps-container" / run_id
    origin = state / "origin"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "docker-calls"
    docker = fake_bin / "docker"
    docker.write_text(
        f"""#!/bin/sh
printf '%s\\n' "$*" >> {calls}
case "$*" in
  "context show") echo fixture-context ;;
  *" info --format "*) echo fixture-daemon ;;
  *" inspect --format "*"{{.Id}}"*"{project}-prepare-1") echo fixture-container-id ;;
  *" inspect --format "*"{{.Id}}"*) exit 1 ;;
  *" inspect fixture-container-id") echo '{{}}' ;;
  *" inspect --format "*"com.docker.compose.project"*"fixture-container-id") echo ;;
  *" inspect --format "*"io.klove.ftps-container.role"*"fixture-container-id") echo prepare ;;
  *) exit 1 ;;
esac
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    state.mkdir(mode=0o700, parents=True)
    origin.write_text(
        "\n".join(
            (
                project,
                "fixture-context",
                "fixture-daemon",
                "rootless",
                f"klove-ftps-contract:{run_id}",
                "a" * 40,
                "b" * 64,
                "c" * 64,
                "0",
                f"{project}_private",
                f"{project}_runtime-secrets",
                f"{project}_klove-state",
                f"{project}_staging-state",
                f"{project}_contract-trust",
                f"{project}-prepare-1",
                f"{project}-klove-1",
                f"{project}-contract-1",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    origin.chmod(0o600)
    try:
        result = subprocess.run(  # noqa: S603 -- fixed repository script, no shell.
            [str(DOWN), run_id],
            check=False,
            env={**os.environ, "PATH": f"{fake_bin}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=5,
        )
        recorded = calls.read_text(encoding="utf-8")
    finally:
        shutil.rmtree(state)

    assert result.returncode == 1
    assert result.stderr == "FTPS container recovery refused a changed container\n"
    assert "container rm" not in recorded
