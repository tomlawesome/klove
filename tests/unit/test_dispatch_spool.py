from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from klove.domain.artifacts import ArtifactLimits, PlateSelection, target_for_safety_profile
from klove.domain.dispatch import DispatchRequest
from klove.persistence import dispatch_spool
from klove.persistence.dispatch_spool import DispatchSpool, DispatchSpoolError

from ..start_helpers import OPERATION_ID, safety_profile


def request(data: bytes = b"archive") -> DispatchRequest:
    target = target_for_safety_profile(safety_profile())
    return DispatchRequest(
        operation_id=OPERATION_ID,
        idempotency_key="33333333-3333-4333-8333-333333333333",
        printer_uuid=target.printer_uuid,
        slicer_profile_id=target.slicer_profile_id,
        safety_profile_generation=target.safety_profile_generation,
        safety_profile_fingerprint=target.safety_profile_fingerprint,
        selected_plate=PlateSelection(plate_id="1", archive_path="Metadata/plate_1.gcode"),
        archive_sha256="sha256:" + hashlib.sha256(data).hexdigest(),
        archive_size_bytes=len(data),
    )


def spool(tmp_path: Path, *, limit: int = 512) -> DispatchSpool:
    directory = tmp_path / "spool"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    value = DispatchSpool(
        directory,
        limits=ArtifactLimits(max_archive_compressed_bytes=limit),
    )
    value.initialize()
    return value


def test_spool_retains_exact_bytes_once_and_deletes_only_its_operation(tmp_path: Path) -> None:
    value = spool(tmp_path)
    submitted = request()

    value.write(submitted, b"archive")
    value.write(submitted, b"archive")

    path = tmp_path / "spool" / f"{OPERATION_ID}.gcode.3mf"
    assert path.stat().st_mode & 0o777 == 0o600
    assert value.exists(submitted) is True
    assert value.read(submitted) == b"archive"
    value.remove(submitted)
    value.remove(submitted)
    assert value.exists(submitted) is False


def test_spool_lists_only_private_canonical_operation_files(tmp_path: Path) -> None:
    value = spool(tmp_path)
    submitted = request()
    value.write(submitted, b"archive")

    assert value.operation_ids() == frozenset({OPERATION_ID})
    value.verify(submitted)

    unexpected = tmp_path / "spool" / "unexpected"
    unexpected.write_bytes(b"not-a-spool-entry")
    unexpected.chmod(0o600)
    with pytest.raises(DispatchSpoolError):
        value.operation_ids()


def test_spool_verification_and_listing_reject_mutated_or_noncanonical_entries(
    tmp_path: Path,
) -> None:
    value = spool(tmp_path)
    submitted = request()
    path = tmp_path / "spool" / f"{OPERATION_ID}.gcode.3mf"
    value.write(submitted, b"archive")

    path.write_bytes(b"short")
    path.chmod(0o600)
    with pytest.raises(DispatchSpoolError):
        value.verify(submitted)
    path.write_bytes(b"changed")
    path.chmod(0o600)
    with pytest.raises(DispatchSpoolError):
        value.verify(submitted)

    path.unlink()
    for operation_id in (
        "not-a-uuid",
        "11111111-1111-1111-8111-111111111111",
        "ABCDEFAB-CDEF-4ABC-8DEF-ABCDEFABCDEF",
    ):
        unexpected = tmp_path / "spool" / f"{operation_id}.gcode.3mf"
        unexpected.write_bytes(b"archive")
        unexpected.chmod(0o600)
        with pytest.raises(DispatchSpoolError):
            value.operation_ids()
        unexpected.unlink()

    (tmp_path / "spool").chmod(0o755)
    with pytest.raises(DispatchSpoolError):
        value.verify(submitted)
    with pytest.raises(DispatchSpoolError):
        value.operation_ids()


def test_spool_rejects_short_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = spool(tmp_path)
    submitted = request()
    value.write(submitted, b"archive")

    value.remove(submitted)
    monkeypatch.setattr(os, "write", lambda _fd, _data: 0)

    with pytest.raises(DispatchSpoolError):
        value.write(submitted, b"archive")

    assert value.exists(submitted) is False


def test_spool_closes_an_aborted_descriptor_before_its_finally_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = spool(tmp_path)
    submitted = request()

    def failed_write(_descriptor: int, _data: object) -> int:
        raise OSError

    monkeypatch.setattr(os, "write", failed_write)
    with pytest.raises(DispatchSpoolError):
        value.write(submitted, b"archive")

    assert value.exists(submitted) is False


@pytest.mark.parametrize("body", [b"different", b"x" * 513])
def test_spool_rejects_mismatched_or_oversized_source_before_writing(
    tmp_path: Path, body: bytes
) -> None:
    value = spool(tmp_path)

    with pytest.raises(DispatchSpoolError):
        value.write(request(), body)

    assert value.exists(request()) is False


def test_spool_rejects_mutated_missing_public_or_linked_source(tmp_path: Path) -> None:
    value = spool(tmp_path)
    submitted = request()
    path = tmp_path / "spool" / f"{OPERATION_ID}.gcode.3mf"

    with pytest.raises(DispatchSpoolError):
        value.read(submitted)
    value.write(submitted, b"archive")
    path.write_bytes(b"changed")
    with pytest.raises(DispatchSpoolError):
        value.read(submitted)
    path.unlink()
    path.symlink_to(tmp_path / "outside")
    with pytest.raises(DispatchSpoolError):
        value.remove(submitted)
    path.unlink()
    path.write_bytes(b"archive")
    path.chmod(0o644)
    with pytest.raises(DispatchSpoolError):
        value.read(submitted)


