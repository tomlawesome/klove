"""Bounded, immutable ZIP archive layout and central-directory metadata validation."""

from __future__ import annotations

import hashlib
import hmac
import stat
import struct
from dataclasses import dataclass
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, LargeZipFile, ZipFile, ZipInfo

from klove.domain.artifact_contract_models import (
    ArtifactBoundary,
    ArtifactFailureCode,
    ArtifactIntent,
    ArtifactLimits,
)

_EOCD = struct.Struct("<4s4H2LH")
_ZIP64_LOCATOR = struct.Struct("<4sLQL")
_ZIP64_EOCD = struct.Struct("<4sQ2H2L4Q")
_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
_ZIP64_SENTINEL_16 = 0xFFFF
_ZIP64_SENTINEL_32 = 0xFFFFFFFF
_ZIP64_EOCD_BODY_BYTES = 44
_ENCRYPTION_FLAGS = (1 << 0) | (1 << 6) | (1 << 13)
_SUPPORTED_FLAGS = (1 << 1) | (1 << 2) | (1 << 3) | (1 << 11)
_DIRECTORY_ATTRIBUTE = 0x10
_REQUIRED_3MF_PARTS = frozenset({"[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model"})
_PARSER_ERRORS = (
    BadZipFile,
    LargeZipFile,
    EOFError,
    NotImplementedError,
    OSError,
    RuntimeError,
    struct.error,
    ValueError,
)


@dataclass(frozen=True, slots=True)
class _ArchiveLayout:
    entries: int
    central_directory_bytes: int
    central_directory_offset: int


@dataclass(frozen=True, slots=True)
class _ArchiveMetadata:
    entries: int
    expanded_bytes: int
    highest_ratio_compressed_bytes: int
    highest_ratio_expanded_bytes: int
    selected: ZipInfo


@dataclass(frozen=True, slots=True)
class _Denied(Exception):
    boundary: ArtifactBoundary
    code: ArtifactFailureCode


class _ImmutableBytesReader:
    """Minimal seekable reader over immutable bytes without a full-buffer copy."""

    def __init__(self, value: bytes) -> None:
        self._value = value
        self._position = 0

    def read(self, size: int = -1) -> bytes:
        """Read at most ``size`` bytes from the current position."""
        end = len(self._value) if size < 0 else min(self._position + size, len(self._value))
        result = self._value[self._position : end]
        self._position = end
        return result

    def seek(self, offset: int, whence: int = 0) -> int:
        """Seek with ordinary binary-file semantics."""
        if whence == 0:
            position = offset
        elif whence == 1:
            position = self._position + offset
        elif whence == 2:
            position = len(self._value) + offset
        else:
            raise ValueError("unsupported whence")
        if position < 0:
            raise ValueError("negative seek position")
        self._position = position
        return position

    def tell(self) -> int:
        """Return the current byte offset."""
        return self._position

    def seekable(self) -> bool:
        """Declare support for the random access required by ``zipfile``."""
        return True


def _verify_intake(intent: ArtifactIntent, archive: bytes, limits: ArtifactLimits) -> None:
    """Verify the type, exact physical size, and digest of an immutable snapshot."""
    if type(archive) is not bytes:
        raise _Denied(ArtifactBoundary.INTAKE, ArtifactFailureCode.UNSUPPORTED_ARCHIVE)
    physical_size = len(archive)
    if physical_size > limits.max_archive_compressed_bytes:
        raise _Denied(ArtifactBoundary.INTAKE, ArtifactFailureCode.LIMIT_EXCEEDED)
    if physical_size != intent.artifact.compressed_size_bytes:
        raise _Denied(ArtifactBoundary.INTAKE, ArtifactFailureCode.INTEGRITY_FAILED)
    expected = intent.artifact.archive_sha256.removeprefix("sha256:")
    if not hmac.compare_digest(hashlib.sha256(archive).hexdigest(), expected):
        raise _Denied(ArtifactBoundary.INTAKE, ArtifactFailureCode.INTEGRITY_FAILED)


