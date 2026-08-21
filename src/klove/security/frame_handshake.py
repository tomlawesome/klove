"""One-time server proofs for the embedded onboarding frame handshake."""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from klove.errors import KloveError
from klove.security.owner_sessions import OnboardingOperation

FRAME_PROTOCOL_VERSION = 1
FRAME_CHALLENGE_TYPE = "klove.frame.challenge"
FRAME_HANDSHAKE_TIMEOUT_SECONDS = 300
_TOKEN_BYTES = 32
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
_SERVER_NONCE_DOMAIN = b"klove-frame-server-nonce-v1\x00"
_PARENT_NONCE_DOMAIN = b"klove-frame-parent-nonce-v1\x00"
_ENTROPY_ATTEMPTS = 4


class FrameHandshakeDenied(KloveError):
    """Fixed, redacted denial for all frame-handshake failures."""

    def __init__(self) -> None:
        super().__init__("frame handshake denied")


@dataclass(frozen=True, slots=True, repr=False)
class FrameHandshakeGrant:
    """The one-time server nonce returned only to an exact configured parent."""

    server_nonce: str
    expires_in_seconds: int

    def __repr__(self) -> str:
        return "FrameHandshakeGrant(<redacted>)"


@dataclass(frozen=True, slots=True)
class _Challenge:
    parent_origin: str
    parent_nonce: str
    operation: OnboardingOperation
    created_at: float


class FrameHandshakeStore:
    """In-memory one-time challenge proofs for exact parent/frame handshakes."""

    def __init__(
        self,
        allowed_parent_origins: frozenset[str],
        *,
        capacity: int,
        timeout_seconds: int = FRAME_HANDSHAKE_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[int], str] = secrets.token_urlsafe,
    ) -> None:
        if (
            type(allowed_parent_origins) is not frozenset
            or not allowed_parent_origins
            or len(allowed_parent_origins) > 32
            or any(
                not isinstance(origin, str)
                or not origin
                or not origin.isascii()
                or origin != origin.strip()
                for origin in allowed_parent_origins
            )
            or type(capacity) is not int
            or not 1 <= capacity <= 10_000
            or type(timeout_seconds) is not int
            or not 1 <= timeout_seconds <= FRAME_HANDSHAKE_TIMEOUT_SECONDS
        ):
            raise ValueError("frame handshake limits are invalid")
        self._allowed_parent_origins = allowed_parent_origins
        self._capacity = capacity
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        self._token_factory = token_factory
        self._challenges: dict[bytes, _Challenge] = {}
        self._parent_claims: dict[bytes, float] = {}

    def issue(
        self,
        *,
        parent_origin: str,
        parent_nonce: str,
        operation: OnboardingOperation,
    ) -> FrameHandshakeGrant:
        """Issue one proof after the browser supplies exact parent-origin evidence."""
        now = self._now()
        self._purge_expired(now)
        if (
            parent_origin not in self._allowed_parent_origins
            or not _valid_token(parent_nonce)
            or not isinstance(operation, OnboardingOperation)
            or len(self._parent_claims) >= self._capacity
        ):
            raise FrameHandshakeDenied
        parent_claim = _digest(_PARENT_NONCE_DOMAIN, parent_origin, parent_nonce, operation)
        if parent_claim in self._parent_claims:
            raise FrameHandshakeDenied
        for _attempt in range(_ENTROPY_ATTEMPTS):
            server_nonce = self._new_token()
            digest = _digest(_SERVER_NONCE_DOMAIN, server_nonce)
            if digest in self._challenges:
                continue
            self._challenges[digest] = _Challenge(
                parent_origin=parent_origin,
                parent_nonce=parent_nonce,
                operation=operation,
                created_at=now,
            )
            self._parent_claims[parent_claim] = now + self._timeout_seconds
            return FrameHandshakeGrant(
                server_nonce=server_nonce,
                expires_in_seconds=self._timeout_seconds,
            )
        raise FrameHandshakeDenied

    def consume(
        self,
        *,
        parent_origin: str,
        parent_nonce: str,
        server_nonce: str,
        operation: OnboardingOperation,
    ) -> None:
        """Consume a complete proof exactly once before owner credential exchange."""
        if (
            parent_origin not in self._allowed_parent_origins
            or not _valid_token(parent_nonce)
            or not _valid_token(server_nonce)
            or not isinstance(operation, OnboardingOperation)
        ):
            raise FrameHandshakeDenied
        digest = _digest(_SERVER_NONCE_DOMAIN, server_nonce)
        challenge = self._challenges.pop(digest, None)
        if challenge is None:
            raise FrameHandshakeDenied
        now = self._now()
        if self._expired(challenge, now) or not (
            hmac.compare_digest(challenge.parent_origin, parent_origin)
            and hmac.compare_digest(challenge.parent_nonce, parent_nonce)
            and challenge.operation is operation
        ):
            raise FrameHandshakeDenied

    @property
    def active_count(self) -> int:
        """Return the bounded number of in-memory challenge records."""
        now = self._now()
        self._purge_expired(now)
        return len(self._challenges)

    @property
    def allowed_parent_origins(self) -> frozenset[str]:
        """Return the exact public origins permitted to embed this frame."""
        return self._allowed_parent_origins

    def _purge_expired(self, now: float) -> None:
        for digest, challenge in tuple(self._challenges.items()):
            if self._expired(challenge, now):
                del self._challenges[digest]
        for claim, expires_at in tuple(self._parent_claims.items()):
            if now >= expires_at:
                del self._parent_claims[claim]

    def _expired(self, challenge: _Challenge, now: float) -> bool:
        return now < challenge.created_at or now - challenge.created_at >= self._timeout_seconds

    def _now(self) -> float:
        value = self._clock()
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            raise FrameHandshakeDenied
        return float(value)

    def _new_token(self) -> str:
        try:
            token = self._token_factory(_TOKEN_BYTES)
        except Exception:
            raise FrameHandshakeDenied from None
        if not _valid_token(token):
            raise FrameHandshakeDenied
        return token


def _valid_token(value: object) -> bool:
    return isinstance(value, str) and _TOKEN_PATTERN.fullmatch(value) is not None


def _digest(domain: bytes, *values: str | OnboardingOperation) -> bytes:
    encoded = b"\x00".join(
        (
            value.value.encode("ascii")
            if isinstance(value, OnboardingOperation)
            else value.encode("ascii")
        )
        for value in values
    )
    return hashlib.sha256(domain + encoded).digest()
