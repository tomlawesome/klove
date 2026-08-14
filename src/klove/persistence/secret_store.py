"""Owner-only credential files and keyed idempotency fingerprints."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import stat
import uuid
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Final

from klove.domain.onboarding import CredentialReference
from klove.errors import KloveError
from klove.persistence.private_files import (
    PrivateFileError,
    fsync_directory,
    require_private_directory,
    require_private_file,
)

_KEY_FILE: Final = ".request-hmac-key"
_KEY_BYTES: Final = 32
_MAX_SECRET_BYTES: Final = 4096
_REFERENCE_PATTERN: Final = re.compile(
    r"^credential-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class SecretStoreError(KloveError):
    """The private credential store is unavailable, unsafe, or contradictory."""


class SecretStore:
    """Store secret values outside SQLite under opaque exact references."""

    def __init__(
        self,
        directory: Path,
        *,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        self._directory = directory
        self._random_bytes = random_bytes
        self._uuid_factory = uuid_factory
        self._fingerprint_key: bytes | None = None

    def initialize(self) -> None:
        """Validate the dedicated directory and create or load its HMAC key."""
        try:
            require_private_directory(self._directory)
            key_path = self._directory / _KEY_FILE
            if not key_path.exists() and not key_path.is_symlink():
                generated_key = self._random_bytes(_KEY_BYTES)
                if type(generated_key) is not bytes or len(generated_key) != _KEY_BYTES:
                    raise SecretStoreError
                with suppress(FileExistsError):
                    self._write_new_file(key_path, generated_key)
            require_private_file(key_path)
            key = self._read_file(key_path, maximum_bytes=_KEY_BYTES)
            if len(key) != _KEY_BYTES:
                raise SecretStoreError
            self._fingerprint_key = key
            self.references()
        except SecretStoreError:
            raise
        except (OSError, PrivateFileError, ValueError) as exc:
            raise SecretStoreError from exc

    def allocate_reference(self) -> CredentialReference:
        """Create one canonical opaque reference without writing a secret."""
        candidate = self._uuid_factory()
        value = f"credential-{candidate}"
        if (
            type(candidate) is not uuid.UUID
            or candidate.version != 4
            or not _valid_reference(value)
        ):
            raise SecretStoreError
        return value

    def write(self, reference: str, value: str, *, minimum_length: int) -> None:
        """Create one exact secret file once and durably publish its directory entry."""
        try:
            path = self._path(reference)
            encoded = _validate_secret(value, minimum_length=minimum_length)
            self._write_new_file(path, encoded)
            require_private_file(path)
        except FileExistsError as exc:
            raise SecretStoreError from exc
        except (OSError, UnicodeError, PrivateFileError, TypeError, ValueError) as exc:
            raise SecretStoreError from exc

    def read(self, reference: str, *, minimum_length: int) -> str:
        """Read and validate one complete exact secret without logging its value."""
        path = self._path(reference)
        try:
            require_private_file(path)
            encoded = self._read_file(path, maximum_bytes=_MAX_SECRET_BYTES)
            value = encoded.decode("utf-8", errors="strict")
            _validate_secret(value, minimum_length=minimum_length)
            return value
        except (OSError, UnicodeError, PrivateFileError, ValueError) as exc:
            raise SecretStoreError from exc

    def delete(self, reference: str) -> None:
        """Idempotently delete only one validated credential reference."""
        path = self._path(reference)
        try:
            require_private_file(path)
        except PrivateFileError as exc:
            if not path.exists() and not path.is_symlink():
                return
            raise SecretStoreError from exc
        try:
            path.unlink()
            fsync_directory(self._directory)
        except (OSError, PrivateFileError) as exc:
            raise SecretStoreError from exc

    def contains(self, reference: str) -> bool:
        """Return true only when an exact owner-only credential file exists."""
        path = self._path(reference)
        try:
            require_private_file(path)
        except PrivateFileError:
            if not path.exists() and not path.is_symlink():
                return False
            raise SecretStoreError from None
        return True

    def references(self) -> frozenset[CredentialReference]:
        """List exact credential references and reject every unknown directory entry."""
        try:
            require_private_directory(self._directory)
            references: set[CredentialReference] = set()
            with os.scandir(self._directory) as entries:
                for entry in entries:
                    if entry.name == _KEY_FILE:
                        require_private_file(Path(entry.path))
                        continue
                    if not _valid_reference(entry.name):
                        raise SecretStoreError
                    require_private_file(Path(entry.path))
                    references.add(entry.name)
            return frozenset(references)
        except SecretStoreError:
            raise
        except (OSError, PrivateFileError) as exc:
            raise SecretStoreError from exc

    def fingerprint(self, document: bytes) -> str:
        """Return a keyed digest so persisted request equality reveals no secret value."""
        if self._fingerprint_key is None:
            raise SecretStoreError
        return hmac.new(self._fingerprint_key, document, hashlib.sha256).hexdigest()

    @property
    def key_identity(self) -> str:
        """Return a non-secret digest binding registry backups to this key set."""
        if self._fingerprint_key is None:
            raise SecretStoreError
        return hashlib.sha256(self._fingerprint_key).hexdigest()

    def _path(self, reference: str) -> Path:
        if not _valid_reference(reference):
            raise SecretStoreError
        return self._directory / reference

    def _write_new_file(self, path: Path, value: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            view = memoryview(value)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        fsync_directory(self._directory)

    @staticmethod
    def _read_file(path: Path, *, maximum_bytes: int) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise SecretStoreError
            if os.name != "nt" and (
                metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise SecretStoreError
            if metadata.st_size > maximum_bytes:
                raise SecretStoreError
            result = bytearray()
            while len(result) <= maximum_bytes:
                chunk = os.read(descriptor, min(4096, maximum_bytes + 1 - len(result)))
                if not chunk:
                    break
                result.extend(chunk)
            if len(result) > maximum_bytes:
                raise SecretStoreError
            return bytes(result)
        finally:
            os.close(descriptor)


def _valid_reference(value: object) -> bool:
    return type(value) is str and _REFERENCE_PATTERN.fullmatch(value) is not None


def _validate_secret(value: str, *, minimum_length: int) -> bytes:
    if not 1 <= minimum_length <= _MAX_SECRET_BYTES:
        raise ValueError
    if type(value) is not str:
        raise TypeError
    encoded = value.encode("ascii", errors="strict")
    if len(value) < minimum_length or len(encoded) > _MAX_SECRET_BYTES:
        raise ValueError
    if any(ord(character) < 33 or ord(character) == 127 for character in value):
        raise ValueError
    return encoded
