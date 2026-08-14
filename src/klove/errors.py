"""Bounded error types used at Klove trust boundaries."""


class KloveError(Exception):
    """Base class for errors safe to classify without exposing their input."""


class ConfigurationError(KloveError):
    """Configuration or secret material failed validation."""


class ProtocolError(KloveError):
    """A remote peer violated the expected protocol contract."""


class StateEvidenceError(KloveError):
    """Printer evidence was stale, malformed, or internally contradictory."""


class ControlTransportError(KloveError):
    """A control transport failed without exposing remote or secret material."""


class UploadTransportError(KloveError):
    """An upload transport failed without exposing remote or artifact material."""


class StartTransportError(KloveError):
    """A print-start transport failed without exposing remote or artifact material."""


class JournalError(KloveError):
    """The durable operation journal is unavailable, invalid, or contradictory."""
