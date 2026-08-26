from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from threading import Event, Thread
from typing import Any
from uuid import UUID

import pytest

from klove.domain.artifacts import ArtifactLimits
from klove.ftps import staging
from klove.ftps.staging import (
    FtpsStageReservation,
    FtpsStagingError,
    FtpsStagingStore,
    StagedArtifact,
)

STAGE_ID = "11111111-1111-4111-8111-111111111111"
SECOND_STAGE_ID = "44444444-4444-4444-8444-444444444444"
PRINTER_ID = "22222222-2222-4222-8222-222222222222"
OTHER_PRINTER_ID = "33333333-3333-4333-8333-333333333333"
CLIENT_PATH = "/observation.3mf"


def reservation(
    *,
    staging_id: str = STAGE_ID,
    printer_uuid: str = PRINTER_ID,
    client_path: str = CLIENT_PATH,
) -> FtpsStageReservation:
    return FtpsStageReservation(
        staging_id=staging_id,
        printer_uuid=printer_uuid,
        client_path=client_path,
        created_at_unix_ms=1_000,
        expires_at_unix_ms=2_000,
    )


def store(tmp_path: Path, *, limit: int = 512, capacity: int = 8) -> FtpsStagingStore:
    directory = tmp_path / "ftps-staging"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    value = FtpsStagingStore(
        directory,
        limits=ArtifactLimits(max_archive_compressed_bytes=limit),
        capacity=capacity,
    )
    value.initialize()
    return value


def test_streaming_stage_binds_complete_private_receipt_and_reader(tmp_path: Path) -> None:
    value = store(tmp_path)
    with value.begin(reservation()) as writer:
        writer.write(b"arc")
        writer.write(b"hive")
        artifact = writer.commit()

    assert artifact == StagedArtifact(
        reservation=reservation(),
        archive_size_bytes=7,
        archive_sha256="sha256:" + hashlib.sha256(b"archive").hexdigest(),
        consumed=False,
    )
    assert value.inspect(reservation()) == artifact
    assert list(value.read_chunks(reservation(), chunk_bytes=3)) == [b"arc", b"hiv", b"e"]
    directory = tmp_path / "ftps-staging"
    assert {path.name for path in directory.iterdir()} == {
        f"{STAGE_ID}.source",
        f"{STAGE_ID}.receipt",
    }
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in directory.iterdir())
    document = json.loads((directory / f"{STAGE_ID}.receipt").read_text())
    assert document == {
        "archive_sha256": artifact.archive_sha256,
        "archive_size_bytes": 7,
        "client_path": CLIENT_PATH,
        "consumed": False,
        "created_at_unix_ms": 1_000,
        "expires_at_unix_ms": 2_000,
        "printer_uuid": PRINTER_ID,
        "staging_id": STAGE_ID,
        "version": "2",
    }
    assert "source" not in document


def test_create_reservation_generates_uuid_and_validates_all_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = store(tmp_path)
    monkeypatch.setattr(staging, "uuid4", lambda: UUID(STAGE_ID))

    assert (
        value.create_reservation(
            printer_uuid=PRINTER_ID,
            client_path=CLIENT_PATH,
            created_at_unix_ms=1_000,
            expires_at_unix_ms=2_000,
        )
        == reservation()
    )


@pytest.mark.parametrize(
    ("staging_id", "printer_uuid"),
    [
        ("not-a-uuid", PRINTER_ID),
        ("11111111-1111-1111-8111-111111111111", PRINTER_ID),
        ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".upper(), PRINTER_ID),
        (STAGE_ID, "not-a-uuid"),
        (STAGE_ID, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb".upper()),
    ],
)
def test_reservation_requires_canonical_uuid4(staging_id: str, printer_uuid: str) -> None:
    with pytest.raises(FtpsStagingError):
        FtpsStageReservation(staging_id, printer_uuid, CLIENT_PATH, 1_000, 2_000)


@pytest.mark.parametrize("value", [None, 1, True])
def test_reservation_rejects_non_text_identifiers(value: object) -> None:
    with pytest.raises(FtpsStagingError):
        FtpsStageReservation(value, PRINTER_ID, CLIENT_PATH, 1_000, 2_000)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "path",
    [
        "observation.3mf",
        "/.hidden.3mf",
        "/nested/file.3mf",
        "/file.3MF",
        "/file.3mf ",
        "/file%2e3mf",
        "/café.3mf",
        "/a" + "x" * 247 + ".3mf",
        1,
    ],
)
def test_reservation_rejects_noncanonical_client_path(path: object) -> None:
    with pytest.raises(FtpsStagingError):
        FtpsStageReservation(STAGE_ID, PRINTER_ID, path, 1_000, 2_000)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("created", "expires"),
    [(-1, 2), (1, 1), (2, 1), (True, 2), (1, True), (1, 9_223_372_036_854_775_808)],
)
def test_reservation_rejects_invalid_lifetime(created: object, expires: object) -> None:
    with pytest.raises(FtpsStagingError):
        FtpsStageReservation(STAGE_ID, PRINTER_ID, CLIENT_PATH, created, expires)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("reservation_value", "size", "digest", "consumed"),
    [
        (object(), 0, "sha256:" + "0" * 64, False),
        (reservation(), -1, "sha256:" + "0" * 64, False),
        (reservation(), True, "sha256:" + "0" * 64, False),
        (reservation(), 0, "sha256:" + "0" * 63, False),
        (reservation(), 0, "sha256:" + "g" * 64, False),
        (reservation(), 0, 1, False),
        (reservation(), 0, "sha256:" + "0" * 64, 1),
    ],
)
def test_receipt_rejects_invalid_fields(
    reservation_value: object, size: object, digest: object, consumed: object
) -> None:
    with pytest.raises(FtpsStagingError):
        StagedArtifact(
            reservation=reservation_value,  # type: ignore[arg-type]
            archive_size_bytes=size,  # type: ignore[arg-type]
            archive_sha256=digest,  # type: ignore[arg-type]
            consumed=consumed,  # type: ignore[arg-type]
        )


