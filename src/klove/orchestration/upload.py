"""Single-dispatch upload policy and post-upload remote reconciliation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import BinaryIO, Protocol

from klove.config import PrinterConfig
from klove.domain.artifact_validation import (
    ValidatedGcodeCandidate,
    inspect_gcode_3mf,
    open_selected_gcode,
)
from klove.domain.artifacts import (
    CONTRACT_VERSION,
    ArtifactFailure,
    ArtifactIntent,
    ArtifactLimits,
    ArtifactQualification,
    PlateSelection,
    SafetyProfile,
    Sha256Digest,
    target_for_safety_profile,
)
from klove.domain.upload import (
    MoonrakerGcodeMetadata,
    MoonrakerUploadReceipt,
    RemoteFileDigest,
    UploadBoundary,
    UploadFailure,
    UploadFailureCode,
    UploadOperationResult,
    UploadState,
    VerifiedUpload,
)


class UploadTransport(Protocol):
    """Narrow file-only boundary around one configured Moonraker endpoint."""

    async def upload(
        self,
        path: str,
        content: BinaryIO,
        sha256: Sha256Digest,
        size_bytes: int,
    ) -> MoonrakerUploadReceipt: ...

    async def metadata(self, path: str) -> MoonrakerGcodeMetadata | None: ...

    async def download(self, path: str, expected_size: int) -> RemoteFileDigest: ...


@dataclass(frozen=True, slots=True)
class _UploadRequest:
    qualification: ArtifactQualification
    inspection: object


@dataclass(frozen=True, slots=True)
class _JournalEntry:
    request: _UploadRequest
    task: asyncio.Task[UploadOperationResult]


class UploadService:
    """Serialize one printer's uploads and retain every terminal dispatch result."""

    def __init__(
        self,
        printer: PrinterConfig,
        transport: UploadTransport,
        *,
        limits: ArtifactLimits,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._printer = printer
        self._transport = transport
        self._limits = limits
        self._clock = clock
        self._sleep = sleep
        self._journal_lock = asyncio.Lock()
        self._printer_lock = asyncio.Lock()
        self._by_key: dict[str, _JournalEntry] = {}
        self._by_operation: dict[str, _JournalEntry] = {}

    async def execute(
        self,
        candidate: ValidatedGcodeCandidate,
        qualification: ArtifactQualification,
    ) -> UploadOperationResult:
        """Upload one exact qualification once; duplicates share its terminal result."""
        request = _UploadRequest(qualification, candidate.inspection)
        async with self._journal_lock:
            keyed = self._by_key.get(qualification.idempotency_key)
            operated = self._by_operation.get(qualification.operation_id)
            existing = keyed or operated
            if existing is not None:
                if keyed is not existing or operated is not existing or existing.request != request:
                    return _denied(qualification, UploadFailureCode.IDEMPOTENCY_CONFLICT)
                task = existing.task
            else:
                if len(self._by_operation) >= self._limits.upload_idempotency_capacity:
                    return _denied(qualification, UploadFailureCode.CAPACITY_EXHAUSTED)
                task = asyncio.create_task(self._execute_safely(candidate, qualification))
                entry = _JournalEntry(request, task)
                self._by_key[qualification.idempotency_key] = entry
                self._by_operation[qualification.operation_id] = entry
        return await asyncio.shield(task)

    async def _execute_safely(  # noqa: PLR0911 -- fail-closed exits bound dispatch.
        self,
        candidate: ValidatedGcodeCandidate,
        qualification: ArtifactQualification,
    ) -> UploadOperationResult:
        async with self._printer_lock:
            try:
                profile, refreshed = self._preflight(candidate, qualification)
            except (TypeError, ValueError):
                return _denied(qualification, UploadFailureCode.SOURCE_INVALID)
            if profile is None:
                return _denied(qualification, UploadFailureCode.TARGET_STALE)
            if refreshed is None:
                return _denied(qualification, UploadFailureCode.SOURCE_INVALID)

            path = f"klove/{qualification.operation_id}.gcode"
            dispatch_begun = False
            try:
                with open_selected_gcode(refreshed) as content:
                    dispatch_begun = True
                    receipt = await self._transport.upload(
                        path,
                        content,
                        qualification.selected_plate.gcode_sha256,
                        qualification.selected_plate.gcode_size_bytes,
                    )
                    return await self._reconcile(
                        qualification,
                        profile,
                        path,
                        receipt,
                    )
            except asyncio.CancelledError:
                return _unknown(
                    qualification,
                    UploadBoundary.TRANSPORT,
                    UploadFailureCode.TRANSPORT_AMBIGUOUS,
                )
            except Exception:
                if dispatch_begun:
                    return _unknown(
                        qualification,
                        UploadBoundary.TRANSPORT,
                        UploadFailureCode.TRANSPORT_AMBIGUOUS,
                    )
                return _denied(qualification, UploadFailureCode.SOURCE_INVALID)

    def _preflight(
        self,
        candidate: ValidatedGcodeCandidate,
        qualification: ArtifactQualification,
    ) -> tuple[SafetyProfile | None, ValidatedGcodeCandidate | None]:
        if qualification.target.printer_uuid != self._printer.uuid:
            return None, None
        profiles = tuple(
            profile
            for profile in self._printer.safety_profiles
            if profile.slicer_profile_id == qualification.target.slicer_profile_id
        )
        if len(profiles) != 1:
            return None, None
        profile = profiles[0]
        if target_for_safety_profile(profile) != qualification.target:
            return None, None
        if (
            candidate.inspection.artifact != qualification.artifact
            or candidate.inspection.selected_plate != qualification.selected_plate
        ):
            return profile, None
        intent = ArtifactIntent(
            contract_version=CONTRACT_VERSION,
            operation_id=qualification.operation_id,
            idempotency_key=qualification.idempotency_key,
            artifact=qualification.artifact,
            selected_plate=PlateSelection(
                plate_id=qualification.selected_plate.plate_id,
                archive_path=qualification.selected_plate.archive_path,
            ),
            target=qualification.target,
        )
        refreshed = inspect_gcode_3mf(intent, candidate.source_archive, limits=self._limits)
        if isinstance(refreshed, ArtifactFailure) or refreshed.inspection != candidate.inspection:
            return profile, None
        return profile, refreshed

    async def _reconcile(
        self,
        qualification: ArtifactQualification,
        profile: SafetyProfile,
        path: str,
        receipt: MoonrakerUploadReceipt,
    ) -> UploadOperationResult:
        expected_size = qualification.selected_plate.gcode_size_bytes
        if receipt.path != path or receipt.size_bytes != expected_size:
            return _unknown(
                qualification,
                UploadBoundary.REMOTE_FILE,
                UploadFailureCode.REMOTE_MISMATCH,
            )
        metadata = await self._wait_for_metadata(path)
        if metadata is None:
            return _unknown(
                qualification,
                UploadBoundary.METADATA,
                UploadFailureCode.METADATA_TIMEOUT,
            )
        if not _metadata_matches(receipt, metadata, profile, expected_size):
            return _unknown(
                qualification,
                UploadBoundary.METADATA,
                UploadFailureCode.REMOTE_MISMATCH,
            )
        remote_file = await self._transport.download(path, expected_size)
        if (
            remote_file.size_bytes != expected_size
            or remote_file.sha256 != qualification.selected_plate.gcode_sha256
        ):
            return _unknown(
                qualification,
                UploadBoundary.REMOTE_FILE,
                UploadFailureCode.REMOTE_MISMATCH,
            )
        metadata_after = await self._transport.metadata(path)
        if metadata_after != metadata:
            return _unknown(
                qualification,
                UploadBoundary.METADATA,
                UploadFailureCode.REMOTE_MISMATCH,
            )
        return UploadOperationResult(
            operation_id=qualification.operation_id,
            idempotency_key=qualification.idempotency_key,
            state=UploadState.VERIFIED,
            verified=VerifiedUpload(
                path=path,
                qualification=qualification,
                receipt=receipt,
                metadata=metadata,
                remote_file=remote_file,
            ),
        )

    async def _wait_for_metadata(self, path: str) -> MoonrakerGcodeMetadata | None:
        deadline = self._clock() + self._limits.metadata_wait_seconds
        while True:
            metadata = await self._transport.metadata(path)
            if metadata is not None:
                return metadata
            remaining = deadline - self._clock()
            if remaining <= 0:
                return None
            await self._sleep(min(self._limits.metadata_poll_interval_seconds, remaining))


def _metadata_matches(
    receipt: MoonrakerUploadReceipt,
    metadata: MoonrakerGcodeMetadata,
    profile: SafetyProfile,
    expected_size: int,
) -> bool:
    return (
        metadata.path == receipt.path
        and metadata.size_bytes == expected_size
        and metadata.modified == receipt.modified
        and not metadata.file_processors
        and metadata.nozzle_diameter_micrometres
        == profile.compatibility.nozzle_diameter_micrometres
    )


def _denied(
    qualification: ArtifactQualification,
    code: UploadFailureCode,
) -> UploadOperationResult:
    boundary = (
        UploadBoundary.IDEMPOTENCY
        if code in {UploadFailureCode.IDEMPOTENCY_CONFLICT, UploadFailureCode.CAPACITY_EXHAUSTED}
        else UploadBoundary.SOURCE
        if code is UploadFailureCode.SOURCE_INVALID
        else UploadBoundary.QUALIFICATION
    )
    return UploadOperationResult(
        operation_id=qualification.operation_id,
        idempotency_key=qualification.idempotency_key,
        state=UploadState.DENIED,
        failure=UploadFailure(boundary=boundary, code=code),
    )


def _unknown(
    qualification: ArtifactQualification,
    boundary: UploadBoundary,
    code: UploadFailureCode,
) -> UploadOperationResult:
    return UploadOperationResult(
        operation_id=qualification.operation_id,
        idempotency_key=qualification.idempotency_key,
        state=UploadState.OUTCOME_UNKNOWN,
        failure=UploadFailure(boundary=boundary, code=code),
    )
