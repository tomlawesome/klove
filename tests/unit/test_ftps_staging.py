from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

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
PRINTER_ID = "22222222-2222-4222-8222-222222222222"
OTHER_PRINTER_ID = "33333333-3333-4333-8333-333333333333"


def reservation(*, printer_uuid: str = PRINTER_ID) -> FtpsStageReservation:
    return FtpsStageReservation(staging_id=STAGE_ID, printer_uuid=printer_uuid)


def store(tmp_path: Path, *, limit: int = 512) -> FtpsStagingStore:
    directory = tmp_path / "ftps-staging"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    value = FtpsStagingStore(
        directory,
        limits=ArtifactLimits(max_archive_compressed_bytes=limit),
    )
    value.initialize()
    return value


def test_staging_writes_bounded_private_source_and_exact_principal_receipt(tmp_path: Path) -> None:
    value = store(tmp_path)

    artifact = value.stage(reservation(), [b"arc", b"hive"])

    assert artifact == StagedArtifact(
        reservation=reservation(),
        archive_size_bytes=7,
        archive_sha256="sha256:" + hashlib.sha256(b"archive").hexdigest(),
    )
    assert value.inspect(reservation()) == artifact
    source = tmp_path / "ftps-staging" / f"{STAGE_ID}.source"
    receipt = tmp_path / "ftps-staging" / f"{STAGE_ID}.receipt"
    assert source.stat().st_mode & 0o777 == 0o600
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "ftps-staging" / f"{STAGE_ID}.receiving").exists()
    assert source.read_bytes() == b"archive"
    assert "source" not in receipt.read_text()


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
        FtpsStageReservation(staging_id=staging_id, printer_uuid=printer_uuid)


@pytest.mark.parametrize("value", [None, 1, True])
def test_reservation_rejects_non_text_identifiers(value: object) -> None:
    with pytest.raises(FtpsStagingError):
        FtpsStageReservation(staging_id=value, printer_uuid=PRINTER_ID)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("size", "digest"),
    [
        (-1, "sha256:" + "0" * 64),
        (True, "sha256:" + "0" * 64),
        (0, "sha256:" + "0" * 63),
        (0, "sha256:" + "g" * 64),
        (0, 1),
    ],
)
def test_receipt_rejects_invalid_local_fields(size: object, digest: object) -> None:
    with pytest.raises(FtpsStagingError):
        StagedArtifact(
            reservation=reservation(),
            archive_size_bytes=size,  # type: ignore[arg-type]
            archive_sha256=digest,  # type: ignore[arg-type]
        )


def test_staging_rejects_non_reservation_conflicts_and_unsafe_directory(tmp_path: Path) -> None:
    value = store(tmp_path)
    directory = tmp_path / "ftps-staging"
    (directory / f"{STAGE_ID}.receiving").write_bytes(b"")
    (directory / f"{STAGE_ID}.receiving").chmod(0o600)
    with pytest.raises(FtpsStagingError):
        value.stage(reservation(), [])
    (directory / f"{STAGE_ID}.receiving").unlink()

    with pytest.raises(FtpsStagingError):
        value.stage(object(), [])  # type: ignore[arg-type]
    directory.chmod(0o755)
    with pytest.raises(FtpsStagingError):
        value.initialize()


@pytest.mark.parametrize("chunks", [["not-bytes"], [b"x" * 513]])
def test_staging_rejects_invalid_or_oversized_input_and_removes_partial(
    tmp_path: Path, chunks: list[object]
) -> None:
    value = store(tmp_path)

    with pytest.raises(FtpsStagingError):
        value.stage(reservation(), chunks)  # type: ignore[arg-type]

    directory = tmp_path / "ftps-staging"
    assert not list(directory.iterdir())


def test_staging_rejects_write_failure_and_removes_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = store(tmp_path)
    monkeypatch.setattr(os, "write", lambda _fd, _data: 0)

    with pytest.raises(FtpsStagingError):
        value.stage(reservation(), [b"archive"])

    assert not list((tmp_path / "ftps-staging").iterdir())


def test_staging_cleans_source_after_receipt_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = store(tmp_path)

    def failed_receipt(_path: Path, _artifact: StagedArtifact) -> None:
        raise OSError

    monkeypatch.setattr(staging, "_write_receipt", failed_receipt)
    with pytest.raises(FtpsStagingError):
        value.stage(reservation(), [b"archive"])

    assert not list((tmp_path / "ftps-staging").iterdir())


def test_staging_cleans_receipt_after_final_sync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = store(tmp_path)
    real_fsync_directory = staging.fsync_directory  # type: ignore[attr-defined]
    calls = 0

    def fail_final_sync(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError
        real_fsync_directory(path)

    monkeypatch.setattr(staging, "fsync_directory", fail_final_sync)
    with pytest.raises(FtpsStagingError):
        value.stage(reservation(), [b"archive"])

    assert not list((tmp_path / "ftps-staging").iterdir())


def test_inspect_rejects_other_principal_missing_or_mutated_state(tmp_path: Path) -> None:
    value = store(tmp_path)
    artifact = value.stage(reservation(), [b"archive"])
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
    receipt.unlink()
    with pytest.raises(FtpsStagingError):
        value.inspect(reservation())
    assert artifact.archive_size_bytes == 7


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
                "archive_size_bytes": 7,
                "archive_sha256": "sha256:" + "0" * 64,
            }
        ).encode(),
        json.dumps(
            {
                "version": "1",
                "staging_id": STAGE_ID,
                "printer_uuid": PRINTER_ID,
                "archive_size_bytes": 7,
                "archive_sha256": "sha256:" + "0" * 64,
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


def test_receipt_writer_closes_failed_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = tmp_path / "receipt"
    artifact = StagedArtifact(
        reservation=reservation(),
        archive_size_bytes=0,
        archive_sha256="sha256:" + "0" * 64,
    )

    monkeypatch.setattr(os, "write", lambda _fd, _data: 0)
    with pytest.raises(FtpsStagingError):
        staging._write_receipt(receipt, artifact)


def test_inspect_rejects_non_reservation_and_invalid_private_receipt(tmp_path: Path) -> None:
    value = store(tmp_path)
    value.stage(reservation(), [b"archive"])
    receipt = tmp_path / "ftps-staging" / f"{STAGE_ID}.receipt"
    receipt.chmod(0o644)
    with pytest.raises(FtpsStagingError):
        value.inspect(reservation())
    with pytest.raises(FtpsStagingError):
        value.inspect(object())  # type: ignore[arg-type]
