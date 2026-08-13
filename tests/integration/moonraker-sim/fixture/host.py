from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import FrameType
from typing import Any

RUN_DIR = Path("/run/klipper")
DATA_DIR = Path("/data/printer_data")
SECRETS_DIR = Path("/run/klove-secrets")
STATE_DIR = Path("/run/printer-state")
DATABASE_DIR = STATE_DIR / "moonraker-database"
BOOTSTRAP_READY = STATE_DIR / "bootstrap-ready"
MCU_PATH = RUN_DIR / "host_mcu"
KLIPPY_SOCKET = RUN_DIR / "klippy.sock"
MOONRAKER = "http://127.0.0.1:7125"


def _wait_for(path: Path, processes: list[subprocess.Popen[bytes]], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        for process in processes:
            if process.poll() is not None:
                raise RuntimeError("printer-host child exited during startup")
        time.sleep(0.05)
    raise RuntimeError("printer-host startup timed out")


def _terminate(processes: list[subprocess.Popen[bytes]]) -> None:
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 5
    for process in reversed(processes):
        remaining = deadline - time.monotonic()
        if remaining > 0:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=remaining)
        if process.poll() is None:
            process.kill()
            process.wait()


def _request(path: str, *, method: str = "GET") -> Any:
    request = urllib.request.Request(  # noqa: S310 -- fixed loopback URL.
        f"{MOONRAKER}{path}", method=method
    )
    with urllib.request.urlopen(request, timeout=3) as response:  # noqa: S310
        if response.status != 200:
            raise RuntimeError("Moonraker bootstrap request failed")
        return json.load(response)


def _wait_ready(processes: list[subprocess.Popen[bytes]], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(process.poll() is not None for process in processes):
            raise RuntimeError("printer-host child exited during bootstrap")
        try:
            server = _request("/server/info")["result"]
            printer = _request("/printer/info")["result"]
            if (
                server.get("klippy_connected") is True
                and server.get("klippy_state") == "ready"
                and printer.get("state") == "ready"
            ):
                return
        except (KeyError, TypeError, urllib.error.URLError):
            pass
        time.sleep(0.1)
    raise RuntimeError("Moonraker did not become ready")


def _write_private(destination: Path, value: str) -> None:
    temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(8)}.new")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        stream = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor = -1
        with stream:
            stream.write(value)
        os.replace(temporary, destination)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def _write_secret(name: str, value: str) -> None:
    _write_private(SECRETS_DIR / name, value)


def _ensure_klove_token() -> None:
    token_path = SECRETS_DIR / "klove-token"
    if not token_path.exists():
        _write_secret("klove-token", secrets.token_hex(32))
        return
    token = token_path.read_text(encoding="utf-8").strip()
    if len(token) != 64:
        raise RuntimeError("persisted Klove token is invalid")
    try:
        bytes.fromhex(token)
    except ValueError as error:
        raise RuntimeError("persisted Klove token is invalid") from error


def _bootstrap(processes: list[subprocess.Popen[bytes]]) -> None:
    _wait_ready(processes, 30)
    api_key = _request("/access/api_key")["result"]
    if not isinstance(api_key, str) or len(api_key) < 32:
        raise RuntimeError("Moonraker returned an invalid API key")
    _write_secret("moonraker-api-key", api_key)
    _ensure_klove_token()
    _write_secret(
        "config.toml",
        Path("/fixture/klove-config.toml").read_text(encoding="utf-8"),
    )

    _write_private(BOOTSTRAP_READY, "ready\n")
    print("moonraker-sim printer host ready", flush=True)


def main() -> int:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    BOOTSTRAP_READY.unlink(missing_ok=True)
    DATABASE_DIR.mkdir(parents=True, exist_ok=True)
    for child in ("config", "comms", "gcodes", "logs", "misc"):
        (DATA_DIR / child).mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "database").symlink_to(DATABASE_DIR, target_is_directory=True)
    shutil.copyfile("/fixture/contract.gcode", DATA_DIR / "gcodes/contract.gcode")

    processes: list[subprocess.Popen[bytes]] = []
    stopping = False

    def stop(_signum: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        processes.append(
            subprocess.Popen(  # noqa: S603 -- fixed image-local executable and arguments.
                ["/opt/klipper/out/klipper.elf", "-I", str(MCU_PATH)],
                stdin=subprocess.DEVNULL,
            )
        )
        _wait_for(MCU_PATH, processes, 5)

        klippy_env = os.environ.copy()
        klippy_env["PYTHONPATH"] = "/opt/klipper/klippy"
        processes.append(
            subprocess.Popen(  # noqa: S603 -- fixed image-local executable and arguments.
                [
                    "/opt/klipper-env/bin/python",
                    "/opt/klipper/klippy/klippy.py",
                    "/fixture/printer.cfg",
                    "-a",
                    str(KLIPPY_SOCKET),
                    "-I",
                    str(RUN_DIR / "printer-pty"),
                ],
                env=klippy_env,
                stdin=subprocess.DEVNULL,
            )
        )
        _wait_for(KLIPPY_SOCKET, processes, 10)

        moonraker_env = os.environ.copy()
        moonraker_env["PYTHONPATH"] = "/opt/moonraker"
        processes.append(
            subprocess.Popen(  # noqa: S603 -- fixed image-local executable and arguments.
                [
                    "/opt/moonraker-env/bin/python",
                    "-m",
                    "moonraker",
                    "-d",
                    str(DATA_DIR),
                    "-c",
                    "/fixture/moonraker.conf",
                    "-n",
                ],
                env=moonraker_env,
                stdin=subprocess.DEVNULL,
            )
        )

        _bootstrap(processes)

        while not stopping:
            if any(process.poll() is not None for process in processes):
                return 1
            time.sleep(0.1)
        return 0
    finally:
        _terminate(processes)


if __name__ == "__main__":
    sys.exit(main())