def _read_archive_layout(
    archive: _ImmutableBytesReader,
    physical_size: int,
    limits: ArtifactLimits,
) -> _ArchiveLayout:
    """Bound the ZIP central directory before ``zipfile`` can allocate for it."""
    if physical_size < _EOCD.size:
        raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.MALFORMED)
    eocd_offset = physical_size - _EOCD.size
    eocd = _read_exact_at(archive, eocd_offset, _EOCD.size)
    (
        signature,
        disk_number,
        directory_disk,
        disk_entries,
        total_entries,
        directory_size,
        directory_offset,
        comment_size,
    ) = _EOCD.unpack(eocd)
    if signature != _EOCD_SIGNATURE or comment_size != 0:
        raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.MALFORMED)

    uses_zip64 = any(
        value == sentinel
        for value, sentinel in (
            (disk_number, _ZIP64_SENTINEL_16),
            (directory_disk, _ZIP64_SENTINEL_16),
            (disk_entries, _ZIP64_SENTINEL_16),
            (total_entries, _ZIP64_SENTINEL_16),
            (directory_size, _ZIP64_SENTINEL_32),
            (directory_offset, _ZIP64_SENTINEL_32),
        )
    )
    if uses_zip64:
        layout = _read_zip64_layout(archive, eocd_offset)
        legacy_fields = (
            (disk_number, _ZIP64_SENTINEL_16, 0),
            (directory_disk, _ZIP64_SENTINEL_16, 0),
            (disk_entries, _ZIP64_SENTINEL_16, layout.entries),
            (total_entries, _ZIP64_SENTINEL_16, layout.entries),
            (directory_size, _ZIP64_SENTINEL_32, layout.central_directory_bytes),
            (directory_offset, _ZIP64_SENTINEL_32, layout.central_directory_offset),
        )
        if any(value not in (sentinel, actual) for value, sentinel, actual in legacy_fields):
            raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.MALFORMED)
    else:
        if disk_number != 0 or directory_disk != 0 or disk_entries != total_entries:
            raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.UNSUPPORTED_ARCHIVE)
        if directory_offset + directory_size != eocd_offset:
            raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.MALFORMED)
        layout = _ArchiveLayout(total_entries, directory_size, directory_offset)

    if layout.entries > limits.max_zip_entries:
        raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.LIMIT_EXCEEDED)
    if layout.central_directory_bytes > limits.max_zip_metadata_bytes:
        raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.LIMIT_EXCEEDED)
    if layout.entries == 0 or layout.central_directory_bytes == 0:
        raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.MALFORMED)
    return layout


def _read_zip64_layout(archive: _ImmutableBytesReader, eocd_offset: int) -> _ArchiveLayout:
    """Read the strict single-disk ZIP64 records immediately preceding EOCD."""
    locator_offset = eocd_offset - _ZIP64_LOCATOR.size
    if locator_offset < _ZIP64_EOCD.size:
        raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.MALFORMED)
    locator = _read_exact_at(archive, locator_offset, _ZIP64_LOCATOR.size)
    signature, record_disk, record_offset, total_disks = _ZIP64_LOCATOR.unpack(locator)
    if signature != _ZIP64_LOCATOR_SIGNATURE:
        raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.MALFORMED)
    if record_disk != 0 or total_disks != 1:
        raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.UNSUPPORTED_ARCHIVE)
    record = _read_exact_at(archive, record_offset, _ZIP64_EOCD.size)
    (
        signature,
        record_size,
        _version_made,
        version_needed,
        disk_number,
        directory_disk,
        disk_entries,
        total_entries,
        directory_size,
        directory_offset,
    ) = _ZIP64_EOCD.unpack(record)
    if signature != _ZIP64_EOCD_SIGNATURE or record_size != _ZIP64_EOCD_BODY_BYTES:
        raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.MALFORMED)
    if record_offset + _ZIP64_EOCD.size != locator_offset:
        raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.MALFORMED)
    if version_needed > 45:
        raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.UNSUPPORTED_ARCHIVE)
    if disk_number != 0 or directory_disk != 0 or disk_entries != total_entries:
        raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.UNSUPPORTED_ARCHIVE)
    if directory_offset + directory_size != record_offset:
        raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.MALFORMED)
    return _ArchiveLayout(total_entries, directory_size, directory_offset)


def _read_exact_at(archive: _ImmutableBytesReader, offset: int, size: int) -> bytes:
    """Read one fixed-size ZIP structure or return a bounded denial."""
    if offset < 0:
        raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.MALFORMED)
    try:
        archive.seek(offset)
        value = archive.read(size)
    except (OSError, TypeError, ValueError) as error:
        raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.MALFORMED) from error
    if not isinstance(value, bytes) or len(value) != size:
        raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.MALFORMED)
    return value