def test_context_exit_abort_and_explicit_abort_are_exact_and_idempotent(tmp_path: Path) -> None:
    value = store(tmp_path)
    with value.begin(reservation()) as writer:
        writer.write(b"partial")
    assert not list((tmp_path / "ftps-staging").iterdir())
    writer.abort()
    with pytest.raises(FtpsStagingError):
        writer.write(b"late")
    with pytest.raises(FtpsStagingError):
        writer.commit()
    with pytest.raises(FtpsStagingError):
        writer.__enter__()

    with pytest.raises(KeyboardInterrupt), value.begin(reservation()) as cancelled:
        cancelled.write(b"partial")
        raise KeyboardInterrupt
    assert not list((tmp_path / "ftps-staging").iterdir())


def test_stage_helper_and_empty_write(tmp_path: Path) -> None:
    value = store(tmp_path)
    artifact = value.stage(reservation(), [b"", b"archive"])
    assert artifact.archive_size_bytes == 7


@pytest.mark.parametrize("chunks", [["not-bytes"], [b"x" * 513]])
def test_writer_rejects_type_or_oversize_and_removes_partial(
    tmp_path: Path, chunks: list[object]
) -> None:
    value = store(tmp_path)
    with pytest.raises(FtpsStagingError):
        value.stage(reservation(), chunks)  # type: ignore[arg-type]
    assert not list((tmp_path / "ftps-staging").iterdir())


def test_write_failure_and_cancel_remove_only_receiving_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = store(tmp_path)
    writer = value.begin(reservation())
    unrelated = tmp_path / "ftps-staging" / "unrelated"
    unrelated.write_bytes(b"keep")
    unrelated.chmod(0o600)
    monkeypatch.setattr(os, "write", lambda _fd, _data: 0)
    with pytest.raises(FtpsStagingError):
        writer.write(b"archive")
    assert {path.name for path in unrelated.parent.iterdir()} == {"unrelated"}
    unrelated.unlink()

    other = reservation(printer_uuid=OTHER_PRINTER_ID)
    monkeypatch.undo()
    writer = value.begin(other)
    with pytest.raises(KeyboardInterrupt):
        try:
            raise KeyboardInterrupt
        finally:
            writer.abort()
    assert {path.name for path in unrelated.parent.iterdir()} == set()


@pytest.mark.parametrize("failure", [OSError(), KeyboardInterrupt()])
def test_writer_preserves_exception_class_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    value = store(tmp_path)
    writer = value.begin(reservation())

    def fail_write(_descriptor: int, _data: bytes) -> None:
        raise failure

    monkeypatch.setattr(staging, "_write_all", fail_write)
    expected = FtpsStagingError if isinstance(failure, Exception) else KeyboardInterrupt
    with pytest.raises(expected):
        writer.write(b"archive")
    assert not list((tmp_path / "ftps-staging").iterdir())


@pytest.mark.parametrize("failure", [OSError(), KeyboardInterrupt()])
def test_commit_preserves_exception_class_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    value = store(tmp_path)
    writer = value.begin(reservation())
    writer.write(b"archive")

    def fail_commit(_reservation: FtpsStageReservation, _artifact: StagedArtifact) -> None:
        raise failure

    monkeypatch.setattr(value, "_commit", fail_commit)
    expected = FtpsStagingError if isinstance(failure, Exception) else KeyboardInterrupt
    with pytest.raises(expected):
        writer.commit()
    assert not list((tmp_path / "ftps-staging").iterdir())


