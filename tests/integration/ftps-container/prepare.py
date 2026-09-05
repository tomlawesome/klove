"""Prepare owner-only runtime secrets and a public-certificate trust volume."""

from __future__ import annotations

import os
from pathlib import Path

SOURCE = Path("/input")
TARGET = Path("/run/klove-secrets")
TRUST = Path("/var/lib/klove/ftps-staging")
NAMES = (
    "api-token",
    "config.toml",
    "tls-certificate.pem",
    "tls-private-key.pem",
)

if os.getuid() != 0 or os.getgid() != 0:
    raise RuntimeError("secret preparation did not start as root")
inputs: dict[str, bytes] = {}
for name in NAMES:
    source = SOURCE / name
    if not source.is_file() or source.is_symlink():
        raise RuntimeError("runtime secret input was unavailable")
    inputs[name] = source.read_bytes()
os.setgroups([])
os.setgid(10001)
os.setuid(10001)
if os.getuid() != 10001 or os.getgid() != 10001:
    raise RuntimeError("secret preparation did not drop identity")
for directory in (TARGET, TRUST):
    metadata = directory.lstat()
    if (
        not directory.is_dir()
        or directory.is_symlink()
        or metadata.st_uid != 10001
        or metadata.st_gid != 10001
        or metadata.st_mode & 0o777 != 0o700
    ):
        raise RuntimeError("prepared volume directory confinement mismatch")
if any(TARGET.iterdir()) or any(TRUST.iterdir()):
    raise RuntimeError("prepared volumes were not empty")


def write_private(target: Path, content: bytes) -> None:
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if target.stat().st_mode & 0o777 != 0o600:
        raise RuntimeError("prepared file mode mismatch")
    if target.stat().st_uid != 10001 or target.stat().st_gid != 10001:
        raise RuntimeError("prepared file identity mismatch")


for name, content in inputs.items():
    write_private(TARGET / name, content)
write_private(TRUST / "tls-certificate.pem", inputs["tls-certificate.pem"])
