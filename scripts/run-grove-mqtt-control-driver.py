#!/usr/bin/env python3
"""Run the disposable control driver with bounded host-only stdout capture."""

from __future__ import annotations

import contextlib
import os
import select
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

MAX_CAPTURE_BYTES = 256
CONNECTION_BARRIER_SECONDS = 20.0
DRIVER_HEADROOM_SECONDS = 25.0
DRIVER_TIMEOUT_SECONDS = CONNECTION_BARRIER_SECONDS + DRIVER_HEADROOM_SECONDS
OUTCOME_TIMEOUT = 124
OUTCOME_OVERSIZE = 125
OUTCOME_PRIVATE_PATH_INVALID = 126


def _owner_private_parent(path: Path) -> bool:
    try:
        metadata = path.parent.stat()
        return (
            path.parent.is_dir()
            and not path.parent.is_symlink()
            and stat.S_IMODE(metadata.st_mode) == 0o700
        )
    except OSError:
        return False


def _unused_path(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _stop(process: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)


def _exit_status(process: subprocess.Popen[bytes]) -> int:
    status = process.wait()
    return status if status >= 0 else 128 + -status


def _publish_exclusively(temporary: Path, output: Path, captured: bytes) -> bool:
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(captured)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, output, follow_symlinks=False)
        temporary.unlink()
        return True
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()
        return False


def capture_driver(  # noqa: PLR0911, PLR0912 -- bounded capture paths have distinct fixed outcomes.
    command: list[str],
    source: BinaryIO,
    output: Path,
    *,
    timeout_seconds: float = DRIVER_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Bound stdout in memory and atomically write it only after a complete run."""
    if (
        not _owner_private_parent(output)
        or not _unused_path(output)
        or not _unused_path(output.with_suffix(output.suffix + ".tmp"))
        or timeout_seconds <= 0
    ):
        return OUTCOME_PRIVATE_PATH_INVALID
    process = subprocess.Popen(  # noqa: S603 -- main supplies one fixed Docker exec argv.
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    if process.stdout is None:
        _stop(process)
        return OUTCOME_PRIVATE_PATH_INVALID
    if process.stdin is None:
        _stop(process)
        return OUTCOME_PRIVATE_PATH_INVALID
    try:
        process.stdin.write(source.read())
        process.stdin.close()
    except OSError:
        _stop(process)
        return OUTCOME_PRIVATE_PATH_INVALID
    captured = bytearray()
    deadline = clock() + timeout_seconds
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            _stop(process)
            return OUTCOME_TIMEOUT
        readable, _, _ = select.select([process.stdout], [], [], min(remaining, 0.1))
        if readable:
            chunk = os.read(process.stdout.fileno(), MAX_CAPTURE_BYTES + 1 - len(captured))
            if chunk:
                captured.extend(chunk)
                if len(captured) > MAX_CAPTURE_BYTES:
                    _stop(process)
                    return OUTCOME_OVERSIZE
                continue
        if process.poll() is not None:
            break
    while True:
        chunk = os.read(process.stdout.fileno(), MAX_CAPTURE_BYTES + 1 - len(captured))
        if not chunk:
            break
        captured.extend(chunk)
        if len(captured) > MAX_CAPTURE_BYTES:
            return OUTCOME_OVERSIZE
    temporary = output.with_suffix(output.suffix + ".tmp")
    if not _publish_exclusively(temporary, output, bytes(captured)):
        return OUTCOME_PRIVATE_PATH_INVALID
    return _exit_status(process)


def main(arguments: list[str]) -> int:
    if len(arguments) != 5:
        return 2
    output = Path(arguments[0])
    container, recorder_ip, drive, timeout = arguments[1:]
    try:
        timeout_seconds = float(timeout)
    except ValueError:
        return 2
    if timeout_seconds != DRIVER_TIMEOUT_SECONDS:
        return 2
    try:
        with Path(drive).open("rb") as source:
            return capture_driver(
                ["docker", "exec", "--interactive", container, "python", "-", recorder_ip],
                source,
                output,
                timeout_seconds=timeout_seconds,
            )
    except (OSError, ValueError):
        return OUTCOME_PRIVATE_PATH_INVALID


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