def test_begin_rejects_conflict_unsafe_directory_and_nonreservation(tmp_path: Path) -> None:
    value = store(tmp_path)
    writer = value.begin(reservation())
    with pytest.raises(FtpsStagingError):
        value.begin(reservation())
    writer.abort()
    with pytest.raises(FtpsStagingError):
        value.begin(object())  # type: ignore[arg-type]
    (tmp_path / "ftps-staging").chmod(0o755)
    with pytest.raises(FtpsStagingError):
        value.initialize()


def test_concurrent_begin_has_one_owner(tmp_path: Path) -> None:
    value = store(tmp_path)
    first = value.begin(reservation())
    with pytest.raises(FtpsStagingError):
        value.begin(reservation())
    first.abort()


def test_begin_creation_failure_removes_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = store(tmp_path)
    real_open = os.open

    def fail_receiving(path: Any, flags: int, mode: int = 0o600) -> int:
        if str(path).endswith(".receiving"):
            raise OSError
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", fail_receiving)
    with pytest.raises(FtpsStagingError):
        value.begin(reservation())
    assert not list((tmp_path / "ftps-staging").iterdir())


def test_begin_preserves_staging_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = store(tmp_path)
    monkeypatch.setattr(
        staging,
        "_write_document",
        lambda *_args: (_ for _ in ()).throw(FtpsStagingError()),
    )
    with pytest.raises(FtpsStagingError):
        value.begin(reservation())


def test_commit_failure_removes_all_exact_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = store(tmp_path)
    writer = value.begin(reservation())
    writer.write(b"archive")
    monkeypatch.setattr(staging, "_write_receipt", lambda *_args: (_ for _ in ()).throw(OSError()))
    with pytest.raises(FtpsStagingError):
        writer.commit()
    assert not list((tmp_path / "ftps-staging").iterdir())


