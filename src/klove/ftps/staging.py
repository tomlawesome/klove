"""Private, bounded FTPS byte staging with no dispatch authority."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Lock
from types import TracebackType
from typing import Final, Self, cast
from uuid import UUID, uuid4

from klove.domain.artifacts import ArtifactLimits
from klove.errors import KloveError
from klove.persistence.private_files import (
    PrivateFileError,
    fsync_directory,
    require_private_directory,
    require_private_file,
)

_CHUNK_BYTES: Final = 1024 * 1024
_MAX_UNIX_MS: Final = 9_223_372_036_854_775_807
_CLIENT_PATH: Final = re.compile(r"/[A-Za-z0-9][A-Za-z0-9._-]{0,246}\.3mf", re.ASCII)
_RECEIPT_VERSION: Final = "2"
_STAGE_ENTRY = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"
    r"\.(reservation|receiving|source|receipt|updating|removing)",
    re.ASCII,
)


class FtpsStagingError(KloveError):
    """An FTPS staging request is unsafe, incomplete, conflicting, or unavailable."""


@dataclass(frozen=True, slots=True)
class FtpsStageReservation:
    """One generated stage bound to an exact printer, client path, and lifetime."""

    staging_id: str
    printer_uuid: str
    client_path: str
    created_at_unix_ms: int
    expires_at_unix_ms: int

    def __post_init__(self) -> None:
        _require_canonical_uuid4(self.staging_id)
        _require_canonical_uuid4(self.printer_uuid)
        if type(self.client_path) is not str or _CLIENT_PATH.fullmatch(self.client_path) is None:
            raise FtpsStagingError
        if (
            type(self.created_at_unix_ms) is not int
            or type(self.expires_at_unix_ms) is not int
            or not 0 <= self.created_at_unix_ms <= _MAX_UNIX_MS
            or not self.created_at_unix_ms < self.expires_at_unix_ms <= _MAX_UNIX_MS
        ):
            raise FtpsStagingError


@dataclass(frozen=True, slots=True)
class StagedArtifact:
    """Non-secret receipt for immutable staged bytes; it grants no dispatch authority."""

    reservation: FtpsStageReservation
    archive_size_bytes: int
    archive_sha256: str
    consumed: bool = False

    def __post_init__(self) -> None:
        if type(self.reservation) is not FtpsStageReservation:
            raise FtpsStagingError
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
            or type(self.consumed) is not bool
        ):
            raise FtpsStagingError


class FtpsStagingWriter:
    """Single-use owner of one receiving file."""

    def __init__(
        self,
        store: FtpsStagingStore,
        reservation: FtpsStageReservation,
        descriptor: int,
    ) -> None:
        self._store = store
        self._reservation = reservation
        self._descriptor = descriptor
        self._digest = hashlib.sha256()
        self._byte_count = 0
        self._active = True

    def __enter__(self) -> Self:
        if not self._active:
            raise FtpsStagingError
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if self._active:
            self.abort()

    def write(self, chunk: bytes) -> None:
        """Append one bounded bytes chunk; any failure aborts this receiving stage."""
        if not self._active or type(chunk) is not bytes:
            if self._active:
                self.abort()
            raise FtpsStagingError
        new_count = self._byte_count + len(chunk)
        if new_count > self._store._limits.max_archive_compressed_bytes:
            self.abort()
            raise FtpsStagingError
        try:
            _write_all(self._descriptor, chunk)
        except BaseException as exc:
            self.abort()
            if isinstance(exc, FtpsStagingError):
                raise
            if isinstance(exc, Exception):
                raise FtpsStagingError from exc
            raise
        self._digest.update(chunk)
        self._byte_count = new_count

    def commit(self) -> StagedArtifact:
        """Flush and atomically publish one immutable complete stage."""
        if not self._active:
            raise FtpsStagingError
        try:
            os.fsync(self._descriptor)
            os.close(self._descriptor)
            self._descriptor = -1
            artifact = StagedArtifact(
                reservation=self._reservation,
                archive_size_bytes=self._byte_count,
                archive_sha256=f"sha256:{self._digest.hexdigest()}",
            )
            self._store._commit(self._reservation, artifact)
            self._active = False
            return artifact
        except BaseException as exc:
            self.abort()
            if isinstance(exc, FtpsStagingError):
                raise
            if isinstance(exc, Exception):
                raise FtpsStagingError from exc
            raise

    def abort(self) -> None:
        """Close and remove only this exact incomplete stage."""
        if not self._active:
            return
        self._active = False
        if self._descriptor >= 0:
            with suppress(OSError):
                os.close(self._descriptor)
            self._descriptor = -1
        self._store._abort(self._reservation)


class FtpsStagingStore:
    """Persist bounded stages under opaque generated identities."""

    def __init__(self, directory: Path, *, limits: ArtifactLimits, capacity: int) -> None:
        if type(capacity) is not int or not 1 <= capacity <= 100_000:
            raise FtpsStagingError
        self._directory = directory
        self._limits = limits
        self._capacity = capacity
        self._capacity_lock = Lock()

    def initialize(self) -> None:
        """Require the deployment-created owner-only staging directory."""
        self._require_directory()

    def reconcile(self) -> None:
        """Validate and recover only exact durable stages left by an interrupted process."""
        self._require_directory()
        with self._capacity_lock:
            actions = self._reconciliation_actions()
            try:
                for action, paths, artifact in actions:
                    if action == "stable":
                        continue
                    if action == "discard_reservation":
                        paths.reservation.unlink()
                    elif action == "discard_partial_receipt":
                        paths.receipt.unlink()
                        fsync_directory(self._directory)
                        raise FtpsStagingError
                    elif action == "discard_incomplete":
                        paths.receiving.unlink()
                        paths.reservation.unlink()
                    elif action == "write_receipt":
                        _write_receipt(paths.receipt, cast(StagedArtifact, artifact))
                        paths.reservation.unlink()
                    elif action == "remove_reservation":
                        paths.reservation.unlink()
                    elif action == "finish_consuming":
                        os.replace(paths.updating, paths.receipt)
                    elif action == "finish_removing_source":
                        paths.source.unlink()
                        paths.removing.unlink()
                    elif action == "finish_removing_marker":
                        paths.removing.unlink()
                    else:  # pragma: no cover - actions are closed by _reconciliation_actions.
                        raise FtpsStagingError
                    fsync_directory(self._directory)
            except (OSError, PrivateFileError, ValueError) as exc:
                raise FtpsStagingError from exc

    def _reconciliation_actions(self) -> list[tuple[str, _StagePaths, StagedArtifact | None]]:
        stages: dict[str, set[str]] = defaultdict(set)
        try:
            for path in self._directory.iterdir():
                match = _STAGE_ENTRY.fullmatch(path.name)
                if match is None:
                    raise FtpsStagingError
                _require_unlinked_private_file(path)
                stages[match.group(1)].add(match.group(2))
            return [
                self._reconciliation_action(stage_id, entries)
                for stage_id, entries in stages.items()
            ]
        except (OSError, PrivateFileError, ValueError, json.JSONDecodeError) as exc:
            raise FtpsStagingError from exc

    def _reconciliation_action(
        self, stage_id: str, entries: set[str]
    ) -> tuple[str, _StagePaths, StagedArtifact | None]:
        paths = self._paths_for_id(stage_id)
        state = frozenset(entries)
        artifact: StagedArtifact | None = None
        if state == frozenset({"reservation"}):
            _require_stage_reservation(paths.reservation, stage_id)
            action = "discard_reservation"
        elif state == frozenset({"reservation", "receiving"}):
            _require_stage_reservation(paths.reservation, stage_id)
            action = "discard_incomplete"
        elif state == frozenset({"reservation", "source"}):
            reservation = _require_stage_reservation(paths.reservation, stage_id)
            artifact = _artifact_from_source(paths.source, reservation, self._limits)
            action = "write_receipt"
        elif state == frozenset({"reservation", "source", "receipt"}):
            action = self._reservation_source_receipt_action(paths, stage_id)
        elif state == frozenset({"source", "receipt"}):
            artifact = _require_stage_receipt(paths.receipt, stage_id)
            _open_and_close_verified_source(paths.source, artifact)
            action = "stable"
        elif state == frozenset({"source", "receipt", "updating"}):
            artifact = _require_stage_receipt(paths.receipt, stage_id)
            updating = _require_stage_receipt(paths.updating, stage_id)
            if artifact.consumed or updating != replace(artifact, consumed=True):
                raise FtpsStagingError
            _open_and_close_verified_source(paths.source, artifact)
            action = "finish_consuming"
        elif state == frozenset({"source", "removing"}):
            artifact = _require_stage_receipt(paths.removing, stage_id)
            if not artifact.consumed:
                raise FtpsStagingError
            _open_and_close_verified_source(paths.source, artifact)
            action = "finish_removing_source"
        elif state == frozenset({"removing"}):
            artifact = _require_stage_receipt(paths.removing, stage_id)
            if not artifact.consumed:
                raise FtpsStagingError
            action = "finish_removing_marker"
        else:
            raise FtpsStagingError
        return action, paths, artifact

    def _reservation_source_receipt_action(self, paths: _StagePaths, stage_id: str) -> str:
        reservation = _require_stage_reservation(paths.reservation, stage_id)
        recovered = _artifact_from_source(paths.source, reservation, self._limits)
        try:
            artifact = _require_stage_receipt(paths.receipt, stage_id)
        except (FtpsStagingError, ValueError, json.JSONDecodeError):
            if not _is_exact_partial_receipt(paths.receipt, recovered):
                raise
            return "discard_partial_receipt"
        if artifact != recovered:
            raise FtpsStagingError
        return "remove_reservation"

    def create_reservation(
        self,
        *,
        printer_uuid: str,
        client_path: str,
        created_at_unix_ms: int,
        expires_at_unix_ms: int,
    ) -> FtpsStageReservation:
        """Generate an opaque canonical stage identity for an authenticated principal."""
        return FtpsStageReservation(
            staging_id=str(uuid4()),
            printer_uuid=printer_uuid,
            client_path=client_path,
            created_at_unix_ms=created_at_unix_ms,
            expires_at_unix_ms=expires_at_unix_ms,
        )

    def begin(self, reservation: FtpsStageReservation) -> FtpsStagingWriter:
        """Durably reserve one receiving identity before accepting bytes."""
        _require_reservation(reservation)
        self._require_directory()
        paths = self._paths(reservation)
        with self._capacity_lock:
            self._require_available_capacity()
            if any(os.path.lexists(path) for path in paths):
                raise FtpsStagingError
            try:
                _write_document(paths.reservation, _reservation_document(reservation))
                descriptor = os.open(
                    paths.receiving,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                fsync_directory(self._directory)
                return FtpsStagingWriter(self, reservation, descriptor)
            except Exception as exc:
                paths.receiving.unlink(missing_ok=True)
                paths.reservation.unlink(missing_ok=True)
                fsync_directory(self._directory)
                if isinstance(exc, FtpsStagingError):
                    raise
                raise FtpsStagingError from exc

    def stage(self, reservation: FtpsStageReservation, chunks: Iterable[bytes]) -> StagedArtifact:
        """Compatibility helper that streams an iterable through the single-use writer."""
        with self.begin(reservation) as writer:
            for chunk in chunks:
                writer.write(chunk)
            return writer.commit()

    def inspect(self, reservation: FtpsStageReservation) -> StagedArtifact:
        """Return an exact principal's receipt after re-verifying retained bytes."""
        _require_reservation(reservation)
        self._require_directory()
        paths = self._paths(reservation)
        try:
            _require_unlinked_private_file(paths.receipt)
            artifact = _read_receipt(paths.receipt)
            if artifact.reservation != reservation:
                raise FtpsStagingError
            descriptor = _open_verified_source(paths.source, artifact)
            os.close(descriptor)
            return artifact
        except (OSError, PrivateFileError, ValueError, json.JSONDecodeError) as exc:
            raise FtpsStagingError from exc

    def read_chunks(
        self, reservation: FtpsStageReservation, *, chunk_bytes: int = _CHUNK_BYTES
    ) -> Iterator[bytes]:
        """Yield bounded immutable bytes only after complete exact-principal revalidation."""
        _require_reservation(reservation)
        self._require_directory()
        if type(chunk_bytes) is not int or not 1 <= chunk_bytes <= _CHUNK_BYTES:
            raise FtpsStagingError
        paths = self._paths(reservation)
        try:
            _require_unlinked_private_file(paths.receipt)
            artifact = _read_receipt(paths.receipt)
            if artifact.reservation != reservation or artifact.consumed:
                raise FtpsStagingError
            descriptor = _open_verified_source(paths.source, artifact)
            try:
                while chunk := os.read(descriptor, chunk_bytes):
                    yield chunk
            finally:
                os.close(descriptor)
        except (OSError, PrivateFileError, ValueError, json.JSONDecodeError) as exc:
            raise FtpsStagingError from exc

    def consume(self, reservation: FtpsStageReservation) -> StagedArtifact:
        """Atomically transition one exact complete stage to consumed at most once."""
        artifact = self.inspect(reservation)
        if artifact.consumed:
            raise FtpsStagingError
        paths = self._paths(reservation)
        update_created = False
        try:
            _write_receipt(paths.updating, replace(artifact, consumed=True))
            update_created = True
            # The exclusive update file is the consume lock. Re-read while holding it
            # so a contender that acquired the lock later cannot reuse stale evidence.
            artifact = self.inspect(reservation)
            if artifact.consumed:
                raise FtpsStagingError
            os.replace(paths.updating, paths.receipt)
            update_created = False
            fsync_directory(self._directory)
            return replace(artifact, consumed=True)
        except Exception as exc:
            if update_created:
                paths.updating.unlink(missing_ok=True)
            fsync_directory(self._directory)
            if isinstance(exc, FtpsStagingError):
                raise
            raise FtpsStagingError from exc

    def remove_consumed(self, reservation: FtpsStageReservation) -> None:
        """Remove only one exact consumed stage, with a restart-safe removing marker."""
        _require_reservation(reservation)
        self._require_directory()
        paths = self._paths(reservation)
        try:
            if os.path.lexists(paths.removing):
                _require_unlinked_private_file(paths.removing)
                artifact = _read_receipt(paths.removing)
                if artifact.reservation != reservation or not artifact.consumed:
                    raise FtpsStagingError
            else:
                artifact = self.inspect(reservation)
                if not artifact.consumed:
                    raise FtpsStagingError
                os.replace(paths.receipt, paths.removing)
                fsync_directory(self._directory)
            paths.source.unlink(missing_ok=True)
            paths.removing.unlink()
            fsync_directory(self._directory)
        except (OSError, PrivateFileError, ValueError, json.JSONDecodeError) as exc:
            raise FtpsStagingError from exc

    def _commit(self, reservation: FtpsStageReservation, artifact: StagedArtifact) -> None:
        paths = self._paths(reservation)
        try:
            _require_unlinked_private_file(paths.reservation)
            if _read_reservation(paths.reservation) != reservation:
                raise FtpsStagingError
            _require_unlinked_private_file(paths.receiving)
            os.link(paths.receiving, paths.source, follow_symlinks=False)
            paths.receiving.unlink()
            fsync_directory(self._directory)
            _write_receipt(paths.receipt, artifact)
            paths.reservation.unlink()
            fsync_directory(self._directory)
        except Exception as exc:
            paths.receipt.unlink(missing_ok=True)
            paths.source.unlink(missing_ok=True)
            paths.receiving.unlink(missing_ok=True)
            paths.reservation.unlink(missing_ok=True)
            fsync_directory(self._directory)
            if isinstance(exc, FtpsStagingError):
                raise
            raise FtpsStagingError from exc

    def _abort(self, reservation: FtpsStageReservation) -> None:
        paths = self._paths(reservation)
        try:
            for path in (paths.receiving, paths.reservation):
                if os.path.lexists(path):
                    _require_unlinked_private_file(path)
                    path.unlink()
            fsync_directory(self._directory)
        except (OSError, PrivateFileError, ValueError) as exc:
            raise FtpsStagingError from exc

    def _require_directory(self) -> None:
        try:
            require_private_directory(self._directory)
        except (OSError, PrivateFileError, ValueError) as exc:
            raise FtpsStagingError from exc

    def _require_available_capacity(self) -> None:
        stages: dict[str, set[str]] = defaultdict(set)
        try:
            for path in self._directory.iterdir():
                match = _STAGE_ENTRY.fullmatch(path.name)
                if match is None:
                    raise FtpsStagingError
                _require_unlinked_private_file(path)
                stages[match.group(1)].add(match.group(2))
            valid_states = {
                frozenset({"reservation", "receiving"}),
                frozenset({"source", "receipt"}),
                frozenset({"source", "receipt", "updating"}),
                frozenset({"source", "removing"}),
                frozenset({"removing"}),
            }
            if any(frozenset(entries) not in valid_states for entries in stages.values()):
                raise FtpsStagingError
            if len(stages) >= self._capacity:
                raise FtpsStagingError
        except (OSError, PrivateFileError, ValueError) as exc:
            raise FtpsStagingError from exc

    def _paths(self, reservation: FtpsStageReservation) -> _StagePaths:
        return self._paths_for_id(reservation.staging_id)

    def _paths_for_id(self, stage_id: str) -> _StagePaths:
        base = self._directory / stage_id
        return _StagePaths(
            reservation=base.with_suffix(".reservation"),
            receiving=base.with_suffix(".receiving"),
            source=base.with_suffix(".source"),
            receipt=base.with_suffix(".receipt"),
            updating=base.with_suffix(".updating"),
            removing=base.with_suffix(".removing"),
        )


