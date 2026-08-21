"""Private, bounded byte staging isolated from every FTPS wire decision."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import UUID

from klove.domain.artifacts import ArtifactLimits
from klove.errors import KloveError
from klove.persistence.private_files import (
    PrivateFileError,
    fsync_directory,
    require_private_directory,
    require_private_file,
)

_CHUNK_BYTES: Final = 1024 * 1024
_RECEIPT_VERSION: Final = "1"


class FtpsStagingError(KloveError):
    """An FTPS staging request is unsafe, incomplete, conflicting, or unavailable."""


@dataclass(frozen=True, slots=True)
class FtpsStageReservation:
    """A pre-authorized exact-printer staging identity with no client-controlled path."""

    staging_id: str
    printer_uuid: str

    def __post_init__(self) -> None:
        _require_canonical_uuid4(self.staging_id)
        _require_canonical_uuid4(self.printer_uuid)


@dataclass(frozen=True, slots=True)
class StagedArtifact:
    """Non-secret receipt for immutable staged bytes; it grants no dispatch authority."""

    reservation: FtpsStageReservation
    archive_size_bytes: int
    archive_sha256: str

    def __post_init__(self) -> None:
        if type(self.archive_size_bytes) is not int or self.archive_size_bytes < 0:
            raise FtpsStagingError
        expected_prefix = "sha256:"
        digest = self.archive_sha256
        if (
            type(digest) is not str
            or not digest.startswith(expected_prefix)
            or len(digest) != len(expected_prefix) + 64
            or any(
                character not in "0123456789abcdef" for character in digest[len(expected_prefix) :]
            )
        ):
            raise FtpsStagingError


class FtpsStagingStore:
    """Persist one bounded staged source under an opaque generated staging identity.

    This store deliberately has no FTP parser, credential lookup, client filename,
    data socket, dispatch adapter, or byte-reading API. Those surfaces remain gated
    on the missing observation profile and accepted ADR 0010 contract.
    """

    def __init__(self, directory: Path, *, limits: ArtifactLimits) -> None:
        self._directory = directory
        self._limits = limits

    def initialize(self) -> None:
        """Require the deployment-created owner-only staging directory."""
        self._require_directory()

    def stage(self, reservation: FtpsStageReservation, chunks: Iterable[bytes]) -> StagedArtifact:
        """Write one new bounded source exactly once and return its non-secret receipt."""
        if type(reservation) is not FtpsStageReservation:
            raise FtpsStagingError
        self._require_directory()
        receiving, source, receipt = self._paths(reservation)
        if any(os.path.lexists(path) for path in (receiving, source, receipt)):
            raise FtpsStagingError

        descriptor = -1
        source_created = False
        try:
            descriptor = os.open(
                receiving,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            digest = hashlib.sha256()
            byte_count = 0
            for chunk in chunks:
                if type(chunk) is not bytes:
                    raise FtpsStagingError
                byte_count += len(chunk)
                if byte_count > self._limits.max_archive_compressed_bytes:
                    raise FtpsStagingError
                digest.update(chunk)
                _write_all(descriptor, chunk)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            require_private_file(receiving)
            os.link(receiving, source, follow_symlinks=False)
            source_created = True
            receiving.unlink()
            fsync_directory(self._directory)
            artifact = StagedArtifact(
                reservation=reservation,
                archive_size_bytes=byte_count,
                archive_sha256=f"sha256:{digest.hexdigest()}",
            )
            _write_receipt(receipt, artifact)
            fsync_directory(self._directory)
            return artifact
        except Exception as exc:
            if descriptor >= 0:
                os.close(descriptor)
            receipt.unlink(missing_ok=True)
            if source_created:
                source.unlink(missing_ok=True)
            receiving.unlink(missing_ok=True)
            fsync_directory(self._directory)
            if isinstance(exc, FtpsStagingError):
                raise
            raise FtpsStagingError from exc

    def inspect(self, reservation: FtpsStageReservation) -> StagedArtifact:
        """Return only an exact principal's receipt after re-verifying retained bytes."""
        if type(reservation) is not FtpsStageReservation:
            raise FtpsStagingError
        self._require_directory()
        _receiving, source, receipt = self._paths(reservation)
        try:
            require_private_file(source)
            require_private_file(receipt)
            artifact = _read_receipt(receipt)
            if artifact.reservation != reservation:
                raise FtpsStagingError
            if source.stat().st_size != artifact.archive_size_bytes:
                raise FtpsStagingError
            digest = hashlib.sha256()
            with source.open("rb", buffering=0) as staged:
                while chunk := staged.read(_CHUNK_BYTES):
                    digest.update(chunk)
            if f"sha256:{digest.hexdigest()}" != artifact.archive_sha256:
                raise FtpsStagingError
            return artifact
        except (OSError, PrivateFileError, ValueError, json.JSONDecodeError) as exc:
            raise FtpsStagingError from exc

    def _require_directory(self) -> None:
        try:
            require_private_directory(self._directory)
        except (OSError, PrivateFileError, ValueError) as exc:
            raise FtpsStagingError from exc

    def _paths(self, reservation: FtpsStageReservation) -> tuple[Path, Path, Path]:
        base = self._directory / reservation.staging_id
        return (
            base.with_suffix(".receiving"),
            base.with_suffix(".source"),
            base.with_suffix(".receipt"),
        )


def _require_canonical_uuid4(value: object) -> None:
    if type(value) is not str:
        raise FtpsStagingError
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise FtpsStagingError from exc
    if parsed.version != 4 or str(parsed) != value:
        raise FtpsStagingError


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    written = 0
    while written < len(view):
        count = os.write(descriptor, view[written:])
        if count <= 0:
            raise FtpsStagingError
        written += count


def _write_receipt(path: Path, artifact: StagedArtifact) -> None:
    payload = json.dumps(
        {
            "archive_sha256": artifact.archive_sha256,
            "archive_size_bytes": artifact.archive_size_bytes,
            "printer_uuid": artifact.reservation.printer_uuid,
            "staging_id": artifact.reservation.staging_id,
            "version": _RECEIPT_VERSION,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        require_private_file(path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_receipt(path: Path) -> StagedArtifact:
    payload = json.loads(path.read_bytes())
    if type(payload) is not dict or set(payload) != {
        "archive_sha256",
        "archive_size_bytes",
        "printer_uuid",
        "staging_id",
        "version",
    }:
        raise FtpsStagingError
    if payload["version"] != _RECEIPT_VERSION:
        raise FtpsStagingError
    return StagedArtifact(
        reservation=FtpsStageReservation(
            staging_id=payload["staging_id"], printer_uuid=payload["printer_uuid"]
        ),
        archive_size_bytes=payload["archive_size_bytes"],
        archive_sha256=payload["archive_sha256"],
    )