@pytest.mark.parametrize("fault", ["file", "directory"])
def test_fsync_failure_removes_all_exact_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    value = store(tmp_path)
    writer = value.begin(reservation())
    writer.write(b"archive")
    if fault == "file":
        real_fsync = os.fsync
        calls = 0

        def fail_first(descriptor: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError
            real_fsync(descriptor)

        monkeypatch.setattr(os, "fsync", fail_first)
    else:
        real_directory_sync = staging.fsync_directory  # type: ignore[attr-defined]
        calls = 0

        def fail_first_directory(path: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError
            real_directory_sync(path)

        monkeypatch.setattr(staging, "fsync_directory", fail_first_directory)
    with pytest.raises(FtpsStagingError):
        writer.commit()
    assert not list((tmp_path / "ftps-staging").iterdir())


def test_commit_rejects_mutated_reservation_and_linked_receiving(tmp_path: Path) -> None:
    value = store(tmp_path)
    writer = value.begin(reservation())
    reservation_path = tmp_path / "ftps-staging" / f"{STAGE_ID}.reservation"
    reservation_path.write_text(
        json.dumps(staging._reservation_document(reservation(printer_uuid=OTHER_PRINTER_ID)))
    )
    reservation_path.chmod(0o600)
    with pytest.raises(FtpsStagingError):
        writer.commit()
    assert not list((tmp_path / "ftps-staging").iterdir())

    writer = value.begin(reservation())
    reservation_path.write_text('{"version":"wrong"}')
    reservation_path.chmod(0o600)
    with pytest.raises(FtpsStagingError):
        writer.commit()
    assert not list((tmp_path / "ftps-staging").iterdir())

    writer = value.begin(reservation())
    receiving = tmp_path / "ftps-staging" / f"{STAGE_ID}.receiving"
    os.link(receiving, tmp_path / "ftps-staging" / "extra-link")
    with pytest.raises(FtpsStagingError):
        writer.commit()
    (tmp_path / "ftps-staging" / "extra-link").unlink()


def test_inspect_rejects_other_principal_missing_mutated_and_linked_state(tmp_path: Path) -> None:
    value = store(tmp_path)
    value.stage(reservation(), [b"archive"])
    directory = tmp_path / "ftps-staging"
    source = directory / f"{STAGE_ID}.source"
    receipt = directory / f"{STAGE_ID}.receipt"

    with pytest.raises(FtpsStagingError):
        value.inspect(reservation(printer_uuid=OTHER_PRINTER_ID))
    source.write_bytes(b"short")
    source.chmod(0o600)
    with pytest.raises(FtpsStagingError):
        value.inspect(reservation())
    source.write_bytes(b"changed")
    source.chmod(0o600)
    with pytest.raises(FtpsStagingError):
        value.inspect(reservation())
    source.write_bytes(b"archive")
    source.chmod(0o600)
    os.link(receipt, directory / "receipt-link")
    with pytest.raises(FtpsStagingError):
        value.inspect(reservation())
    (directory / "receipt-link").unlink()
    receipt.chmod(0o644)
    with pytest.raises(FtpsStagingError):
        value.inspect(reservation())
    with pytest.raises(FtpsStagingError):
        value.inspect(object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"[]",
        json.dumps({"version": "wrong"}).encode(),
        json.dumps(
            {
                "version": "wrong",
                "staging_id": STAGE_ID,
                "printer_uuid": PRINTER_ID,
                "client_path": CLIENT_PATH,
                "created_at_unix_ms": 1_000,
                "expires_at_unix_ms": 2_000,
                "archive_size_bytes": 7,
                "archive_sha256": "sha256:" + "0" * 64,
                "consumed": False,
            }
        ).encode(),
        json.dumps(
            {
                "version": "2",
                "staging_id": STAGE_ID,
                "printer_uuid": PRINTER_ID,
                "client_path": CLIENT_PATH,
                "created_at_unix_ms": 1_000,
                "expires_at_unix_ms": 2_000,
                "archive_size_bytes": 7,
                "archive_sha256": "sha256:" + "0" * 64,
                "consumed": False,
                "extra": "no",
            }
        ).encode(),
    ],
)
def test_inspect_rejects_malformed_receipts(tmp_path: Path, payload: bytes) -> None:
    value = store(tmp_path)
    value.stage(reservation(), [b"archive"])
    receipt = tmp_path / "ftps-staging" / f"{STAGE_ID}.receipt"
    receipt.write_bytes(payload)
    receipt.chmod(0o600)
    with pytest.raises(FtpsStagingError):
        value.inspect(reservation())


@pytest.mark.parametrize("chunk_bytes", [0, 1024 * 1024 + 1, True])
def test_reader_rejects_invalid_bound(tmp_path: Path, chunk_bytes: object) -> None:
    value = store(tmp_path)
    value.stage(reservation(), [b"archive"])
    with pytest.raises(FtpsStagingError):
        list(value.read_chunks(reservation(), chunk_bytes=chunk_bytes))  # type: ignore[arg-type]


def test_consume_is_atomic_single_use_and_reader_denies_consumed(tmp_path: Path) -> None:
    value = store(tmp_path)
    value.stage(reservation(), [b"archive"])
    consumed = value.consume(reservation())
    assert consumed.consumed is True
    assert value.inspect(reservation()) == consumed
    with pytest.raises(FtpsStagingError):
        value.consume(reservation())
    with pytest.raises(FtpsStagingError):
        list(value.read_chunks(reservation()))


def test_consume_failure_preserves_unconsumed_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = store(tmp_path)
    value.stage(reservation(), [b"archive"])
    real_replace = os.replace

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(FtpsStagingError):
        value.consume(reservation())
    monkeypatch.setattr(os, "replace", real_replace)
    assert value.inspect(reservation()).consumed is False


def test_consume_preserves_staging_error_and_rechecks_under_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = store(tmp_path)
    artifact = value.stage(reservation(), [b"archive"])
    monkeypatch.setattr(
        staging,
        "_write_receipt",
        lambda *_args: (_ for _ in ()).throw(FtpsStagingError()),
    )
    with pytest.raises(FtpsStagingError):
        value.consume(reservation())
    monkeypatch.undo()

    calls = 0

    def changing_inspect(request: FtpsStageReservation) -> StagedArtifact:
        nonlocal calls
        calls += 1
        return (
            artifact
            if calls == 1
            else StagedArtifact(
                reservation=artifact.reservation,
                archive_size_bytes=artifact.archive_size_bytes,
                archive_sha256=artifact.archive_sha256,
                consumed=True,
            )
        )

    monkeypatch.setattr(value, "inspect", changing_inspect)
    with pytest.raises(FtpsStagingError):
        value.consume(reservation())
    assert not (tmp_path / "ftps-staging" / f"{STAGE_ID}.updating").exists()


def test_concurrent_consume_has_one_winner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = store(tmp_path)
    value.stage(reservation(), [b"archive"])
    entered = Event()
    release = Event()
    real_replace = os.replace

    def blocking_replace(source: Path, target: Path) -> None:
        if str(source).endswith(".updating"):
            entered.set()
            assert release.wait(2)
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", blocking_replace)
    results: list[StagedArtifact] = []
    failures: list[BaseException] = []

    def consume() -> None:
        try:
            results.append(value.consume(reservation()))
        except BaseException as exc:
            failures.append(exc)

    thread = Thread(target=consume)
    thread.start()
    assert entered.wait(2)
    with pytest.raises(FtpsStagingError):
        value.consume(reservation())
    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert len(results) == 1 and not failures


def test_only_consumed_exact_stage_can_be_removed_and_restart_resumes_marker(
    tmp_path: Path,
) -> None:
    value = store(tmp_path)
    value.stage(reservation(), [b"archive"])
    with pytest.raises(FtpsStagingError):
        value.remove_consumed(reservation())
    value.consume(reservation())
    receipt = tmp_path / "ftps-staging" / f"{STAGE_ID}.receipt"
    removing = tmp_path / "ftps-staging" / f"{STAGE_ID}.removing"
    os.replace(receipt, removing)
    value.remove_consumed(reservation())
    assert not list((tmp_path / "ftps-staging").iterdir())
    with pytest.raises(FtpsStagingError):
        value.remove_consumed(reservation())


def test_normal_consumed_removal_and_invalid_request(tmp_path: Path) -> None:
    value = store(tmp_path)
    value.stage(reservation(), [b"archive"])
    value.consume(reservation())
    value.remove_consumed(reservation())
    assert not list((tmp_path / "ftps-staging").iterdir())
    with pytest.raises(FtpsStagingError):
        value.remove_consumed(object())  # type: ignore[arg-type]


def test_removal_rejects_cross_principal_or_invalid_marker(tmp_path: Path) -> None:
    value = store(tmp_path)
    value.stage(reservation(), [b"archive"])
    value.consume(reservation())
    receipt = tmp_path / "ftps-staging" / f"{STAGE_ID}.receipt"
    removing = tmp_path / "ftps-staging" / f"{STAGE_ID}.removing"
    os.replace(receipt, removing)
    with pytest.raises(FtpsStagingError):
        value.remove_consumed(reservation(printer_uuid=OTHER_PRINTER_ID))
    unconsumed = StagedArtifact(
        reservation=reservation(),
        archive_size_bytes=7,
        archive_sha256="sha256:" + hashlib.sha256(b"archive").hexdigest(),
    )
    removing.unlink()
    staging._write_receipt(removing, unconsumed)
    with pytest.raises(FtpsStagingError):
        value.remove_consumed(reservation())
    removing.chmod(0o644)
    with pytest.raises(FtpsStagingError):
        value.remove_consumed(reservation())
    removing.chmod(0o600)
    removing.write_text("{}")
    removing.chmod(0o600)
    with pytest.raises(FtpsStagingError):
        value.remove_consumed(reservation())


def test_abort_rejects_link_replacement(tmp_path: Path) -> None:
    value = store(tmp_path)
    writer = value.begin(reservation())
    receiving = tmp_path / "ftps-staging" / f"{STAGE_ID}.receiving"
    linked = tmp_path / "ftps-staging" / "linked"
    os.link(receiving, linked)
    with pytest.raises(FtpsStagingError):
        writer.abort()
    linked.unlink()
    receiving.unlink()
    (tmp_path / "ftps-staging" / f"{STAGE_ID}.reservation").unlink()

    writer = value.begin(reservation())
    receiving = tmp_path / "ftps-staging" / f"{STAGE_ID}.receiving"
    receiving.chmod(0o644)
    with pytest.raises(FtpsStagingError):
        writer.abort()


def test_document_writer_closes_failed_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "write", lambda _fd, _data: 0)
    with pytest.raises(FtpsStagingError):
        staging._write_document(tmp_path / "receipt", {"version": "2"})


