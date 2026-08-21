"""Validated immutable identity shared by durable private stores."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import RFC_4122, UUID

from klove.errors import JournalError


def canonical_uuid4(value: str) -> str:
    """Return one canonical RFC 9562 UUIDv4 string or reject it."""
    if type(value) is not str:
        raise JournalError
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise JournalError from exc
    if parsed.version != 4 or parsed.variant != RFC_4122:
        raise JournalError
    canonical = str(parsed)
    if canonical != value:
        raise JournalError
    return canonical


@dataclass(frozen=True, slots=True)
class StoreIdentity:
    """The immutable installation, store, and exact schema identity."""

    installation_id: str
    store_id: str
    schema_version: int

    def __post_init__(self) -> None:
        canonical_uuid4(self.installation_id)
        canonical_uuid4(self.store_id)
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise JournalError