@dataclass(frozen=True, slots=True)
class _StagePaths:
    reservation: Path
    receiving: Path
    source: Path
    receipt: Path
    updating: Path
    removing: Path

    def __iter__(self) -> Iterator[Path]:
        return iter(
            (
                self.reservation,
                self.receiving,
                self.source,
                self.receipt,
                self.updating,
                self.removing,
            )
        )


def _require_reservation(value: object) -> None:
    if type(value) is not FtpsStageReservation:
        raise FtpsStagingError


def _require_stage_reservation(path: Path, stage_id: str) -> FtpsStageReservation:
    _require_unlinked_private_file(path)
    reservation = _read_reservation(path)
    if reservation.staging_id != stage_id:
        raise FtpsStagingError
    return reservation


def _require_stage_receipt(path: Path, stage_id: str) -> StagedArtifact:
    _require_unlinked_private_file(path)
    artifact = _read_receipt(path)
    if artifact.reservation.staging_id != stage_id:
        raise FtpsStagingError
    return artifact


def _require_canonical_uuid4(value: object) -> None:
    if type(value) is not str:
        raise FtpsStagingError
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise FtpsStagingError from exc
    if parsed.version != 4 or str(parsed) != value:
        raise FtpsStagingError


def _require_unlinked_private_file(path: Path) -> None:
    require_private_file(path)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise FtpsStagingError


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    written = 0
    while written < len(view):
        count = os.write(descriptor, view[written:])
        if count <= 0:
            raise FtpsStagingError
        written += count


