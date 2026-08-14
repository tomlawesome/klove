from __future__ import annotations

import os
from pathlib import Path

import pytest

from klove.persistence.private_files import (
    PrivateFileError,
    fsync_directory,
    prepare_private_file,
    require_private_directory,
    require_private_file,
)


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner-only storage contract")
def test_private_file_lifecycle_is_owner_only_and_idempotent(tmp_path: Path) -> None:
    require_private_directory(tmp_path)
    path = tmp_path / "state.sqlite3"

    prepare_private_file(path)
    prepare_private_file(path)
    require_private_file(path)
    fsync_directory(tmp_path)

    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner-only storage contract")
def test_private_directory_rejects_missing_wrong_linked_public_and_foreign_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(PrivateFileError):
        require_private_directory(tmp_path / "missing")

    regular = tmp_path / "regular"
    regular.touch(mode=0o600)
    with pytest.raises(PrivateFileError):
        require_private_directory(regular)

    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(PrivateFileError):
        require_private_directory(linked)

    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    public.chmod(0o755)
    with pytest.raises(PrivateFileError):
        require_private_directory(public)

    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(PrivateFileError):
        require_private_directory(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner-only storage contract")
def test_private_file_rejects_missing_wrong_linked_public_and_foreign_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(PrivateFileError):
        require_private_file(tmp_path / "missing")

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(PrivateFileError):
        require_private_file(directory)

    target = tmp_path / "target"
    target.touch(mode=0o600)
    linked = tmp_path / "linked"
    linked.symlink_to(target)
    with pytest.raises(PrivateFileError):
        require_private_file(linked)

    public = tmp_path / "public"
    public.touch(mode=0o644)
    public.chmod(0o644)
    with pytest.raises(PrivateFileError):
        require_private_file(public)

    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(PrivateFileError):
        require_private_file(target)


def test_prepare_private_file_wraps_creation_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(PrivateFileError):
        prepare_private_file(tmp_path / "missing" / "state")

    def denied(*_args: object, **_kwargs: object) -> int:
        raise PermissionError

    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(PrivateFileError):
        prepare_private_file(tmp_path / "state")


def test_fsync_directory_wraps_open_and_sync_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open = os.open

    def denied_open(*_args: object, **_kwargs: object) -> int:
        raise PermissionError

    monkeypatch.setattr(os, "open", denied_open)
    with pytest.raises(PrivateFileError):
        fsync_directory(tmp_path)

    monkeypatch.setattr(os, "open", real_open)
    closed: list[int] = []
    monkeypatch.setattr(os, "fsync", lambda _descriptor: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(os, "close", closed.append)
    with pytest.raises(PrivateFileError):
        fsync_directory(tmp_path)
    assert len(closed) == 1
