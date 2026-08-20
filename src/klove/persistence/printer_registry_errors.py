"""Private exception hierarchy for canonical printer registry persistence."""

from klove.errors import KloveError


class RegistryStoreError(KloveError):
    """The canonical registry is unavailable, invalid, or contradictory."""


class RegistryConflictError(RegistryStoreError):
    """An idempotency key, UUID, endpoint, or credential is bound elsewhere."""


class RegistryBusyError(RegistryStoreError):
    """Another preparing operation already serializes this printer."""


class RegistryTransitionError(RegistryStoreError):
    """A requested persistent lifecycle transition is not exact or current."""
