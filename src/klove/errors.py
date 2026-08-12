"""Bounded error types used at Klove trust boundaries."""


class KloveError(Exception):
    """Base class for errors safe to classify without exposing their input."""


class ConfigurationError(KloveError):
    """Configuration or secret material failed validation."""


class ProtocolError(KloveError):
    """A remote peer violated the expected protocol contract."""


class StateEvidenceError(KloveError):
    """Printer evidence was stale, malformed, or internally contradictory."""
