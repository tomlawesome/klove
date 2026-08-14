from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from pathlib import Path

import pytest

import klove.persistence.secret_store as secret_module
from klove.persistence.secret_store import SecretStore, SecretStoreError

from ..onboarding_helpers import COMPATIBILITY_REF, MOONRAKER_REF


def secret_store(
    tmp_path: Path,
    *,
    random_bytes: object | None = None,
    uuid_factory: object | None = None,
) -> SecretStore:
    directory = tmp_path / "secrets"
    directory.mkdir(mode=0o700, parents=True)
    return SecretStore(
        directory,
        random_bytes=(lambda length: b"k" * length) if random_bytes is None else random_bytes,  # type: ignore[arg-type]
        uuid_factory=uuid.uuid4 if uuid_factory is None else uuid_factory,  # type: ignore[arg-type]
    )


def test_initialize_persists_private_key_and_keyed_fingerprints(tmp_path: Path) -> None:
    store = secret_store(tmp_path)
    with pytest.raises(SecretStoreError):
        store.fingerprint(b"request")
    with pytest.raises(SecretStoreError):
        _ = store.key_identity

    store.initialize()
    key_path = tmp_path / "secrets" / ".request-hmac-key"
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert store.references() == frozenset()
    assert store.key_identity == hashlib.sha256(b"k" * 32).hexdigest()
    assert (
        store.fingerprint(b"request")
        == hmac.new(
            b"k" * 32,
            b"request",
            hashlib.sha256,
        ).hexdigest()
    )

    restarted = SecretStore(
        tmp_path / "secrets",
        random_bytes=lambda _length: (_ for _ in ()).throw(AssertionError),
    )
    restarted.initialize()
    assert restarted.key_identity == store.key_identity


@pytest.mark.parametrize("generated", [b"short", "not-bytes"])
def test_initialize_rejects_invalid_generated_key(tmp_path: Path, generated: object) -> None:
    store = secret_store(tmp_path, random_bytes=lambda _length: generated)
    with pytest.raises(SecretStoreError):
        store.initialize()


def test_initialize_rejects_missing_unsafe_or_contradictory_storage(tmp_path: Path) -> None:
    with pytest.raises(SecretStoreError):
        SecretStore(tmp_path / "missing").initialize()

    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    public.chmod(0o755)
    with pytest.raises(SecretStoreError):
        SecretStore(public).initialize()

    short = tmp_path / "short"
    short.mkdir(mode=0o700)
    (short / ".request-hmac-key").write_bytes(b"short")
    (short / ".request-hmac-key").chmod(0o600)
    with pytest.raises(SecretStoreError):
        SecretStore(short).initialize()

    unknown = tmp_path / "unknown"
    unknown.mkdir(mode=0o700)
    (unknown / "unexpected").touch(mode=0o600)
    with pytest.raises(SecretStoreError):
        SecretStore(unknown, random_bytes=lambda length: b"k" * length).initialize()


def test_initialize_handles_exclusive_key_creation_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = secret_store(tmp_path)
    original = store._write_new_file

    def raced(path: Path, value: bytes) -> None:
        original(path, value)
        raise FileExistsError

    monkeypatch.setattr(store, "_write_new_file", raced)
    store.initialize()
    assert store.key_identity == hashlib.sha256(b"k" * 32).hexdigest()


def test_reference_allocation_is_canonical_and_rejects_bad_factories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_uuid = uuid.UUID("55555555-5555-4555-8555-555555555555")
    assert secret_store(tmp_path, uuid_factory=lambda: canonical_uuid).allocate_reference() == (
        MOONRAKER_REF
    )

    for candidate in (uuid.uuid1(), "not-a-uuid"):
        directory = tmp_path / f"invalid-{type(candidate).__name__}"
        store = secret_store(directory, uuid_factory=lambda candidate=candidate: candidate)
        with pytest.raises(SecretStoreError):
            store.allocate_reference()

    store = secret_store(tmp_path / "invalid-reference", uuid_factory=lambda: canonical_uuid)
    monkeypatch.setattr(secret_module, "_valid_reference", lambda _value: False)
    with pytest.raises(SecretStoreError):
        store.allocate_reference()


def test_secret_write_read_list_contains_and_delete_are_exact(tmp_path: Path) -> None:
    store = secret_store(tmp_path)
    store.initialize()
    store.write(MOONRAKER_REF, "m" * 32, minimum_length=32)

    path = tmp_path / "secrets" / MOONRAKER_REF
    assert path.stat().st_mode & 0o777 == 0o600
    assert store.read(MOONRAKER_REF, minimum_length=32) == "m" * 32
    assert store.contains(MOONRAKER_REF)
    assert store.references() == frozenset({MOONRAKER_REF})
    with pytest.raises(SecretStoreError):
        store.write(MOONRAKER_REF, "m" * 32, minimum_length=32)

    store.delete(MOONRAKER_REF)
    store.delete(MOONRAKER_REF)
    assert not store.contains(MOONRAKER_REF)
    assert store.references() == frozenset()


@pytest.mark.parametrize(
    ("value", "minimum_length"),
    [
        ("token", 0),
        ("token", 4097),
        ("short", 6),
        ("x" * 4097, 1),
        ("non-ascii-£", 1),
        ("contains space", 1),
        ("contains\x7fcontrol", 1),
        (b"not-text", 1),
    ],
)
def test_write_rejects_invalid_secret_values(
    tmp_path: Path,
    value: object,
    minimum_length: int,
) -> None:
    store = secret_store(tmp_path)
    store.initialize()
    with pytest.raises(SecretStoreError):
        store.write(
            MOONRAKER_REF,
            value,  # type: ignore[arg-type]
            minimum_length=minimum_length,
        )


