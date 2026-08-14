"""Owner-only filesystem checks shared by durable private stores."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from klove.errors import KloveError


class PrivateFileError(KloveError):
    """A durable path has unsafe identity, ownership, type, or permissions."""


def require_private_directory(path: Path) -> None:
    """Require one existing, real, owner-only directory."""
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PrivateFileError from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise PrivateFileError
    if os.name != "nt" and (
        metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise PrivateFileError


def prepare_private_file(path: Path) -> None:
    """Create one owner-only regular file without following an existing link."""
    require_private_directory(path.parent)
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        pass
    except OSError as exc:
        raise PrivateFileError from exc
    else:
        os.close(descriptor)
        fsync_directory(path.parent)
    require_private_file(path)


def require_private_file(path: Path) -> None:
    """Require one owner-only regular file that is not a symbolic link."""
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PrivateFileError from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise PrivateFileError
    if os.name != "nt" and (
        metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise PrivateFileError


def fsync_directory(path: Path) -> None:
    """Persist one directory entry update on platforms that support it."""
    if os.name == "nt":  # pragma: no cover - Windows-only filesystem contract
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise PrivateFileError from exc
