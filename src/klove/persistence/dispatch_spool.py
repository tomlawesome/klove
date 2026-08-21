"""Owner-only immutable operation spool for exact artifact continuation."""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from typing import Final
from uuid import UUID

from klove.domain.artifacts import ArtifactLimits
from klove.domain.dispatch import DispatchRequest
from klove.errors import KloveError
from klove.persistence.private_files import (
    PrivateFileError,
    fsync_directory,
    require_private_directory,
    require_private_file,
)

_CHUNK_BYTES: Final = 1024 * 1024


class DispatchSpoolError(KloveError):
    """The retained source is missing, mutable, invalid, or unsafe to access."""


class DispatchSpool:
    """Write and read one operation-derived immutable archive without aliases."""

    def __init__(self, directory: Path, *, limits: ArtifactLimits) -> None:
        self._directory = directory
        self._limits = limits

    def initialize(self) -> None:
        """Require the pre-created owner-only spool directory."""
        try:
            require_private_directory(self._directory)
        except (OSError, PrivateFileError, ValueError) as exc:
            raise DispatchSpoolError from exc

    def write(self, request: DispatchRequest, archive: bytes) -> None:
        """Durably retain one exact declared archive without replacing an existing file."""
        self._verify_bytes(request, archive)
        path = self._path(request)
        try:
            require_private_directory(self._directory)
            try:
                require_private_file(path)
            except PrivateFileError:
                pass
            else:
                self._read_checked(request)
                return
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            try:
                view = memoryview(archive)
                written = 0
                while written < len(view):
                    count = os.write(descriptor, view[written:])
                    if count <= 0:
                        raise OSError("spool write made no progress")
                    written += count
                os.fsync(descriptor)
            except BaseException:
                os.close(descriptor)
                path.unlink(missing_ok=True)
                fsync_directory(self._directory)
                raise
            os.close(descriptor)
            require_private_file(path)
            fsync_directory(self._directory)
        except (OSError, PrivateFileError, ValueError) as exc:
            raise DispatchSpoolError from exc

    async def write_stream(
        self,
        request: DispatchRequest,
        chunks: AsyncIterator[bytes],
        *,
        timeout_seconds: float,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        """Hash and retain bounded input only after its receiving row is durable."""
        try:
            async with asyncio.timeout(timeout_seconds):
                await self._write_stream(request, chunks, cancel_event=cancel_event)
        except Exception as exc:
            raise DispatchSpoolError from exc

    async def _write_stream(
        self,
        request: DispatchRequest,
        chunks: AsyncIterator[bytes],
        *,
        cancel_event: asyncio.Event | None,
    ) -> None:
        path = self._path(request)
        descriptor = -1
        created = False
        try:
            require_private_directory(self._directory)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            created = True
            digest = hashlib.sha256()
            size = 0
            iterator = aiter(chunks)
            while True:
                chunk = await _next_chunk_or_cancel(iterator, cancel_event)
                if chunk is None:
                    break
                if type(chunk) is not bytes:
                    raise DispatchSpoolError
                size += len(chunk)
                if (
                    size > request.archive_size_bytes
                    or size > self._limits.max_archive_compressed_bytes
                ):
                    raise DispatchSpoolError
                digest.update(chunk)
                view = memoryview(chunk)
                written = 0
                while written < len(view):
                    count = os.write(descriptor, view[written:])
                    if count <= 0:
                        raise OSError("spool write made no progress")
                    written += count
            if size != request.archive_size_bytes:
                raise DispatchSpoolError
            if f"sha256:{digest.hexdigest()}" != request.archive_sha256:
                raise DispatchSpoolError
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            require_private_file(path)
            fsync_directory(self._directory)
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            if created:
                path.unlink(missing_ok=True)
                fsync_directory(self._directory)
            raise

    def read(self, request: DispatchRequest) -> bytes:
        """Return the exact retained bytes only after repeat size and digest checks."""
        try:
            return self._read_checked(request)
        except (OSError, PrivateFileError, ValueError) as exc:
            raise DispatchSpoolError from exc

    def verify(self, request: DispatchRequest) -> None:
        """Prove one retained source's exact bytes without materializing it in memory."""
        path = self._path(request)
        try:
            require_private_directory(self._directory)
            require_private_file(path)
            metadata = path.stat()
            if (
                metadata.st_size != request.archive_size_bytes
                or metadata.st_size > self._limits.max_archive_compressed_bytes
            ):
                raise DispatchSpoolError
            digest = hashlib.sha256()
            with path.open("rb", buffering=0) as source:
                while chunk := source.read(_CHUNK_BYTES):
                    digest.update(chunk)
            if f"sha256:{digest.hexdigest()}" != request.archive_sha256:
                raise DispatchSpoolError
        except (OSError, PrivateFileError, ValueError) as exc:
            raise DispatchSpoolError from exc

    def remove(self, request: DispatchRequest) -> None:
        """Delete only the exact private local source after a durable safe terminal state."""
        path = self._path(request)
        try:
            require_private_directory(self._directory)
            try:
                require_private_file(path)
            except PrivateFileError:
                if os.path.lexists(path):
                    raise
                return
            path.unlink()
            fsync_directory(self._directory)
        except (OSError, PrivateFileError, ValueError) as exc:
            raise DispatchSpoolError from exc

    def discard_incomplete(self, request: DispatchRequest) -> None:
        """Remove only the receiving operation's possible partial private file."""
        self.remove(request)

    def exists(self, request: DispatchRequest) -> bool:
        """Prove whether the expected private source can still be read safely."""
        try:
            require_private_file(self._path(request))
        except PrivateFileError:
            return False
        return True

    def operation_ids(self) -> frozenset[str]:
        """List only canonical, private spool entries for journal reconciliation."""
        try:
            require_private_directory(self._directory)
            operations: set[str] = set()
            for path in self._directory.iterdir():
                operation_id = _operation_id_for_path(path)
                require_private_file(path)
                operations.add(operation_id)
            return frozenset(operations)
        except (OSError, PrivateFileError, ValueError) as exc:
            raise DispatchSpoolError from exc

    def _read_checked(self, request: DispatchRequest) -> bytes:
        path = self._path(request)
        require_private_directory(self._directory)
        require_private_file(path)
        metadata = path.stat()
        if metadata.st_size != request.archive_size_bytes:
            raise DispatchSpoolError
        if metadata.st_size > self._limits.max_archive_compressed_bytes:
            raise DispatchSpoolError
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        with path.open("rb", buffering=0) as source:
            while chunk := source.read(_CHUNK_BYTES):
                digest.update(chunk)
                chunks.append(chunk)
        data = b"".join(chunks)
        if f"sha256:{digest.hexdigest()}" != request.archive_sha256:
            raise DispatchSpoolError
        return data

    def _verify_bytes(self, request: DispatchRequest, archive: bytes) -> None:
        if len(archive) != request.archive_size_bytes:
            raise DispatchSpoolError
        if len(archive) > self._limits.max_archive_compressed_bytes:
            raise DispatchSpoolError
        digest = f"sha256:{hashlib.sha256(archive).hexdigest()}"
        if digest != request.archive_sha256:
            raise DispatchSpoolError

    def _path(self, request: DispatchRequest) -> Path:
        return self._directory / f"{request.operation_id}.gcode.3mf"


def _operation_id_for_path(path: Path) -> str:
    """Accept only the coordinator's exact operation-derived spool filename."""
    suffix = ".gcode.3mf"
    if not path.name.endswith(suffix):
        raise DispatchSpoolError
    operation_id = path.name.removesuffix(suffix)
    try:
        parsed = UUID(operation_id)
    except ValueError as exc:
        raise DispatchSpoolError from exc
    if parsed.version != 4 or str(parsed) != operation_id:
        raise DispatchSpoolError
    return operation_id


async def _next_chunk_or_cancel(
    chunks: AsyncIterator[bytes],
    cancel_event: asyncio.Event | None,
) -> bytes | None:
    """Wait for one source chunk while allowing a caller to abandon intake promptly."""
    if cancel_event is None:
        return await _next_chunk(chunks)
    if cancel_event.is_set():
        raise DispatchSpoolError
    next_chunk = asyncio.create_task(_next_chunk(chunks))
    cancelled = asyncio.create_task(cancel_event.wait())
    try:
        done, _pending = await asyncio.wait(
            (next_chunk, cancelled), return_when=asyncio.FIRST_COMPLETED
        )
        if cancelled in done or cancel_event.is_set():
            if not next_chunk.done():
                _cancel_background(next_chunk)
            raise DispatchSpoolError
        cancelled.cancel()
        with suppress(asyncio.CancelledError):
            await cancelled
        return next_chunk.result()
    except BaseException:
        if not next_chunk.done():
            _cancel_background(next_chunk)
        if not cancelled.done():
            _cancel_background(cancelled)
        raise


async def _next_chunk(chunks: AsyncIterator[bytes]) -> bytes | None:
    """Bridge ``anext`` to a task-compatible coroutine with normal termination."""
    try:
        return await anext(chunks)
    except StopAsyncIteration:
        return None


def _cancel_background(task: asyncio.Task[object]) -> None:
    """Cancel a pending source wait without letting its cleanup delay spool removal."""
    task.cancel()
    task.add_done_callback(_consume_task_result)


def _consume_task_result(task: asyncio.Task[object]) -> None:
    """Observe cancelled or failed abandoned source waits without reflected warnings."""
    with suppress(asyncio.CancelledError, Exception):
        task.result()