@pytest.mark.parametrize("capacity", [0, 100_001, True])
def test_store_rejects_invalid_capacity(tmp_path: Path, capacity: object) -> None:
    with pytest.raises(FtpsStagingError):
        FtpsStagingStore(
            tmp_path,
            limits=ArtifactLimits(max_archive_compressed_bytes=512),
            capacity=capacity,  # type: ignore[arg-type]
        )


def test_capacity_counts_complete_and_receiving_stages_without_eviction(tmp_path: Path) -> None:
    value = store(tmp_path, capacity=1)
    artifact = value.stage(reservation(), [b"archive"])
    with pytest.raises(FtpsStagingError):
        value.begin(reservation(staging_id=SECOND_STAGE_ID))
    assert value.inspect(reservation()) == artifact

    value.remove_consumed(value.consume(reservation()).reservation)
    first = value.begin(reservation())
    with pytest.raises(FtpsStagingError):
        value.begin(reservation(staging_id=SECOND_STAGE_ID))
    first.abort()


@pytest.mark.parametrize(
    "name",
    [
        "unknown",
        "not-a-uuid.source",
        f"{STAGE_ID}.unknown",
        f"{STAGE_ID}.source",
    ],
)
def test_capacity_scan_fails_closed_on_unknown_or_malformed_entry(
    tmp_path: Path, name: str
) -> None:
    value = store(tmp_path)
    entry = tmp_path / "ftps-staging" / name
    entry.write_bytes(b"unknown")
    entry.chmod(0o600)
    with pytest.raises(FtpsStagingError):
        value.begin(reservation())
    assert entry.read_bytes() == b"unknown"