def _reservation_document(reservation: FtpsStageReservation) -> dict[str, object]:
    return {
        "client_path": reservation.client_path,
        "created_at_unix_ms": reservation.created_at_unix_ms,
        "expires_at_unix_ms": reservation.expires_at_unix_ms,
        "printer_uuid": reservation.printer_uuid,
        "staging_id": reservation.staging_id,
        "version": _RECEIPT_VERSION,
    }


def _write_document(path: Path, document: dict[str, object]) -> None:
    payload = json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "ascii"
    )
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
        _require_unlinked_private_file(path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_receipt(path: Path, artifact: StagedArtifact) -> None:
    _write_document(path, _receipt_document(artifact))


def _receipt_document(artifact: StagedArtifact) -> dict[str, object]:
    return {
        **_reservation_document(artifact.reservation),
        "archive_sha256": artifact.archive_sha256,
        "archive_size_bytes": artifact.archive_size_bytes,
        "consumed": artifact.consumed,
    }


def _is_exact_partial_receipt(path: Path, artifact: StagedArtifact) -> bool:
    _require_unlinked_private_file(path)
    expected = json.dumps(
        _receipt_document(artifact), ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    if path.lstat().st_size >= len(expected):
        return False
    return expected.startswith(path.read_bytes())


def _read_reservation(path: Path) -> FtpsStageReservation:
    payload = json.loads(path.read_bytes())
    required = {
        "client_path",
        "created_at_unix_ms",
        "expires_at_unix_ms",
        "printer_uuid",
        "staging_id",
        "version",
    }
    if (
        type(payload) is not dict
        or set(payload) != required
        or payload["version"] != _RECEIPT_VERSION
    ):
        raise FtpsStagingError
    return FtpsStageReservation(
        staging_id=payload["staging_id"],
        printer_uuid=payload["printer_uuid"],
        client_path=payload["client_path"],
        created_at_unix_ms=payload["created_at_unix_ms"],
        expires_at_unix_ms=payload["expires_at_unix_ms"],
    )


def _read_receipt(path: Path) -> StagedArtifact:
    payload = json.loads(path.read_bytes())
    required = {
        "archive_sha256",
        "archive_size_bytes",
        "client_path",
        "consumed",
        "created_at_unix_ms",
        "expires_at_unix_ms",
        "printer_uuid",
        "staging_id",
        "version",
    }
    if (
        type(payload) is not dict
        or set(payload) != required
        or payload["version"] != _RECEIPT_VERSION
    ):
        raise FtpsStagingError
    return StagedArtifact(
        reservation=FtpsStageReservation(
            staging_id=payload["staging_id"],
            printer_uuid=payload["printer_uuid"],
            client_path=payload["client_path"],
            created_at_unix_ms=payload["created_at_unix_ms"],
            expires_at_unix_ms=payload["expires_at_unix_ms"],
        ),
        archive_size_bytes=payload["archive_size_bytes"],
        archive_sha256=payload["archive_sha256"],
        consumed=payload["consumed"],
    )


def _open_verified_source(path: Path, artifact: StagedArtifact) -> int:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (os.name != "nt" and (metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077))
            or metadata.st_size != artifact.archive_size_bytes
        ):
            raise FtpsStagingError
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, _CHUNK_BYTES):
            digest.update(chunk)
        if f"sha256:{digest.hexdigest()}" != artifact.archive_sha256:
            raise FtpsStagingError
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise


def _open_and_close_verified_source(path: Path, artifact: StagedArtifact) -> None:
    descriptor = _open_verified_source(path, artifact)
    os.close(descriptor)


def _artifact_from_source(
    path: Path, reservation: FtpsStageReservation, limits: ArtifactLimits
) -> StagedArtifact:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (os.name != "nt" and (metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077))
            or metadata.st_size > limits.max_archive_compressed_bytes
        ):
            raise FtpsStagingError
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, _CHUNK_BYTES):
            digest.update(chunk)
        return StagedArtifact(
            reservation=reservation,
            archive_size_bytes=metadata.st_size,
            archive_sha256=f"sha256:{digest.hexdigest()}",
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
