"""Independent owner authentication and bounded onboarding sessions."""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from klove.errors import KloveError

SESSION_COOKIE_NAME = "klove_setup"
SESSION_COOKIE_PATH = "/v1/onboarding"
CSRF_HEADER_NAME = "X-Klove-CSRF"
_TOKEN_BYTES = 32
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
_CANONICAL_UUID4 = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_COOKIE_NAME_PATTERN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_COOKIE_VALUE_PATTERN = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]*")
_SESSION_DOMAIN = b"klove-owner-session-v1\x00"
_CSRF_DOMAIN = b"klove-owner-csrf-v1\x00"
_ENTROPY_ATTEMPTS = 4


class OnboardingOperation(StrEnum):
    """One exact lifecycle operation authorized by an owner session."""

    INSPECT = "inspect"
    CREATE = "create"
    UPDATE = "update"
    ROTATE_MOONRAKER = "rotate_moonraker"
    ROTATE_COMPATIBILITY = "rotate_compatibility"
    DISABLE = "disable"
    REMOVE = "remove"


class OwnerSessionDenied(KloveError):
    """Fixed, redacted denial for every owner-session failure."""

    def __init__(self) -> None:
        super().__init__("owner session denied")


class OwnerCredentialAuthenticator:
    """Authenticate one owner credential independently of Grove and native API auth."""

    def __init__(self, credential: str) -> None:
        encoded = _bounded_ascii_token(credential)
        if encoded is None:
            raise ValueError("owner credential must be bounded visible ASCII")
        self._credential = encoded

    def authenticate(self, supplied: str | None) -> bool:
        """Compare one exact credential without exposing it or accepting malformed input."""
        if supplied is None:
            return False
        encoded = _bounded_ascii_token(supplied)
        return encoded is not None and hmac.compare_digest(encoded, self._credential)


@dataclass(frozen=True, slots=True, repr=False)
class OwnerSessionGrant:
    """Secret browser material issued after independent owner authentication."""

    cookie_value: str
    csrf_token: str
    flow_nonce: str
    operation: OnboardingOperation
    max_age_seconds: int
    cookie_secure: bool

    @property
    def cookie_attributes(self) -> dict[str, str | bool | int]:
        """Return the only permitted session-cookie attributes."""
        return {
            "path": SESSION_COOKIE_PATH,
            "secure": self.cookie_secure,
            "httponly": True,
            "samesite": "Strict",
            "max_age": self.max_age_seconds,
        }

    def __repr__(self) -> str:
        return "OwnerSessionGrant(<redacted>)"


@dataclass(frozen=True, slots=True)
class CompletionBinding:
    """One exact active record made available for a single framed handoff."""

    printer_uuid: str
    revision: int


@dataclass(slots=True)
class _Session:
    parent_origin: str
    request_origin: str
    flow_nonce: str
    operation: OnboardingOperation
    framed: bool
    csrf_digest: bytes
    created_at: float
    last_used_at: float
    in_use: bool = False
    completion: CompletionBinding | None = None


class OwnerSessionLease:
    """Exclusive authorization for one request using an owner session."""

    def __init__(self, store: OwnerSessionStore, session_digest: bytes) -> None:
        self._store = store
        self._session_digest = session_digest
        self._closed = False

    def release(self) -> None:
        """Permit a later request without completing the bound operation."""
        if self._closed:
            raise OwnerSessionDenied
        self._closed = True
        self._store._release(self._session_digest)

    def invalidate(self) -> None:
        """Invalidate after completion, cancellation, or an unsafe request outcome."""
        if self._closed:
            raise OwnerSessionDenied
        self._closed = True
        self._store._invalidate(self._session_digest)

    def bind_completion(self, printer_uuid: str, revision: int) -> None:
        """Reserve one exact framed CREATE result for the completion route."""
        if self._closed:
            raise OwnerSessionDenied
        self._store._bind_completion(self._session_digest, printer_uuid, revision)

    @property
    def completion(self) -> CompletionBinding | None:
        """Return the one bound result while this exact lease remains claimed."""
        if self._closed:
            raise OwnerSessionDenied
        return self._store._completion(self._session_digest)

    @property
    def framed(self) -> bool:
        """Return whether this lease was issued through the parent-frame proof."""
        if self._closed:
            raise OwnerSessionDenied
        return self._store._framed(self._session_digest)

    @property
    def parent_origin(self) -> str:
        """Return the exact configured parent origin bound to this active lease."""
        if self._closed:
            raise OwnerSessionDenied
        return self._store._parent_origin(self._session_digest)