@pytest.mark.asyncio
async def test_stream_spool_discards_partial_source_when_iterator_fails(tmp_path: Path) -> None:
    value = spool(tmp_path)
    submitted = request()

    async def broken_source() -> AsyncIterator[bytes]:
        yield b"archive"
        raise RuntimeError("source read failed")

    with pytest.raises(DispatchSpoolError):
        await value.write_stream(submitted, broken_source(), timeout_seconds=1)

    assert value.exists(submitted) is False


@pytest.mark.asyncio
async def test_stream_spool_retains_one_valid_exact_source(tmp_path: Path) -> None:
    value = spool(tmp_path)
    submitted = request()

    async def source() -> AsyncIterator[bytes]:
        yield b"arc"
        yield b"hive"

    await value.write_stream(submitted, source(), timeout_seconds=1)

    assert value.exists(submitted) is True
    assert value.read(submitted) == b"archive"


@pytest.mark.asyncio
async def test_stream_spool_cancels_a_stalled_source_without_waiting_for_timeout(
    tmp_path: Path,
) -> None:
    value = spool(tmp_path)
    submitted = request()
    entered = asyncio.Event()
    cancel = asyncio.Event()

    async def stalled_source() -> AsyncIterator[bytes]:
        entered.set()
        await asyncio.Event().wait()
        yield b"archive"

    task = asyncio.create_task(
        value.write_stream(
            submitted,
            stalled_source(),
            timeout_seconds=60,
            cancel_event=cancel,
        )
    )
    await entered.wait()
    cancel.set()

    with pytest.raises(DispatchSpoolError):
        await asyncio.wait_for(task, timeout=0.5)

    assert value.exists(submitted) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("submitted", "chunks"),
    [
        (request(), ("not-bytes",)),
        (request(), (b"archive", b"extra")),
        (request(), (b"short",)),
        (request(b"xxxxxxx"), (b"archive",)),
    ],
)
async def test_stream_spool_rejects_invalid_chunk_type_size_and_digest(
    tmp_path: Path,
    submitted: DispatchRequest,
    chunks: tuple[object, ...],
) -> None:
    value = spool(tmp_path)

    async def source() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk  # type: ignore[misc]

    with pytest.raises(DispatchSpoolError):
        await value.write_stream(submitted, source(), timeout_seconds=1)

    assert value.exists(submitted) is False


@pytest.mark.asyncio
async def test_stream_spool_rejects_short_writes_and_already_cancelled_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = spool(tmp_path)
    submitted = request()

    async def source() -> AsyncIterator[bytes]:
        yield b"archive"

    monkeypatch.setattr(os, "write", lambda _fd, _data: 0)
    with pytest.raises(DispatchSpoolError):
        await value.write_stream(submitted, source(), timeout_seconds=1)
    cancel = asyncio.Event()
    cancel.set()
    with pytest.raises(DispatchSpoolError):
        await value.write_stream(submitted, source(), timeout_seconds=1, cancel_event=cancel)

    assert value.exists(submitted) is False


@pytest.mark.asyncio
async def test_stream_spool_exercises_cancel_races_and_precreation_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = spool(tmp_path)
    submitted = request()
    cancel = asyncio.Event()

    async def cancellation_race() -> AsyncIterator[bytes]:
        cancel.set()
        yield b"archive"

    with pytest.raises(DispatchSpoolError):
        await value.write_stream(
            submitted,
            cancellation_race(),
            timeout_seconds=1,
            cancel_event=cancel,
        )
    cancel.clear()

    class FailingChunks:
        def __aiter__(self) -> FailingChunks:
            return self

        async def __anext__(self) -> bytes:
            raise RuntimeError

    with pytest.raises(DispatchSpoolError):
        await value.write_stream(
            submitted,
            FailingChunks(),
            timeout_seconds=1,
            cancel_event=cancel,
        )
    (tmp_path / "spool").chmod(0o755)
    with pytest.raises(DispatchSpoolError):
        await value.write_stream(submitted, cancellation_race(), timeout_seconds=1)

    with pytest.raises(RuntimeError):
        await dispatch_spool._next_chunk_or_cancel(aiter(FailingChunks()), asyncio.Event())

    async def failed_wait(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError

    monkeypatch.setattr(asyncio, "wait", failed_wait)
    with pytest.raises(RuntimeError):
        await dispatch_spool._next_chunk_or_cancel(aiter(FailingChunks()), asyncio.Event())


def test_spool_checks_retained_size_limits_hash_and_explicit_incomplete_cleanup(
    tmp_path: Path,
) -> None:
    value = spool(tmp_path)
    submitted = request()
    path = tmp_path / "spool" / f"{OPERATION_ID}.gcode.3mf"
    path.write_bytes(b"short")
    path.chmod(0o600)

    with pytest.raises(DispatchSpoolError):
        value.read(submitted)
    path.write_bytes(b"x" * 513)
    path.chmod(0o600)
    too_large = request(b"x" * 513)

    with pytest.raises(DispatchSpoolError):
        value.read(too_large)
    value.discard_incomplete(too_large)
    assert value.exists(too_large) is False
    with pytest.raises(DispatchSpoolError):
        value._verify_bytes(request(b"x" * 513), b"x" * 513)
    with pytest.raises(DispatchSpoolError):
        value._verify_bytes(request(b"xxxxxxx"), b"archive")


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner-only filesystem contract")
def test_spool_requires_private_existing_directory(tmp_path: Path) -> None:
    directory = tmp_path / "public"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    value = DispatchSpool(directory, limits=ArtifactLimits())

    with pytest.raises(DispatchSpoolError):
        value.initialize()
