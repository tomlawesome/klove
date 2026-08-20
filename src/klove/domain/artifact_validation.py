"""Bounded inspection of hostile G-code 3MF archives without extraction or transport.

Stable public facade: orchestrates the low-level immutable ZIP/archive layout
and metadata validation in ``artifact_archive`` and the bounded G-code
scanning in ``artifact_gcode``, and re-exports their internal names for
existing consumers of ``klove.domain.artifact_validation``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import BinaryIO, cast
from zipfile import BadZipFile, LargeZipFile, ZipFile

from klove.domain.artifact_archive import (
    _PARSER_ERRORS,
    _Denied,
    _ImmutableBytesReader,
    _read_archive_layout,
    _read_exact_at,
    _validate_archive_metadata,
    _validate_zip_info,
    _verify_intake,
)
from klove.domain.artifact_gcode import _read_selected_gcode
from klove.domain.artifacts import (
    CONTRACT_VERSION,
    ArtifactBoundary,
    ArtifactFailure,
    ArtifactFailureCode,
    ArtifactInspectionEvidence,
    ArtifactIntent,
    ArtifactLimits,
    CompressionRatioEvidence,
    SelectedPlate,
)

__all__ = [
    "ValidatedGcodeCandidate",
    "_Denied",
    "_ImmutableBytesReader",
    "_read_exact_at",
    "_read_selected_gcode",
    "_validate_zip_info",
    "inspect_gcode_3mf",
    "open_selected_gcode",
]


@dataclass(frozen=True, slots=True)
class ValidatedGcodeCandidate:
    """One immutable verified source archive and the evidence derived from it."""

    inspection: ArtifactInspectionEvidence
    source_archive: bytes = field(repr=False)


def inspect_gcode_3mf(
    intent: ArtifactIntent,
    archive: bytes,
    *,
    limits: ArtifactLimits,
) -> ValidatedGcodeCandidate | ArtifactFailure:
    """Validate one caller-owned immutable archive snapshot.

    The returned candidate retains the same immutable ``bytes`` object so future
    stages can consume the exact verified source. This function does not extract
    files, write to storage, contact a printer, or authorize dispatch.
    """
    try:
        _verify_intake(intent, archive, limits)
        source = _ImmutableBytesReader(archive)
        layout = _read_archive_layout(source, len(archive), limits)
        source.seek(0)
        with ZipFile(cast(BinaryIO, source), mode="r", allowZip64=True) as bundle:
            metadata = _validate_archive_metadata(intent, bundle, layout, limits)
            selected_gcode = _read_selected_gcode(bundle, metadata.selected, limits)
    except _Denied as denial:
        return ArtifactFailure(boundary=denial.boundary, code=denial.code)
    except _PARSER_ERRORS:
        return ArtifactFailure(
            boundary=ArtifactBoundary.ARCHIVE,
            code=ArtifactFailureCode.MALFORMED,
        )

    selected = SelectedPlate(
        plate_id=intent.selected_plate.plate_id,
        archive_path=intent.selected_plate.archive_path,
        gcode_sha256=selected_gcode.sha256,
        gcode_size_bytes=selected_gcode.size_bytes,
    )
    inspection = ArtifactInspectionEvidence(
        contract_version=CONTRACT_VERSION,
        artifact=intent.artifact,
        selected_plate=selected,
        zip_entry_count=metadata.entries,
        archive_expanded_bytes=metadata.expanded_bytes,
        highest_ratio_entry=CompressionRatioEvidence(
            compressed_bytes=metadata.highest_ratio_compressed_bytes,
            expanded_bytes=metadata.highest_ratio_expanded_bytes,
        ),
    )
    return ValidatedGcodeCandidate(inspection=inspection, source_archive=archive)


@contextmanager
def open_selected_gcode(candidate: ValidatedGcodeCandidate) -> Iterator[BinaryIO]:
    """Open the exact selected member from a previously re-inspected candidate."""
    try:
        source = _ImmutableBytesReader(candidate.source_archive)
        bundle = ZipFile(cast(BinaryIO, source), mode="r", allowZip64=True)
    except (BadZipFile, LargeZipFile, OSError, RuntimeError, ValueError) as exc:
        raise ValueError("selected member is unavailable") from exc
    try:
        info = bundle.getinfo(candidate.inspection.selected_plate.archive_path)
        if info.is_dir() or info.file_size != candidate.inspection.selected_plate.gcode_size_bytes:
            raise ValueError("selected member no longer matches inspection")
        selected = cast(BinaryIO, bundle.open(info, mode="r"))
    except (BadZipFile, KeyError, LargeZipFile, OSError, RuntimeError, ValueError) as exc:
        bundle.close()
        raise ValueError("selected member is unavailable") from exc
    try:
        yield selected
    finally:
        selected.close()
        bundle.close()
