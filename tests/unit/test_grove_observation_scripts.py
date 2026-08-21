from __future__ import annotations

import os
import shutil
import stat
import subprocess
from hashlib import sha256
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _mock_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, str], Path]:
    tools = tmp_path / "tools"
    tools.mkdir()
    log = tmp_path / "docker.log"
    _write_executable(
        tools / "timeout",
        "#!/usr/bin/env sh\nshift\nexec \"$@\"\n",
    )
    _write_executable(tools / "sleep", "#!/usr/bin/env sh\nexit 0\n")
    _write_executable(tools / "dd", "#!/usr/bin/env sh\nprintf x\n")
    _write_executable(
        tools / "sha256sum",
        "#!/usr/bin/env sh\n"
        "printf '%s  -\\n' abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789\n",
    )
    _write_executable(
        tools / "curl",
        """#!/usr/bin/env sh
case " $* " in
  *"/api/v1/virtual-printers"*) printf '%s\\n' '{"name":"Klove Observation","status":"running"}' ;;
esac
exit 0
""",
    )
    _write_executable(
        tools / "docker",
        """#!/usr/bin/env sh
printf '%s\\n' "$*" >> "$MOCK_DOCKER_LOG"
for argument; do resource_name=$argument; done
run_id=${resource_name#klove-grove-observation-}
if [ "$1" = context ]; then printf '%s\\n' mock-context; exit 0; fi
if [ "$1" = info ]; then
  case "$*" in
    *'{{.ID}}'*) printf '%s\\n' mock-daemon ;;
    *) printf '%s\\n' '["name=rootless"]' ;;
  esac
  exit 0
fi
if [ "$1" = network ] && [ "$2" = create ]; then printf '%s\\n' mock-network; exit 0; fi
if [ "$1" = network ] && [ "$2" = inspect ]; then
  case "$*" in *'{{.Id}}'*) printf '%s\\n' mock-network ;; *) printf '%s\\n' "$run_id" ;; esac
  exit 0
fi
if [ "$1" = run ]; then
  if [ "${MOCK_DOCKER_FAIL_RUN:-}" = 1 ]; then exit 1; fi
  printf '%s\\n' mock-container
  exit 0
fi
if [ "$1" = exec ]; then
  case "$*" in
    *'/api/v1/virtual-printers'*)
      printf '%s\n' '{"name":"Klove Observation","status":{"running":true}}'
      ;;
  esac
  exit 0
fi
if [ "$1" = inspect ]; then
  case "$*" in
    *'.Config.Labels'*) printf '%s\\n' "$run_id" ;;
    *'.Config.Image'*) printf '%s\\n' grove-observer:cdf6b829 ;;
    *'.NetworkSettings.Networks'*) printf '%s\\n' 172.30.0.2 ;;
    *) printf '%s\\n' mock-container ;;
  esac
  exit 0
fi
exit 0
""",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{tools}:{environment['PATH']}",
            "MOCK_DOCKER_LOG": str(log),
            "PYTHONPATH": "",
        }
    )
    monkeypatch.setenv("PATH", environment["PATH"])
    return environment, log


def test_lifecycle_uses_the_public_run_sequence_and_exact_bound_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, log = _mock_environment(tmp_path, monkeypatch)
    run_id = f"mock-{sha256(str(tmp_path).encode()).hexdigest()[:12]}"
    state_dir = ROOT / ".klove-integration" / "grove-observation" / run_id
    try:
        up = subprocess.run(  # noqa: S603 -- fixed repository script and generated safe run ID.
            [str(SCRIPTS / "grove-observation-up.sh"), run_id],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        assert up.returncode == 0, up.stderr
        assert (state_dir / "origin").is_file()
        assert "abcdefgh" not in (state_dir / "origin").read_text(encoding="utf-8")

        down = subprocess.run(  # noqa: S603 -- fixed repository script and generated safe run ID.
            [str(SCRIPTS / "grove-observation-down.sh"), run_id],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        assert down.returncode == 0, down.stderr
        assert not state_dir.exists()
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)

    commands = log.read_text(encoding="utf-8")
    assert "network create --internal" in commands
    assert f"run --detach --name klove-grove-observation-{run_id}" in commands
    assert "--user 0:0 --cap-drop ALL --cap-add NET_BIND_SERVICE" in commands
    assert "--security-opt no-new-privileges" in commands
    assert "--memory 512m --cpus 1 --pids-limit 256" in commands
    assert "--entrypoint sh grove-observer:cdf6b829 -c python -m uvicorn" in commands
    assert f"exec klove-grove-observation-{run_id} curl" in commands
    assert f"rm --force klove-grove-observation-{run_id}" in commands
    assert f"network rm klove-grove-observation-{run_id}" in commands


def test_lifecycle_rejects_an_unsafe_run_id_before_docker(tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603 -- fixed repository script and literal hostile test input.
        [str(SCRIPTS / "grove-observation-up.sh"), "unsafe_id"],
        cwd=ROOT,
        env={**os.environ, "PATH": "/usr/bin:/bin"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "RUN_ID must contain" in result.stderr


def test_failed_start_removes_only_its_pending_labelled_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, log = _mock_environment(tmp_path, monkeypatch)
    environment["MOCK_DOCKER_FAIL_RUN"] = "1"
    run_id = f"fail-{sha256(str(tmp_path).encode()).hexdigest()[:12]}"
    state_dir = ROOT / ".klove-integration" / "grove-observation" / run_id
    try:
        result = subprocess.run(  # noqa: S603 -- fixed repository script and generated safe run ID.
            [str(SCRIPTS / "grove-observation-up.sh"), run_id],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert not state_dir.exists()
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)

    assert f"network rm klove-grove-observation-{run_id}" in log.read_text(encoding="utf-8")
