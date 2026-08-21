from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable, Iterator
from typing import Any, TypedDict, cast

import pytest

from klove.security.owner_sessions import (
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_PATH,
    OnboardingOperation,
    OwnerCredentialAuthenticator,
    OwnerSessionDenied,
    OwnerSessionLease,
    OwnerSessionStore,
)

ORIGIN = "https://grove.example.invalid"


class SessionEvidence(TypedDict):
    cookie_headers: list[str]
    csrf_headers: list[str]
    origin_headers: list[str]
    operation: OnboardingOperation
    flow_nonce: str


def token(value: int) -> str:
    return base64.urlsafe_b64encode(value.to_bytes(32, "big")).rstrip(b"=").decode("ascii")


class Clock:
    def __init__(self) -> None:
        self.now: float | object = 10.0

    def __call__(self) -> float:
        return cast(float, self.now)


def token_sequence(*values: int) -> Callable[[int], str]:
    sequence: Iterator[int] = iter(values)

    def generate(size: int) -> str:
        assert size == 32
        return token(next(sequence))

    return generate


def store(  # noqa: PLR0913 -- concise security-fixture boundary.
    *,
    clock: Callable[[], float] | None = None,
    token_factory: Callable[[int], str] | None = None,
    capacity: int = 2,
    inactivity: int = 900,
    absolute: int = 1_800,
    secure: bool = True,
) -> OwnerSessionStore:
    kwargs: dict[str, object] = {
        "capacity": capacity,
        "inactivity_timeout_seconds": inactivity,
        "absolute_timeout_seconds": absolute,
        "cookie_secure": secure,
    }
    if clock is not None:
        kwargs["clock"] = clock
    if token_factory is not None:
        kwargs["token_factory"] = token_factory
    return OwnerSessionStore(frozenset({ORIGIN}), **kwargs)  # type: ignore[arg-type]


def evidence(grant: Any) -> SessionEvidence:
    return {
        "cookie_headers": [f"theme=dark; {SESSION_COOKIE_NAME}={grant.cookie_value}"],
        "csrf_headers": [grant.csrf_token],
        "origin_headers": [ORIGIN],
        "operation": grant.operation,
        "flow_nonce": grant.flow_nonce,
    }


def test_owner_credential_is_independent_exact_and_redacted() -> None:
    credential = "o" * 32
    authenticator = OwnerCredentialAuthenticator(credential)

    assert authenticator.authenticate(credential)
    for supplied in (None, "x" * 32, "short", "o" * 31 + " ", "é" * 32):
        assert not authenticator.authenticate(supplied)
    assert not authenticator.authenticate(cast(Any, 1))
    assert credential not in repr(authenticator)

    for invalid in ("short", "a" * 4_097, "a" * 31 + " ", "é" * 32, cast(Any, None)):
        with pytest.raises(ValueError, match="bounded visible ASCII"):
            OwnerCredentialAuthenticator(invalid)


@pytest.mark.parametrize(
    "overrides",
    [
        {"allowed_origins": frozenset()},
        {"allowed_origins": cast(Any, {ORIGIN})},
        {"allowed_origins": frozenset({""})},
        {"allowed_origins": frozenset({" https://grove.invalid"})},
        {"allowed_origins": frozenset({"https://grøve.invalid"})},
        {"allowed_origins": frozenset({str(index) for index in range(33)})},
        {"capacity": 0},
        {"capacity": 10_001},
        {"capacity": True},
        {"inactivity_timeout_seconds": 0},
        {"inactivity_timeout_seconds": 901},
        {"inactivity_timeout_seconds": 1.0},
        {"absolute_timeout_seconds": 0},
        {"absolute_timeout_seconds": 1_801},
        {"absolute_timeout_seconds": 1.0},
        {"inactivity_timeout_seconds": 20, "absolute_timeout_seconds": 10},
        {"cookie_secure": cast(Any, 1)},
    ],
)
def test_session_store_rejects_unbounded_or_ambiguous_limits(overrides: dict[str, object]) -> None:
    values: dict[str, object] = {
        "allowed_origins": frozenset({ORIGIN}),
        "capacity": 2,
        "inactivity_timeout_seconds": 900,
        "absolute_timeout_seconds": 1_800,
    }
    values.update(overrides)
    with pytest.raises(ValueError, match="limits are invalid"):
        OwnerSessionStore(**values)  # type: ignore[arg-type]


