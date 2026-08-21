from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest

from klove.security.frame_handshake import (
    FRAME_HANDSHAKE_TIMEOUT_SECONDS,
    FRAME_PROTOCOL_VERSION,
    FrameHandshakeDenied,
    FrameHandshakeGrant,
    FrameHandshakeStore,
)
from klove.security.owner_sessions import OnboardingOperation

PARENT = "https://grove.example.invalid"


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


def store(
    *,
    clock: Callable[[], float] | None = None,
    token_factory: Callable[[int], str] | None = None,
    capacity: int = 2,
    timeout: int = 300,
) -> FrameHandshakeStore:
    values: dict[str, object] = {"capacity": capacity, "timeout_seconds": timeout}
    if clock is not None:
        values["clock"] = clock
    if token_factory is not None:
        values["token_factory"] = token_factory
    return FrameHandshakeStore(frozenset({PARENT}), **values)  # type: ignore[arg-type]


def issue(
    handshakes: FrameHandshakeStore,
    *,
    parent_nonce: str = token(1),
    operation: OnboardingOperation = OnboardingOperation.CREATE,
) -> FrameHandshakeGrant:
    return handshakes.issue(
        parent_origin=PARENT,
        parent_nonce=parent_nonce,
        operation=operation,
    )


def test_constants_and_public_origin_set_are_exact() -> None:
    handshakes = store(token_factory=token_sequence(2))

    assert FRAME_PROTOCOL_VERSION == 1
    assert FRAME_HANDSHAKE_TIMEOUT_SECONDS == 300
    assert handshakes.allowed_parent_origins == frozenset({PARENT})


@pytest.mark.parametrize(
    "overrides",
    [
        {"allowed_parent_origins": frozenset()},
        {"allowed_parent_origins": cast(Any, {PARENT})},
        {"allowed_parent_origins": frozenset({""})},
        {"allowed_parent_origins": frozenset({" https://grove.invalid"})},
        {"allowed_parent_origins": frozenset({"https://grøve.invalid"})},
        {"allowed_parent_origins": frozenset({str(index) for index in range(33)})},
        {"capacity": 0},
        {"capacity": 10_001},
        {"capacity": True},
        {"timeout_seconds": 0},
        {"timeout_seconds": 301},
        {"timeout_seconds": 1.0},
    ],
)
def test_store_rejects_ambiguous_or_unbounded_limits(overrides: dict[str, object]) -> None:
    values: dict[str, object] = {
        "allowed_parent_origins": frozenset({PARENT}),
        "capacity": 2,
        "timeout_seconds": 300,
    }
    values.update(overrides)
    with pytest.raises(ValueError, match="frame handshake limits are invalid"):
        FrameHandshakeStore(**values)  # type: ignore[arg-type]


def test_issue_and_consume_are_one_time_parent_bound_and_redacted() -> None:
    handshakes = store(token_factory=token_sequence(2))
    grant = issue(handshakes)

    assert grant.server_nonce == token(2)
    assert grant.expires_in_seconds == 300
    assert repr(grant) == "FrameHandshakeGrant(<redacted>)"
    assert handshakes.active_count == 1

    handshakes.consume(
        parent_origin=PARENT,
        parent_nonce=token(1),
        server_nonce=grant.server_nonce,
        operation=OnboardingOperation.CREATE,
    )
    assert handshakes.active_count == 0
    with pytest.raises(FrameHandshakeDenied, match="frame handshake denied"):
        handshakes.consume(
            parent_origin=PARENT,
            parent_nonce=token(1),
            server_nonce=grant.server_nonce,
            operation=OnboardingOperation.CREATE,
        )
    with pytest.raises(FrameHandshakeDenied):
        issue(handshakes)


def test_consumed_parent_nonce_tombstones_consume_capacity_until_expiry() -> None:
    clock = Clock()
    handshakes = store(clock=clock, token_factory=token_sequence(2, 3), capacity=1)
    grant = issue(handshakes)
    handshakes.consume(
        parent_origin=PARENT,
        parent_nonce=token(1),
        server_nonce=grant.server_nonce,
        operation=OnboardingOperation.CREATE,
    )

    with pytest.raises(FrameHandshakeDenied):
        issue(handshakes, parent_nonce=token(4))
    clock.now = 310.0
    replacement = issue(handshakes, parent_nonce=token(4))
    assert replacement.server_nonce == token(3)