class OwnerSessionStore:
    """In-memory, restart-invalidated, capacity-bounded owner sessions."""

    def __init__(  # noqa: PLR0913 -- every security boundary is explicit.
        self,
        allowed_origins: frozenset[str],
        *,
        capacity: int,
        inactivity_timeout_seconds: int,
        absolute_timeout_seconds: int,
        cookie_secure: bool = True,
        frame_origin: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[int], str] = secrets.token_urlsafe,
    ) -> None:
        if (
            type(allowed_origins) is not frozenset
            or not allowed_origins
            or len(allowed_origins) > 32
            or any(
                not isinstance(origin, str)
                or not origin
                or not origin.isascii()
                or origin != origin.strip()
                for origin in allowed_origins
            )
            or type(capacity) is not int
            or not 1 <= capacity <= 10_000
            or type(inactivity_timeout_seconds) is not int
            or inactivity_timeout_seconds < 1
            or inactivity_timeout_seconds > 900
            or type(absolute_timeout_seconds) is not int
            or absolute_timeout_seconds < inactivity_timeout_seconds
            or absolute_timeout_seconds > 1_800
            or type(cookie_secure) is not bool
            or (
                frame_origin is not None
                and (
                    not isinstance(frame_origin, str)
                    or not frame_origin
                    or not frame_origin.isascii()
                    or frame_origin != frame_origin.strip()
                )
            )
        ):
            raise ValueError("owner session limits are invalid")
        if frame_origin is not None and not _framed_origins_share_one_trusted_site(
            allowed_origins, frame_origin
        ):
            raise ValueError("framed owner-session origins must be exact and same-site")
        self._allowed_origins = allowed_origins
        self._capacity = capacity
        self._inactivity_timeout = inactivity_timeout_seconds
        self._absolute_timeout = absolute_timeout_seconds
        self._cookie_secure = cookie_secure
        self._frame_origin = frame_origin
        self._clock = clock
        self._token_factory = token_factory
        self._sessions: dict[bytes, _Session] = {}

    def issue(self, origin: str, operation: OnboardingOperation) -> OwnerSessionGrant:
        """Issue three independent unguessable values for one exact origin and operation."""
        return self._issue(
            parent_origin=origin,
            request_origin=origin,
            operation=operation,
        )

    def issue_framed(
        self,
        *,
        parent_origin: str,
        operation: OnboardingOperation,
    ) -> OwnerSessionGrant:
        """Issue a session for one configured parent after its one-time frame proof."""
        if self._frame_origin is None:
            raise OwnerSessionDenied
        return self._issue(
            parent_origin=parent_origin,
            request_origin=self._frame_origin,
            operation=operation,
        )

    @property
    def frame_origin(self) -> str | None:
        """Return the configured browser request origin, if frame routes are enabled."""
        return self._frame_origin

    @property
    def allowed_parent_origins(self) -> frozenset[str]:
        """Return the exact public origins that may own a framed session."""
        return self._allowed_origins

    def _issue(
        self,
        *,
        parent_origin: str,
        request_origin: str,
        operation: OnboardingOperation,
    ) -> OwnerSessionGrant:
        """Create a session with separate parent and browser request origins."""
        now = self._now()
        self._purge_expired(now)
        if (
            parent_origin not in self._allowed_origins
            or not isinstance(operation, OnboardingOperation)
            or len(self._sessions) >= self._capacity
        ):
            raise OwnerSessionDenied
        for _attempt in range(_ENTROPY_ATTEMPTS):
            cookie_value = self._new_token()
            csrf_token = self._new_token()
            flow_nonce = self._new_token()
            session_digest = _digest(_SESSION_DOMAIN, cookie_value)
            if session_digest in self._sessions or len({cookie_value, csrf_token, flow_nonce}) != 3:
                continue
            self._sessions[session_digest] = _Session(
                parent_origin=parent_origin,
                request_origin=request_origin,
                flow_nonce=flow_nonce,
                operation=operation,
                framed=parent_origin != request_origin,
                csrf_digest=_digest(_CSRF_DOMAIN, csrf_token),
                created_at=now,
                last_used_at=now,
            )
            return OwnerSessionGrant(
                cookie_value=cookie_value,
                csrf_token=csrf_token,
                flow_nonce=flow_nonce,
                operation=operation,
                max_age_seconds=self._absolute_timeout,
                cookie_secure=self._cookie_secure or request_origin.startswith("https://"),
            )
        raise OwnerSessionDenied

    def authorize(
        self,
        *,
        cookie_headers: Sequence[str],
        csrf_headers: Sequence[str],
        origin_headers: Sequence[str],
        operation: OnboardingOperation,
        flow_nonce: str,
    ) -> OwnerSessionLease:
        """Claim one session exclusively from exact raw HTTP request evidence."""
        cookie_value = _session_cookie(cookie_headers)
        csrf_token = _single_token(csrf_headers)
        origin = _single_header(origin_headers)
        if (
            cookie_value is None
            or csrf_token is None
            or origin is None
            or not isinstance(operation, OnboardingOperation)
            or not _valid_token(flow_nonce)
        ):
            raise OwnerSessionDenied
        digest = _digest(_SESSION_DOMAIN, cookie_value)
        session = self._sessions.get(digest)
        now = self._now()
        if session is None:
            raise OwnerSessionDenied
        if self._expired(session, now):
            self._sessions.pop(digest, None)
            raise OwnerSessionDenied
        if (
            session.in_use
            or session.request_origin != origin
            or session.operation is not operation
            or not hmac.compare_digest(session.flow_nonce, flow_nonce)
            or not hmac.compare_digest(session.csrf_digest, _digest(_CSRF_DOMAIN, csrf_token))
        ):
            raise OwnerSessionDenied
        session.in_use = True
        return OwnerSessionLease(self, digest)

    @property
    def active_count(self) -> int:
        """Return bounded non-secret state for health and tests."""
        now = self._now()
        self._purge_expired(now)
        return len(self._sessions)

    def _release(self, digest: bytes) -> None:
        session = self._sessions.get(digest)
        now = self._now()
        if session is None or not session.in_use or self._expired(session, now):
            self._sessions.pop(digest, None)
            raise OwnerSessionDenied
        session.in_use = False
        session.last_used_at = now

    def _invalidate(self, digest: bytes) -> None:
        session = self._sessions.pop(digest, None)
        if session is None or not session.in_use:
            raise OwnerSessionDenied

    def _parent_origin(self, digest: bytes) -> str:
        session = self._sessions.get(digest)
        if session is None or not session.in_use:
            raise OwnerSessionDenied
        return session.parent_origin

    def _bind_completion(self, digest: bytes, printer_uuid: str, revision: int) -> None:
        session = self._sessions.get(digest)
        if (
            session is None
            or not session.in_use
            or session.operation is not OnboardingOperation.CREATE
            or not session.framed
            or session.completion is not None
            or type(printer_uuid) is not str
            or _CANONICAL_UUID4.fullmatch(printer_uuid) is None
            or type(revision) is not int
            or isinstance(revision, bool)
            or not 1 <= revision <= 9_223_372_036_854_775_807
        ):
            raise OwnerSessionDenied
        session.completion = CompletionBinding(printer_uuid, revision)

    def _completion(self, digest: bytes) -> CompletionBinding | None:
        session = self._sessions.get(digest)
        if session is None or not session.in_use:
            raise OwnerSessionDenied
        return session.completion

    def _framed(self, digest: bytes) -> bool:
        session = self._sessions.get(digest)
        if session is None or not session.in_use:
            raise OwnerSessionDenied
        return session.framed

    def _purge_expired(self, now: float) -> None:
        for digest, session in tuple(self._sessions.items()):
            if self._expired(session, now):
                del self._sessions[digest]

    def _expired(self, session: _Session, now: float) -> bool:
        return (
            now < session.last_used_at
            or now < session.created_at
            or (
                now - session.last_used_at >= self._inactivity_timeout
                or now - session.created_at >= self._absolute_timeout
            )
        )

    def _now(self) -> float:
        value = self._clock()
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            raise OwnerSessionDenied
        return float(value)

    def _new_token(self) -> str:
        try:
            token = self._token_factory(_TOKEN_BYTES)
        except Exception:
            raise OwnerSessionDenied from None
        if not isinstance(token, str) or not _valid_token(token):
            raise OwnerSessionDenied
        return token