def test_issue_returns_only_strict_cookie_material_and_claims_exclusively() -> None:
    sessions = store(token_factory=token_sequence(1, 2, 3))
    grant = sessions.issue(ORIGIN, OnboardingOperation.CREATE)

    assert grant.operation is OnboardingOperation.CREATE
    assert len({grant.cookie_value, grant.csrf_token, grant.flow_nonce}) == 3
    assert repr(grant) == "OwnerSessionGrant(<redacted>)"
    assert grant.cookie_attributes == {
        "path": SESSION_COOKIE_PATH,
        "secure": True,
        "httponly": True,
        "samesite": "Strict",
        "max_age": 1_800,
    }
    assert CSRF_HEADER_NAME == "X-Klove-CSRF"
    assert sessions.active_count == 1

    lease = sessions.authorize(**evidence(grant))
    with pytest.raises(OwnerSessionDenied, match="owner session denied"):
        sessions.authorize(**evidence(grant))
    lease.release()
    with pytest.raises(OwnerSessionDenied):
        lease.release()

    final = sessions.authorize(**evidence(grant))
    final.invalidate()
    assert sessions.active_count == 0
    with pytest.raises(OwnerSessionDenied):
        final.invalidate()
    with pytest.raises(OwnerSessionDenied):
        sessions.authorize(**evidence(grant))


def test_loopback_development_cookie_can_be_explicitly_non_secure() -> None:
    loopback = "http://127.0.0.1:8080"
    sessions = OwnerSessionStore(
        frozenset({loopback, ORIGIN}),
        capacity=2,
        inactivity_timeout_seconds=900,
        absolute_timeout_seconds=1_800,
        cookie_secure=False,
        token_factory=token_sequence(1, 2, 3, 4, 5, 6),
    )
    grant = sessions.issue(loopback, OnboardingOperation.INSPECT)

    assert grant.cookie_attributes["secure"] is False
    assert sessions.issue(ORIGIN, OnboardingOperation.INSPECT).cookie_attributes["secure"] is True


def test_origin_operation_flow_and_csrf_are_all_exact() -> None:
    sessions = store(token_factory=token_sequence(1, 2, 3))
    grant = sessions.issue(ORIGIN, OnboardingOperation.UPDATE)
    valid = evidence(grant)
    invalid: list[dict[str, object]] = [
        {"cookie_headers": []},
        {"cookie_headers": ["", ""]},
        {"cookie_headers": [f"{SESSION_COOKIE_NAME}={grant.cookie_value}", "other=one"]},
        {"cookie_headers": ["missing"]},
        {"cookie_headers": ["=missing-name"]},
        {"cookie_headers": ["bad name=value"]},
        {"cookie_headers": ['other="quoted"']},
        {"cookie_headers": ["other=é"]},
        {"cookie_headers": ["other=one; other=two"]},
        {"cookie_headers": [f"{SESSION_COOKIE_NAME}=short"]},
        {"cookie_headers": [f"other=value; {SESSION_COOKIE_NAME}={token(99)}"]},
        {"csrf_headers": []},
        {"csrf_headers": [grant.csrf_token, grant.csrf_token]},
        {"csrf_headers": ["short"]},
        {"csrf_headers": [token(99)]},
        {"origin_headers": []},
        {"origin_headers": [ORIGIN, ORIGIN]},
        {"origin_headers": [f" {ORIGIN}"]},
        {"origin_headers": ["https://other.invalid"]},
        {"operation": OnboardingOperation.CREATE},
        {"operation": cast(Any, "update")},
        {"flow_nonce": "short"},
        {"flow_nonce": token(99)},
    ]

    for change in invalid:
        request = dict(valid)
        request.update(change)
        with pytest.raises(OwnerSessionDenied):
            sessions.authorize(**request)  # type: ignore[arg-type]

    sessions.authorize(**valid).invalidate()


def test_capacity_expiry_and_restart_all_fail_closed() -> None:
    clock = Clock()
    sessions = store(
        clock=clock,
        token_factory=token_sequence(1, 2, 3, 4, 5, 6, 7, 8, 9),
        capacity=1,
        inactivity=10,
        absolute=20,
    )
    grant = sessions.issue(ORIGIN, OnboardingOperation.CREATE)
    with pytest.raises(OwnerSessionDenied):
        sessions.issue(ORIGIN, OnboardingOperation.CREATE)

    clock.now = 20.0
    assert sessions.active_count == 0
    replacement = sessions.issue(ORIGIN, OnboardingOperation.CREATE)
    assert replacement.cookie_value != grant.cookie_value

    restarted = store(token_factory=token_sequence(10, 11, 12))
    with pytest.raises(OwnerSessionDenied):
        restarted.authorize(**evidence(replacement))