@pytest.mark.parametrize("reference", ["../escape", "credential-invalid", 1])
def test_public_methods_reject_invalid_references(tmp_path: Path, reference: object) -> None:
    store = secret_store(tmp_path)
    store.initialize()
    with pytest.raises(SecretStoreError):
        store.write(reference, "x" * 32, minimum_length=32)  # type: ignore[arg-type]
    with pytest.raises(SecretStoreError):
        store.read(reference, minimum_length=1)  # type: ignore[arg-type]
    with pytest.raises(SecretStoreError):
        store.delete(reference)  # type: ignore[arg-type]
    with pytest.raises(SecretStoreError):
        store.contains(reference)  # type: ignore[arg-type]


def test_read_rejects_missing_invalid_encoding_size_and_permissions(tmp_path: Path) -> None:
    store = secret_store(tmp_path)
    store.initialize()
    directory = tmp_path / "secrets"

    with pytest.raises(SecretStoreError):
        store.read(MOONRAKER_REF, minimum_length=1)

    corrupt = directory / MOONRAKER_REF
    corrupt.write_bytes(b"\xff")
    corrupt.chmod(0o600)
    with pytest.raises(SecretStoreError):
        store.read(MOONRAKER_REF, minimum_length=1)
    corrupt.unlink()

    corrupt.write_bytes(b"x" * 4097)
    corrupt.chmod(0o600)
    with pytest.raises(SecretStoreError):
        store.read(MOONRAKER_REF, minimum_length=1)
    corrupt.unlink()

    corrupt.write_text("valid-token", encoding="ascii")
    corrupt.chmod(0o644)
    with pytest.raises(SecretStoreError):
        store.read(MOONRAKER_REF, minimum_length=1)
    with pytest.raises(SecretStoreError):
        SecretStore._read_file(corrupt, maximum_bytes=32)


def test_contains_delete_and_listing_reject_unsafe_entries(tmp_path: Path) -> None:
    store = secret_store(tmp_path)
    store.initialize()
    directory = tmp_path / "secrets"
    target = directory / "target"
    target.write_text("token", encoding="ascii")
    target.chmod(0o600)
    linked = directory / MOONRAKER_REF
    linked.symlink_to(target)

    with pytest.raises(SecretStoreError):
        store.contains(MOONRAKER_REF)
    with pytest.raises(SecretStoreError):
        store.delete(MOONRAKER_REF)
    with pytest.raises(SecretStoreError):
        store.references()

    linked.unlink()
    target.unlink()
    unknown = directory / "unexpected"
    unknown.touch(mode=0o600)
    with pytest.raises(SecretStoreError):
        store.references()


def test_listing_rejects_public_key_and_credential_files(tmp_path: Path) -> None:
    store = secret_store(tmp_path)
    store.initialize()
    directory = tmp_path / "secrets"
    key = directory / ".request-hmac-key"
    key.chmod(0o644)
    with pytest.raises(SecretStoreError):
        store.references()
    key.chmod(0o600)

    credential = directory / COMPATIBILITY_REF
    credential.write_text("c" * 20, encoding="ascii")
    credential.chmod(0o644)
    with pytest.raises(SecretStoreError):
        store.references()


def test_low_level_writer_handles_partial_and_failed_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = secret_store(tmp_path)
    directory = tmp_path / "secrets"
    real_write = os.write

    def partial_write(descriptor: int, value: bytes | memoryview) -> int:
        return real_write(descriptor, bytes(value[:1]))

    monkeypatch.setattr(os, "write", partial_write)
    path = directory / "partial"
    store._write_new_file(path, b"complete")
    assert path.read_bytes() == b"complete"

    monkeypatch.setattr(os, "write", lambda _descriptor, _value: 0)
    with pytest.raises(OSError):
        store._write_new_file(directory / "failed", b"value")


def test_low_level_reader_rejects_nonregular_and_growth_past_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(SecretStoreError):
        SecretStore._read_file(directory, maximum_bytes=32)

    path = tmp_path / "growing"
    path.write_bytes(b"x")
    path.chmod(0o600)
    real_fstat = os.fstat
    real_read = os.read

    def small_fstat(descriptor: int) -> os.stat_result:
        result = real_fstat(descriptor)
        values = list(result)
        values[6] = 1
        return os.stat_result(values)

    chunks = iter((b"xx", b""))
    monkeypatch.setattr(os, "fstat", small_fstat)
    monkeypatch.setattr(os, "read", lambda _descriptor, _size: next(chunks))
    with pytest.raises(SecretStoreError):
        SecretStore._read_file(path, maximum_bytes=1)
    monkeypatch.setattr(os, "read", real_read)


def test_store_wraps_random_scandir_unlink_and_directory_sync_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = secret_store(tmp_path, random_bytes=lambda _length: (_ for _ in ()).throw(ValueError))
    with pytest.raises(SecretStoreError):
        store.initialize()

    store = secret_store(tmp_path / "io")
    store.initialize()
    store.write(MOONRAKER_REF, "m" * 32, minimum_length=32)
    monkeypatch.setattr(os, "scandir", lambda _path: (_ for _ in ()).throw(OSError))
    with pytest.raises(SecretStoreError):
        store.references()
    monkeypatch.undo()

    original_unlink = Path.unlink
    monkeypatch.setattr(Path, "unlink", lambda _path: (_ for _ in ()).throw(OSError))
    with pytest.raises(SecretStoreError):
        store.delete(MOONRAKER_REF)
    monkeypatch.setattr(Path, "unlink", original_unlink)

    monkeypatch.setattr(
        secret_module, "fsync_directory", lambda _path: (_ for _ in ()).throw(OSError)
    )
    with pytest.raises(SecretStoreError):
        store.delete(MOONRAKER_REF)
