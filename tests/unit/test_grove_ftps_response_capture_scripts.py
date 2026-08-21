# ruff: noqa: E501 -- the embedded mock shell records exact command lines.

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import zipfile
from hashlib import sha256
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
PROFILE = ROOT / "tests" / "fixtures" / "grove-observations" / "ftps-server-response-profile"


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _copy_capture_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    scripts = root / "scripts"
    fixtures = root / "tests" / "fixtures" / "grove-observations"
    scripts.mkdir(parents=True)
    fixtures.mkdir(parents=True)
    for name in (
        "capture-grove-ftps-responses.sh",
        "grove-ftps-response-drive.py",
        "grove-ftps-response-recorder.py",
        "grove-observation-down.sh",
        "grove-observation-lib.sh",
        "grove-observation-up.sh",
    ):
        shutil.copy2(SCRIPTS / name, scripts / name)
    return root


def _mock_environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    tools = tmp_path / "tools"
    tools.mkdir()
    log = tmp_path / "docker.log"
    _write_executable(tools / "timeout", '#!/usr/bin/env sh\nshift\nexec "$@"\n')
    _write_executable(tools / "sleep", "#!/usr/bin/env sh\nexit 0\n")
    _write_executable(
        tools / "docker",
        """#!/usr/bin/env sh
printf '%s\n' "$*" >> "$MOCK_DOCKER_LOG"
if [ "$1" = context ]; then printf '%s\n' mock-context; exit 0; fi
if [ "$1" = info ]; then
  case "$*" in *'{{.ID}}'*) printf '%s\n' mock-daemon ;; *) printf '%s\n' '["name=rootless"]' ;; esac  # noqa: E501
  exit 0
fi
if [ "$1" = network ] && [ "$2" = create ]; then printf '%s\n' mock-network; exit 0; fi
if [ "$1" = network ] && [ "$2" = inspect ]; then
  case "$*" in *'.Labels'*) printf '%s\n' mock-run ;; *) printf '%s\n' mock-network ;; esac
  exit 0
fi
if [ "$1" = network ] && [ "$2" = rm ]; then exit 0; fi
if [ "$1" = run ]; then
  case "$*" in
    *'ftps-response-recorder'*)
      for argument; do
        case "$argument" in *':/evidence:rw') evidence=${argument%:/evidence:rw} ;; esac
      done
      printf '%s\n' '{"observed":{"successful_client_flow":true}}' > "$evidence/ftps-server-response-profile"  # noqa: E501
      printf '%s\n' mock-recorder
      ;;
    *) printf '%s\n' mock-grove ;;
  esac
  exit 0
fi
if [ "$1" = wait ]; then printf '%s\n' 0; exit 0; fi
if [ "$1" = logs ] || [ "$1" = rm ]; then exit 0; fi
if [ "$1" = exec ]; then
  case "$*" in
    *'python - 172.30.0.3'*) printf '%s\n' 'public API accepted generated printer, archive, and queue item' ;;  # noqa: E501
    *'/api/v1/virtual-printers'*) printf '%s\n' '{"name":"Klove Observation","status":{"running":true}}' ;;  # noqa: E501
  esac
  exit 0
fi
if [ "$1" = inspect ]; then
  resource=""
  for argument; do resource=$argument; done
  case "$*" in
    *'grove-observation.role'*) printf '%s\n' ftps-response-recorder ;;
    *'grove-observation.run-id'*) printf '%s\n' mock-run ;;
    *'.Config.Image'*) printf '%s\n' grove-observer:cdf6b829 ;;
    *'.NetworkSettings.Networks'*)
      case "$resource" in *ftps-response*) printf '%s\n' 172.30.0.3 ;; *) printf '%s\n' 172.30.0.2 ;; esac  # noqa: E501
      ;;
    *'.Id'*)
      case "$resource" in *ftps-response*) printf '%s\n' mock-recorder ;; *) printf '%s\n' mock-grove ;; esac  # noqa: E501
      ;;
    *) printf '%s\n' mock-grove ;;
  esac
  exit 0
fi
exit 0
""",
    )
    environment = os.environ.copy()
    environment.update({"PATH": f"{tools}:{environment['PATH']}", "MOCK_DOCKER_LOG": str(log)})
    return environment, log


def test_capture_replays_public_path_and_removes_exact_resources(tmp_path: Path) -> None:
    root = _copy_capture_tree(tmp_path)
    environment, log = _mock_environment(tmp_path)
    result = subprocess.run(  # noqa: S603 -- copied repository script and generated safe run ID.
        [str(root / "scripts" / "capture-grove-ftps-responses.sh"), "mock-run"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Sanitized FTPS response observation captured" in result.stdout
    assert json.loads(
        (root / "tests/fixtures/grove-observations/ftps-server-response-profile").read_text()
    ) == {"observed": {"successful_client_flow": True}}
    assert not (root / ".klove-integration/grove-observation/mock-run").exists()

    commands = log.read_text(encoding="utf-8")
    assert "network create --internal" in commands
    assert "--label io.klove.grove-observation.role=ftps-response-recorder" in commands
    assert "--read-only --tmpfs /tmp:rw,noexec,nosuid,size=4m" in commands
    assert "--memory 128m --cpus 0.5 --pids-limit 64" in commands
    assert "exec --interactive klove-grove-observation-mock-run python - 172.30.0.3" in commands
    assert "wait klove-ftps-response-mock-run" in commands
    assert "rm --force klove-ftps-response-mock-run" in commands
    assert "rm --force klove-grove-observation-mock-run" in commands
    assert "network rm klove-grove-observation-mock-run" in commands


def test_generated_archive_is_deterministic_and_harmless() -> None:
    spec = importlib.util.spec_from_file_location(
        "grove_ftps_response_drive", SCRIPTS / "grove-ftps-response-drive.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    first = module._generated_archive()
    second = module._generated_archive()
    assert first == second
    assert sha256(first).digest() == sha256(second).digest()
    with zipfile.ZipFile(BytesIO(first)) as archive:
        assert {member.date_time for member in archive.infolist()} == {(2020, 1, 1, 0, 0, 0)}
        assert set(archive.namelist()) == {
            "[Content_Types].xml",
            "_rels/.rels",
            "3D/3dmodel.model",
            "Metadata/plate_1.gcode",
        }
        assert archive.read("Metadata/plate_1.gcode") == (
            b"; generated observation archive\nG4 P1\n"
        )


def test_sanitized_response_profile_retains_no_endpoint_or_secret_values() -> None:
    document = json.loads(PROFILE.read_text(encoding="ascii"))
    serialized = json.dumps(document, sort_keys=True)
    assert document["observed"]["successful_client_flow"] is True
    assert "TEST0000" not in serialized
    assert "BEGIN CERTIFICATE" not in serialized
    assert "PRIVATE KEY" not in serialized
    assert "172." not in serialized
    assert "227" in serialized
    assert "data_peer_matches_control_peer" in serialized