def test_reader_keeps_verified_descriptor_when_source_name_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = store(tmp_path)
    value.stage(reservation(), [b"archive"])
    source = tmp_path / "ftps-staging" / f"{STAGE_ID}.source"
    replacement = tmp_path / "ftps-staging" / "replacement"
    real_open_verified = staging._open_verified_source

    def replace_after_open(path: Path, artifact: StagedArtifact) -> int:
        descriptor = real_open_verified(path, artifact)
        replacement.write_bytes(b"changed")
        replacement.chmod(0o600)
        os.replace(replacement, source)
        return descriptor

    monkeypatch.setattr(staging, "_open_verified_source", replace_after_open)
    assert b"".join(value.read_chunks(reservation(), chunk_bytes=2)) == b"archive"
    assert source.read_bytes() == b"changed"


def test_reader_wraps_read_failure_and_closes_verified_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = store(tmp_path)
    value.stage(reservation(), [b"archive"])
    real_open_verified = staging._open_verified_source
    descriptor = -1

    def fail_after_verification(path: Path, artifact: StagedArtifact) -> int:
        nonlocal descriptor
        descriptor = real_open_verified(path, artifact)
        monkeypatch.setattr(os, "read", lambda _fd, _size: (_ for _ in ()).throw(OSError()))
        return descriptor

    monkeypatch.setattr(staging, "_open_verified_source", fail_after_verification)
    with pytest.raises(FtpsStagingError):
        list(value.read_chunks(reservation()))
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_capacity_scan_wraps_private_file_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = store(tmp_path)
    entry = tmp_path / "ftps-staging" / f"{STAGE_ID}.source"
    entry.write_bytes(b"unknown")
    entry.chmod(0o600)
    monkeypatch.setattr(
        staging,
        "_require_unlinked_private_file",
        lambda _path: (_ for _ in ()).throw(OSError()),
    )
    with pytest.raises(FtpsStagingError):
        value.begin(reservation())