def test_absolute_expiry_is_not_extended_by_sequential_activity() -> None:
    clock = Clock()
    sessions = store(
        clock=clock,
        token_factory=token_sequence(1, 2, 3, 4, 5, 6),
        inactivity=10,
        absolute=20,
    )
    grant = sessions.issue(ORIGIN, OnboardingOperation.UPDATE)
    clock.now = 19.0
    sessions.authorize(**evidence(grant)).release()
    clock.now = 30.0

    with pytest.raises(OwnerSessionDenied):
        sessions.authorize(**evidence(grant))
    assert sessions.active_count == 0


def test_expiry_or_clock_regression_during_a_claim_invalidates_it() -> None:
    clock = Clock()
    sessions = store(
        clock=clock,
        token_factory=token_sequence(1, 2, 3, 4, 5, 6),
        inactivity=10,
        absolute=20,
    )
    grant = sessions.issue(ORIGIN, OnboardingOperation.DISABLE)
    lease = sessions.authorize(**evidence(grant))
    clock.now = 20.0
    with pytest.raises(OwnerSessionDenied):
        lease.release()
    assert sessions.active_count == 0

    clock.now = 30.0
    grant = sessions.issue(ORIGIN, OnboardingOperation.REMOVE)
    lease = sessions.authorize(**evidence(grant))
    clock.now = 29.0
    with pytest.raises(OwnerSessionDenied):
        lease.release()


def test_entropy_collisions_and_failures_are_bounded_and_redacted() -> None:
    collisions = store(token_factory=lambda _size: token(1))
    with pytest.raises(OwnerSessionDenied) as collision:
        collisions.issue(ORIGIN, OnboardingOperation.CREATE)
    assert token(1) not in str(collision.value)

    sequence = token_sequence(1, 2, 3, 1, 4, 5, 6, 7, 8)
    sessions = store(token_factory=sequence)
    first = sessions.issue(ORIGIN, OnboardingOperation.CREATE)
    second = sessions.issue(ORIGIN, OnboardingOperation.CREATE)
    assert first.cookie_value != second.cookie_value

    for factory in (
        lambda _size: "short",
        lambda _size: cast(Any, b"not-a-string"),
        lambda _size: (_ for _ in ()).throw(RuntimeError("secret detail")),
    ):
        broken = store(token_factory=factory)
        with pytest.raises(OwnerSessionDenied) as denied:
            broken.issue(ORIGIN, OnboardingOperation.CREATE)
        assert "secret detail" not in str(denied.value)


@pytest.mark.parametrize("bad_clock", [-1.0, float("nan"), float("inf"), True, "now"])
def test_untrusted_clock_values_fail_closed(bad_clock: object) -> None:
    clock = Clock()
    clock.now = bad_clock
    sessions = store(clock=clock)

    with pytest.raises(OwnerSessionDenied):
        sessions.issue(ORIGIN, OnboardingOperation.CREATE)


def test_unknown_or_unclaimed_internal_lease_cannot_change_state() -> None:
    sessions = store(token_factory=token_sequence(1, 2, 3))
    grant = sessions.issue(ORIGIN, OnboardingOperation.CREATE)
    unknown = OwnerSessionLease(sessions, hashlib.sha256(b"unknown").digest())
    with pytest.raises(OwnerSessionDenied):
        unknown.release()

    digest = hashlib.sha256(b"klove-owner-session-v1\x00" + grant.cookie_value.encode()).digest()
    unclaimed = OwnerSessionLease(sessions, digest)
    with pytest.raises(OwnerSessionDenied):
        unclaimed.invalidate()


def test_unknown_origin_or_untyped_operation_never_issues_material() -> None:
    sessions = store(token_factory=token_sequence(1, 2, 3))
    with pytest.raises(OwnerSessionDenied):
        sessions.issue("https://other.invalid", OnboardingOperation.CREATE)
    with pytest.raises(OwnerSessionDenied):
        sessions.issue(ORIGIN, cast(Any, "create"))