def test_active_challenges_are_purged_after_expiry() -> None:
    clock = Clock()
    handshakes = store(clock=clock, token_factory=token_sequence(2))
    issue(handshakes)

    clock.now = 310.0
    assert handshakes.active_count == 0


def test_issue_rejects_unknown_or_malformed_parent_evidence() -> None:
    handshakes = store(token_factory=token_sequence(2))
    invalid: list[dict[str, object]] = [
        {"parent_origin": "https://other.invalid"},
        {"parent_nonce": "short"},
        {"parent_nonce": token(1), "operation": cast(Any, "create")},
    ]
    for values in invalid:
        with pytest.raises(FrameHandshakeDenied):
            handshakes.issue(
                parent_origin=cast(str, values.get("parent_origin", PARENT)),
                parent_nonce=cast(str, values.get("parent_nonce", token(1))),
                operation=cast(
                    OnboardingOperation, values.get("operation", OnboardingOperation.CREATE)
                ),
            )
    assert handshakes.active_count == 0


def test_entropy_collisions_and_failures_are_bounded_and_redacted() -> None:
    collisions = store(token_factory=lambda _size: token(2))
    first = issue(collisions)
    with pytest.raises(FrameHandshakeDenied) as denied:
        issue(collisions, parent_nonce=token(3))
    assert first.server_nonce not in str(denied.value)

    for factory in (
        lambda _size: "short",
        lambda _size: cast(Any, b"not-a-string"),
        lambda _size: (_ for _ in ()).throw(RuntimeError("private detail")),
    ):
        handshakes = store(token_factory=factory)
        with pytest.raises(FrameHandshakeDenied) as denied:
            issue(handshakes)
        assert "private detail" not in str(denied.value)


def test_consume_rejects_malformed_expired_replayed_and_mismatched_evidence() -> None:
    clock = Clock()
    handshakes = store(clock=clock, token_factory=token_sequence(2, 4, 6, 8))
    grant = issue(handshakes)
    invalid: list[dict[str, object]] = [
        {"parent_origin": "https://other.invalid"},
        {"parent_nonce": "short"},
        {"server_nonce": "short"},
        {"operation": cast(Any, "create")},
    ]
    for values in invalid:
        with pytest.raises(FrameHandshakeDenied):
            handshakes.consume(
                parent_origin=cast(str, values.get("parent_origin", PARENT)),
                parent_nonce=cast(str, values.get("parent_nonce", token(1))),
                server_nonce=cast(str, values.get("server_nonce", grant.server_nonce)),
                operation=cast(
                    OnboardingOperation, values.get("operation", OnboardingOperation.CREATE)
                ),
            )

    with pytest.raises(FrameHandshakeDenied):
        handshakes.consume(
            parent_origin=PARENT,
            parent_nonce=token(3),
            server_nonce=grant.server_nonce,
            operation=OnboardingOperation.CREATE,
        )
    with pytest.raises(FrameHandshakeDenied):
        handshakes.consume(
            parent_origin=PARENT,
            parent_nonce=token(1),
            server_nonce=grant.server_nonce,
            operation=OnboardingOperation.CREATE,
        )

    expiring = issue(handshakes, parent_nonce=token(3))
    clock.now = 310.0
    with pytest.raises(FrameHandshakeDenied):
        handshakes.consume(
            parent_origin=PARENT,
            parent_nonce=token(3),
            server_nonce=expiring.server_nonce,
            operation=OnboardingOperation.CREATE,
        )


@pytest.mark.parametrize("bad_clock", [-1.0, float("nan"), float("inf"), True, "now"])
def test_untrusted_clock_values_fail_closed(bad_clock: object) -> None:
    clock = Clock()
    clock.now = bad_clock
    handshakes = store(clock=clock)

    with pytest.raises(FrameHandshakeDenied):
        issue(handshakes)


def test_clock_regression_expires_challenges_and_retains_parent_tombstones() -> None:
    clock = Clock()
    handshakes = store(clock=clock, token_factory=token_sequence(2, 4), capacity=2)
    grant = issue(handshakes)
    clock.now = 9.0
    with pytest.raises(FrameHandshakeDenied):
        handshakes.consume(
            parent_origin=PARENT,
            parent_nonce=token(1),
            server_nonce=grant.server_nonce,
            operation=OnboardingOperation.CREATE,
        )
    with pytest.raises(FrameHandshakeDenied):
        issue(handshakes)