def _framed_origins_share_one_trusted_site(
    parent_origins: frozenset[str], frame_origin: str
) -> bool:
    frame = _origin_parts(frame_origin)
    if frame is None:
        return False
    return all(
        (parent := _origin_parts(origin)) is not None
        and parent[:2] == frame[:2]
        and parent != frame
        for origin in parent_origins
    )


def _origin_parts(origin: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    return (
        parsed.scheme,
        parsed.hostname,
        port if port is not None else (443 if parsed.scheme == "https" else 80),
    )


def _bounded_ascii_token(value: str) -> bytes | None:
    try:
        encoded = value.encode("ascii", errors="strict")
    except (AttributeError, UnicodeError):
        return None
    if not 32 <= len(encoded) <= 4_096 or any(byte <= 32 or byte == 127 for byte in encoded):
        return None
    return encoded


def _valid_token(value: object) -> bool:
    return isinstance(value, str) and _TOKEN_PATTERN.fullmatch(value) is not None


def _digest(domain: bytes, token: str) -> bytes:
    return hashlib.sha256(domain + token.encode("ascii")).digest()


def _single_token(values: Sequence[str]) -> str | None:
    if len(values) != 1 or not _valid_token(values[0]):
        return None
    return values[0]


def _single_header(values: Sequence[str]) -> str | None:
    if len(values) != 1 or not values[0] or values[0] != values[0].strip():
        return None
    return values[0]


def _session_cookie(headers: Sequence[str]) -> str | None:
    if len(headers) != 1 or not headers[0] or not headers[0].isascii():
        return None
    cookies: dict[str, str] = {}
    for segment in headers[0].split(";"):
        name, separator, value = segment.strip().partition("=")
        if (
            separator != "="
            or not name
            or _COOKIE_NAME_PATTERN.fullmatch(name) is None
            or _COOKIE_VALUE_PATTERN.fullmatch(value) is None
            or name in cookies
        ):
            return None
        cookies[name] = value
    session_value = cookies.get(SESSION_COOKIE_NAME)
    return session_value if session_value is not None and _valid_token(session_value) else None