def _validate_archive_metadata(  # noqa: PLR0912 -- each hostile metadata check is explicit.
    intent: ArtifactIntent,
    bundle: ZipFile,
    layout: _ArchiveLayout,
    limits: ArtifactLimits,
) -> _ArchiveMetadata:
    """Validate every central-directory entry before reading any member body."""
    infos = bundle.infolist()
    if len(infos) != layout.entries:
        raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.MALFORMED)

    names: set[str] = set()
    folded_names: set[str] = set()
    expanded_bytes = 0
    highest_compressed = 0
    highest_expanded = 0
    selected: ZipInfo | None = None
    for info in infos:
        _validate_zip_info(info)
        name = info.filename
        folded_name = name.casefold()
        if name in names or folded_name in folded_names:
            raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.UNSAFE_ARCHIVE)
        names.add(name)
        folded_names.add(folded_name)

        expanded_bytes += info.file_size
        if expanded_bytes > limits.max_archive_expanded_bytes:
            raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.LIMIT_EXCEEDED)
        if info.file_size:
            if info.compress_size == 0:
                raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.LIMIT_EXCEEDED)
            if info.file_size > limits.max_compression_ratio * info.compress_size:
                raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.LIMIT_EXCEEDED)
            if (
                highest_expanded == 0
                or info.file_size * highest_compressed > highest_expanded * info.compress_size
            ):
                highest_compressed = info.compress_size
                highest_expanded = info.file_size
        if name == intent.selected_plate.archive_path:
            selected = info

    if not _REQUIRED_3MF_PARTS.issubset(names):
        raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.UNSUPPORTED_ARCHIVE)
    if selected is None:
        raise _Denied(ArtifactBoundary.SELECTED_PLATE, ArtifactFailureCode.EVIDENCE_MISSING)
    if selected.is_dir() or selected.file_size == 0:
        raise _Denied(ArtifactBoundary.SELECTED_PLATE, ArtifactFailureCode.INVALID_GCODE)
    if selected.file_size > limits.max_gcode_bytes:
        raise _Denied(ArtifactBoundary.SELECTED_PLATE, ArtifactFailureCode.LIMIT_EXCEEDED)

    for info in infos:
        try:
            with bundle.open(info, mode="r"):
                pass
        except _PARSER_ERRORS as error:
            raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.MALFORMED) from error

    return _ArchiveMetadata(
        entries=len(infos),
        expanded_bytes=expanded_bytes,
        highest_ratio_compressed_bytes=highest_compressed,
        highest_ratio_expanded_bytes=highest_expanded,
        selected=selected,
    )


def _validate_zip_info(info: ZipInfo) -> None:
    """Reject unsafe, ambiguous, encrypted, and unsupported ZIP entries."""
    if info.filename != info.orig_filename or not _canonical_member_name(info):
        raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.UNSAFE_ARCHIVE)
    if info.flag_bits & _ENCRYPTION_FLAGS:
        raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.UNSAFE_ARCHIVE)
    if info.flag_bits & ~_SUPPORTED_FLAGS:
        raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.UNSUPPORTED_ARCHIVE)
    if info.extract_version > 45 or info.compress_type not in {ZIP_STORED, ZIP_DEFLATED}:
        raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.UNSUPPORTED_ARCHIVE)
    if info.volume != 0 or info.comment:
        raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.UNSUPPORTED_ARCHIVE)
    if info.file_size < 0 or info.compress_size < 0 or info.header_offset < 0:
        raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.MALFORMED)

    file_type = stat.S_IFMT((info.external_attr >> 16) & 0xFFFF)
    has_directory_attribute = bool(info.external_attr & _DIRECTORY_ATTRIBUTE)
    if info.is_dir():
        if (
            file_type not in {0, stat.S_IFDIR}
            or info.file_size != 0
            or info.compress_size != 0
            or info.compress_type != ZIP_STORED
        ):
            raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.UNSAFE_ARCHIVE)
    elif file_type not in {0, stat.S_IFREG} or has_directory_attribute:
        raise _Denied(ArtifactBoundary.ARCHIVE, ArtifactFailureCode.UNSAFE_ARCHIVE)


def _canonical_member_name(info: ZipInfo) -> bool:
    """Return whether a ZIP name is one canonical, visible-ASCII POSIX path."""
    name = info.filename
    try:
        encoded = name.encode("ascii")
    except UnicodeEncodeError:
        return False
    if not encoded or len(encoded) > 1024 or name.startswith("/") or "\\" in name or ":" in name:
        return False
    if any(byte <= 32 or byte > 126 for byte in encoded):
        return False
    candidate = name[:-1] if info.is_dir() else name
    return all(part not in {"", ".", ".."} for part in candidate.split("/"))