def test_verified_source_open_failure_does_not_close_invalid_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = store(tmp_path)
    artifact = value.stage(reservation(), [b"archive"])
    source = tmp_path / "ftps-staging" / f"{STAGE_ID}.source"
    closed: list[int] = []
    monkeypatch.setattr(os, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(os, "close", closed.append)
    with pytest.raises(OSError):
        staging._open_verified_source(source, artifact)
    assert closed == []


def test_reconcile_discards_only_an_exact_verified_incomplete_transfer(tmp_path: Path) -> None:
    value = store(tmp_path)
    directory = tmp_path / "ftps-staging"
    reservation_path = directory / f"{STAGE_ID}.reservation"
    receiving = directory / f"{STAGE_ID}.receiving"
    staging._write_document(reservation_path, staging._reservation_document(reservation()))
    receiving.write_bytes(b"partial")
    receiving.chmod(0o600)

    value.reconcile()

    assert not list(directory.iterdir())


def test_reconcile_restarts_interrupted_incomplete_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = store(tmp_path)
    directory = tmp_path / "ftps-staging"
    reservation_path = directory / f"{STAGE_ID}.reservation"
    receiving = directory / f"{STAGE_ID}.receiving"
    staging._write_document(reservation_path, staging._reservation_document(reservation()))
    receiving.write_bytes(b"partial")
    receiving.chmod(0o600)
    real_unlink = Path.unlink

    def fail_reservation_removal(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == reservation_path:
            raise OSError
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_reservation_removal)
    with pytest.raises(FtpsStagingError):
        value.reconcile()
    assert reservation_path.exists()
    assert not receiving.exists()

    monkeypatch.undo()
    value.reconcile()
    assert not list(directory.iterdir())


def test_reconcile_restarts_after_partial_recovery_receipt_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = store(tmp_path)
    directory = tmp_path / "ftps-staging"
    reservation_path = directory / f"{STAGE_ID}.reservation"
    source = directory / f"{STAGE_ID}.source"
    receipt = directory / f"{STAGE_ID}.receipt"
    staging._write_document(reservation_path, staging._reservation_document(reservation()))
    source.write_bytes(b"archive")
    source.chmod(0o600)

    def write_partial(path: Path, artifact: StagedArtifact) -> None:
        payload = json.dumps(
            staging._receipt_document(artifact),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        path.write_bytes(payload[: len(payload) // 2])
        path.chmod(0o600)
        raise OSError

    monkeypatch.setattr(staging, "_write_receipt", write_partial)
    with pytest.raises(FtpsStagingError):
        value.reconcile()
    assert reservation_path.exists()
    assert source.exists()
    assert receipt.exists()

    monkeypatch.undo()
    with pytest.raises(FtpsStagingError):
        value.reconcile()
    assert reservation_path.exists()
    assert source.exists()
    assert not receipt.exists()

    value.reconcile()
    assert value.inspect(reservation()).archive_size_bytes == 7


def test_reconcile_retains_nonpartial_malformed_recovery_receipt(tmp_path: Path) -> None:
    value = store(tmp_path)
    directory = tmp_path / "ftps-staging"
    reservation_path = directory / f"{STAGE_ID}.reservation"
    source = directory / f"{STAGE_ID}.source"
    receipt = directory / f"{STAGE_ID}.receipt"
    staging._write_document(reservation_path, staging._reservation_document(reservation()))
    source.write_bytes(b"archive")
    source.chmod(0o600)
    receipt.write_bytes(b"not-a-recovery-receipt" * 32)
    receipt.chmod(0o600)

    with pytest.raises(FtpsStagingError):
        value.reconcile()

    assert reservation_path.exists()
    assert source.exists()
    assert receipt.exists()


@pytest.mark.parametrize("fault", ["malformed", "cross_id", "permission", "link"])
def test_reconcile_rejects_unsafe_incomplete_transfer_without_removal(
    tmp_path: Path, fault: str
) -> None:
    value = store(tmp_path)
    directory = tmp_path / "ftps-staging"
    reservation_path = directory / f"{STAGE_ID}.reservation"
    receiving = directory / f"{STAGE_ID}.receiving"
    staging._write_document(reservation_path, staging._reservation_document(reservation()))
    receiving.write_bytes(b"partial")
    receiving.chmod(0o600)
    if fault == "malformed":
        reservation_path.write_text("{}")
        reservation_path.chmod(0o600)
    elif fault == "cross_id":
        reservation_path.unlink()
        staging._write_document(
            reservation_path,
            staging._reservation_document(reservation(staging_id=SECOND_STAGE_ID)),
        )
    elif fault == "permission":
        receiving.chmod(0o644)
    else:
        os.link(receiving, directory / "receiving-link")

    with pytest.raises(FtpsStagingError):
        value.reconcile()

    assert reservation_path.exists()
    assert receiving.exists()


def test_reconcile_recovers_post_link_commit_and_finalizes_receipt_write_restart(
    tmp_path: Path,
) -> None:
    value = store(tmp_path)
    directory = tmp_path / "ftps-staging"
    reservation_path = directory / f"{STAGE_ID}.reservation"
    source = directory / f"{STAGE_ID}.source"
    staging._write_document(reservation_path, staging._reservation_document(reservation()))
    source.write_bytes(b"archive")
    source.chmod(0o600)

    value.reconcile()

    artifact = value.inspect(reservation())
    assert artifact.archive_size_bytes == 7
    assert artifact.archive_sha256 == "sha256:" + hashlib.sha256(b"archive").hexdigest()
    assert {path.name for path in directory.iterdir()} == {
        f"{STAGE_ID}.source",
        f"{STAGE_ID}.receipt",
    }
    value.reconcile()
    assert value.inspect(reservation()) == artifact

    staging._write_document(reservation_path, staging._reservation_document(reservation()))
    value.reconcile()
    assert value.inspect(reservation()) == artifact
    assert not reservation_path.exists()


@pytest.mark.parametrize("fault", ["oversize", "linked", "receipt_write"])
def test_reconcile_retains_unresolved_post_link_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    value = store(tmp_path, limit=7)
    directory = tmp_path / "ftps-staging"
    reservation_path = directory / f"{STAGE_ID}.reservation"
    source = directory / f"{STAGE_ID}.source"
    staging._write_document(reservation_path, staging._reservation_document(reservation()))
    source.write_bytes(b"archive" if fault != "oversize" else b"oversized")
    source.chmod(0o600)
    if fault == "linked":
        os.link(source, directory / "source-link")
    if fault == "receipt_write":
        monkeypatch.setattr(
            staging,
            "_write_receipt",
            lambda *_args: (_ for _ in ()).throw(OSError()),
        )

    with pytest.raises(FtpsStagingError):
        value.reconcile()

    assert reservation_path.exists()
    assert source.exists()
    assert not (directory / f"{STAGE_ID}.receipt").exists()


def test_reconcile_finishes_exact_consumption_and_removal_transitions(tmp_path: Path) -> None:
    value = store(tmp_path)
    artifact = value.stage(reservation(), [b"archive"])
    directory = tmp_path / "ftps-staging"
    updating = directory / f"{STAGE_ID}.updating"
    staging._write_receipt(
        updating,
        StagedArtifact(
            reservation=artifact.reservation,
            archive_size_bytes=artifact.archive_size_bytes,
            archive_sha256=artifact.archive_sha256,
            consumed=True,
        ),
    )

    value.reconcile()
    assert value.inspect(reservation()).consumed is True

    receipt = directory / f"{STAGE_ID}.receipt"
    removing = directory / f"{STAGE_ID}.removing"
    os.replace(receipt, removing)
    value.reconcile()
    assert not list(directory.iterdir())

    artifact = value.stage(reservation(), [b"archive"])
    consumed = value.consume(reservation())
    os.replace(directory / f"{STAGE_ID}.receipt", removing)
    (directory / f"{STAGE_ID}.source").unlink()
    value.reconcile()
    assert not list(directory.iterdir())
    assert artifact.consumed is False
    assert consumed.consumed is True


@pytest.mark.parametrize("fault", ["unconsumed_update", "cross_id_update", "unconsumed_remove"])
def test_reconcile_retains_ambiguous_transition_evidence(tmp_path: Path, fault: str) -> None:
    value = store(tmp_path)
    artifact = value.stage(reservation(), [b"archive"])
    directory = tmp_path / "ftps-staging"
    if fault == "unconsumed_remove":
        os.replace(directory / f"{STAGE_ID}.receipt", directory / f"{STAGE_ID}.removing")
    else:
        updating = directory / f"{STAGE_ID}.updating"
        update = (
            artifact
            if fault == "unconsumed_update"
            else StagedArtifact(
                reservation=reservation(staging_id=SECOND_STAGE_ID),
                archive_size_bytes=artifact.archive_size_bytes,
                archive_sha256=artifact.archive_sha256,
                consumed=True,
            )
        )
        staging._write_receipt(updating, update)

    with pytest.raises(FtpsStagingError):
        value.reconcile()

    assert list(directory.iterdir())


def test_reconcile_rejects_cross_reservation_after_receipt_write(tmp_path: Path) -> None:
    value = store(tmp_path)
    artifact = value.stage(reservation(), [b"archive"])
    directory = tmp_path / "ftps-staging"
    reservation_path = directory / f"{STAGE_ID}.reservation"
    staging._write_document(
        reservation_path,
        staging._reservation_document(reservation(printer_uuid=OTHER_PRINTER_ID)),
    )

    with pytest.raises(FtpsStagingError):
        value.reconcile()

    assert value.inspect(reservation()) == artifact
    assert reservation_path.exists()


@pytest.mark.parametrize("state", ["source_only", "unconsumed_removing_only"])
def test_reconcile_rejects_unknown_or_unconsumed_removing_state(tmp_path: Path, state: str) -> None:
    value = store(tmp_path)
    value.stage(reservation(), [b"archive"])
    directory = tmp_path / "ftps-staging"
    if state == "source_only":
        (directory / f"{STAGE_ID}.receipt").unlink()
    else:
        os.replace(directory / f"{STAGE_ID}.receipt", directory / f"{STAGE_ID}.removing")
        (directory / f"{STAGE_ID}.source").unlink()

    with pytest.raises(FtpsStagingError):
        value.reconcile()

    assert list(directory.iterdir())


def test_reconcile_validates_all_entries_before_any_recovery(tmp_path: Path) -> None:
    value = store(tmp_path)
    directory = tmp_path / "ftps-staging"
    staging._write_document(
        directory / f"{STAGE_ID}.reservation", staging._reservation_document(reservation())
    )
    receiving = directory / f"{STAGE_ID}.receiving"
    receiving.write_bytes(b"partial")
    receiving.chmod(0o600)
    unknown = directory / "unknown"
    unknown.write_bytes(b"evidence")
    unknown.chmod(0o600)

    with pytest.raises(FtpsStagingError):
        value.reconcile()

    assert receiving.exists()
    assert unknown.read_bytes() == b"evidence"


def test_reconcile_is_serialized_with_capacity_admission(tmp_path: Path) -> None:
    value = store(tmp_path)
    entered = Event()
    release = Event()
    failures: list[BaseException] = []
    writers: list[staging.FtpsStagingWriter] = []
    original_actions = value._reconciliation_actions

    def blocking_actions() -> list[tuple[str, staging._StagePaths, StagedArtifact | None]]:
        entered.set()
        assert release.wait(2)
        return original_actions()

    value._reconciliation_actions = blocking_actions  # type: ignore[method-assign]

    def reconcile() -> None:
        try:
            value.reconcile()
        except BaseException as exc:
            failures.append(exc)

    thread = Thread(target=reconcile)
    thread.start()
    assert entered.wait(2)
    admission_done = Event()

    def begin() -> None:
        try:
            writers.append(value.begin(reservation()))
        except BaseException as exc:
            failures.append(exc)
        finally:
            admission_done.set()

    admission = Thread(target=begin)
    admission.start()
    assert not admission_done.wait(0.1)
    release.set()
    thread.join(2)
    admission.join(2)
    assert not thread.is_alive()
    assert not admission.is_alive()
    assert not failures
    assert len(writers) == 1
    writers[0].abort()
