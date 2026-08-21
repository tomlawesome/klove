from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, TypeVar, cast

import pytest
from aiohttp.test_utils import TestClient, TestServer

from klove.domain.onboarding import RegisteredPrinter
from klove.northbound.api import create_api
from klove.orchestration.onboarding import LifecycleFailureCode, LifecycleServiceError
from klove.registry import PrinterRegistry
from klove.security.auth import BearerAuthenticator
from klove.security.owner_sessions import (
    SESSION_COOKIE_NAME,
    OnboardingOperation,
    OwnerCredentialAuthenticator,
    OwnerSessionStore,
)

from ..onboarding_helpers import IDEMPOTENCY_KEY, PRINTER_UUID, identity, printer
from .test_api import FakeControls

ORIGIN = "https://grove.example.invalid"
OWNER_CREDENTIAL = "o" * 32
ResultT = TypeVar("ResultT")


class FakeLifecycle:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.failure: Exception | None = None
        self.record = printer()

    async def inspect(self, request: object) -> object:
        return await self._complete("inspect", request, identity())

    async def create(self, request: object) -> RegisteredPrinter:
        return await self._complete("create", request, self.record)

    async def update(self, request: object) -> RegisteredPrinter:
        return await self._complete("update", request, self.record)

    async def rotate_moonraker(self, request: object) -> RegisteredPrinter:
        return await self._complete("rotate_moonraker", request, self.record)

    async def rotate_compatibility(self, request: object) -> RegisteredPrinter:
        return await self._complete("rotate_compatibility", request, self.record)

    async def disable(self, request: object) -> RegisteredPrinter:
        return await self._complete("disable", request, self.record)

    async def remove(self, request: object) -> RegisteredPrinter:
        return await self._complete("remove", request, self.record)

    def result(self, request: object) -> RegisteredPrinter:
        self.calls.append(("result", request))
        if self.failure is not None:
            raise self.failure
        return self.record

    async def _complete(self, name: str, request: object, result: ResultT) -> ResultT:
        self.calls.append((name, request))
        if self.failure is not None:
            raise self.failure
        return result


@pytest.fixture
async def onboarding_client() -> AsyncIterator[tuple[TestClient[Any, Any], FakeLifecycle]]:
    lifecycle = FakeLifecycle()
    sessions = OwnerSessionStore(
        frozenset({ORIGIN}),
        capacity=20,
        inactivity_timeout_seconds=900,
        absolute_timeout_seconds=1_800,
    )
    app = create_api(
        PrinterRegistry([]),
        BearerAuthenticator("a" * 32),
        FakeControls(),  # type: ignore[arg-type]
        owner_authenticator=OwnerCredentialAuthenticator(OWNER_CREDENTIAL),
        owner_sessions=sessions,
        lifecycle=lifecycle,  # type: ignore[arg-type]
    )
    async with TestClient(TestServer(app)) as client:
        yield client, lifecycle


async def session_headers(
    client: TestClient[Any, Any],
    operation: OnboardingOperation,
    *,
    idempotency: bool = False,
) -> tuple[dict[str, str], str]:
    response = await client.post(
        "/v1/onboarding/session",
        headers={"Origin": ORIGIN, "Content-Type": "application/json"},
        data=json.dumps({"owner_credential": OWNER_CREDENTIAL, "operation": operation.value}),
    )
    document = await response.json()
    headers = {
        "Origin": ORIGIN,
        "Content-Type": "application/json",
        "Cookie": f"{SESSION_COOKIE_NAME}={response.cookies[SESSION_COOKIE_NAME].value}",
        "X-Klove-CSRF": document["csrf_token"],
    }
    if idempotency:
        headers["Idempotency-Key"] = IDEMPOTENCY_KEY
    return headers, document["flow_nonce"]


def endpoint() -> dict[str, object]:
    return {
        "url": "http://127.0.0.1:7125",
        "allow_insecure_http": False,
        "verify_tls": False,
    }


@pytest.mark.asyncio
async def test_session_exchange_is_exact_bounded_and_private(
    onboarding_client: tuple[TestClient[Any, Any], FakeLifecycle],
) -> None:
    client, _lifecycle = onboarding_client
    response = await client.post(
        "/v1/onboarding/session",
        headers={"Origin": ORIGIN, "Content-Type": "application/json"},
        data=json.dumps(
            {"owner_credential": OWNER_CREDENTIAL, "operation": OnboardingOperation.CREATE}
        ),
    )
    document = await response.json()

    assert response.status == 201
    assert set(document) == {"csrf_token", "flow_nonce", "operation", "expires_in_seconds"}
    assert document["operation"] == "create"
    assert OWNER_CREDENTIAL not in await response.text()
    cookie = response.cookies[SESSION_COOKIE_NAME]
    assert cookie["httponly"] and cookie["secure"] and cookie["samesite"] == "Strict"
    assert cookie["path"] == "/v1/onboarding"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "method", "path", "body", "expected_call"),
    [
        (
            OnboardingOperation.INSPECT,
            "post",
            "/v1/onboarding/inspect",
            {
                "printer_uuid": PRINTER_UUID,
                "endpoint": endpoint(),
                "moonraker_credential": "m" * 32,
            },
            "inspect",
        ),
        (
            OnboardingOperation.CREATE,
            "post",
            "/v1/onboarding/printers",
            {
                "printer_uuid": PRINTER_UUID,
                "display_name": "Workshop Voron",
                "endpoint": endpoint(),
                "moonraker_credential": "m" * 32,
                "safety_profiles": [],
                "control_enabled": False,
                "dispatch_enabled": False,
            },
            "create",
        ),
        (
            OnboardingOperation.UPDATE,
            "put",
            f"/v1/onboarding/printers/{PRINTER_UUID}",
            {
                "expected_revision": 1,
                "display_name": "Workshop Voron",
                "endpoint": endpoint(),
                "safety_profiles": [],
                "control_enabled": False,
                "dispatch_enabled": False,
                "reactivate": False,
            },
            "update",
        ),
        (
            OnboardingOperation.ROTATE_MOONRAKER,
            "post",
            f"/v1/onboarding/printers/{PRINTER_UUID}/rotate/moonraker",
            {"expected_revision": 1, "moonraker_credential": "n" * 32},
            "rotate_moonraker",
        ),
        (
            OnboardingOperation.ROTATE_COMPATIBILITY,
            "post",
            f"/v1/onboarding/printers/{PRINTER_UUID}/rotate/compatibility",
            {"expected_revision": 1},
            "rotate_compatibility",
        ),
        (
            OnboardingOperation.DISABLE,
            "post",
            f"/v1/onboarding/printers/{PRINTER_UUID}/disable",
            {"expected_revision": 1},
            "disable",
        ),
        (
            OnboardingOperation.REMOVE,
            "delete",
            f"/v1/onboarding/printers/{PRINTER_UUID}",
            {"expected_revision": 1},
            "remove",
        ),
    ],
)
async def test_routes_forward_only_strict_typed_secret_free_requests(  # noqa: PLR0913,PLR0917
    onboarding_client: tuple[TestClient[Any, Any], FakeLifecycle],
    operation: OnboardingOperation,
    method: str,
    path: str,
    body: dict[str, object],
    expected_call: str,
) -> None:
    client, lifecycle = onboarding_client
    headers, flow_nonce = await session_headers(
        client,
        operation,
        idempotency=operation is not OnboardingOperation.INSPECT,
    )
    response = await client.request(
        method,
        path,
        headers=headers,
        data=json.dumps({"flow_nonce": flow_nonce, **body}),
    )
    document = await response.json()

    assert response.status == (201 if operation is OnboardingOperation.CREATE else 200)
    assert lifecycle.calls[-1][0] == expected_call
    request = cast(Any, lifecycle.calls[-1][1])
    assert request.actor == "owner" and request.request_origin == ORIGIN
    assert request.printer_uuid == PRINTER_UUID
    encoded = json.dumps(document)
    assert "credential_ref" not in encoded
    assert "m" * 32 not in encoded and "n" * 32 not in encoded
    if operation is OnboardingOperation.INSPECT:
        assert set(document) == {"identity"}
    else:
        assert set(document) == {"printer"}


@pytest.mark.asyncio
async def test_result_and_cancel_are_bound_to_the_original_operation(
    onboarding_client: tuple[TestClient[Any, Any], FakeLifecycle],
) -> None:
    client, lifecycle = onboarding_client
    headers, nonce = await session_headers(client, OnboardingOperation.CREATE)
    result = await client.post(
        "/v1/onboarding/result",
        headers=headers,
        data=json.dumps(
            {
                "flow_nonce": nonce,
                "operation": "create",
                "printer_uuid": PRINTER_UUID,
                "idempotency_key": IDEMPOTENCY_KEY,
            }
        ),
    )
    assert result.status == 200 and lifecycle.calls[-1][0] == "result"

    headers, nonce = await session_headers(client, OnboardingOperation.DISABLE)
    cancelled = await client.post(
        "/v1/onboarding/cancel",
        headers=headers,
        data=json.dumps({"flow_nonce": nonce, "operation": "disable"}),
    )
    replay = await client.post(
        "/v1/onboarding/cancel",
        headers=headers,
        data=json.dumps({"flow_nonce": nonce, "operation": "disable"}),
    )
    assert cancelled.status == 200 and await cancelled.json() == {"status": "cancelled"}
    assert replay.status == 403 and await replay.json() == {"error": "owner_denied"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "body", "status"),
    [
        ({"Origin": ORIGIN}, {}, 400),
        (
            {"Origin": ORIGIN, "Content-Type": "text/plain"},
            {"owner_credential": OWNER_CREDENTIAL, "operation": "create"},
            400,
        ),
        (
            {"Origin": ORIGIN, "Content-Type": "application/json"},
            {"owner_credential": "wrong" * 8, "operation": "create"},
            403,
        ),
        (
            {"Origin": ORIGIN, "Content-Type": "application/json"},
            {"owner_credential": OWNER_CREDENTIAL, "operation": "unknown"},
            400,
        ),
        (
            {"Origin": "https://other.invalid", "Content-Type": "application/json"},
            {"owner_credential": OWNER_CREDENTIAL, "operation": "create"},
            403,
        ),
    ],
)
async def test_session_exchange_denies_malformed_or_unauthorized_input(
    onboarding_client: tuple[TestClient[Any, Any], FakeLifecycle],
    headers: dict[str, str],
    body: dict[str, object],
    status: int,
) -> None:
    client, _lifecycle = onboarding_client
    response = await client.post(
        "/v1/onboarding/session",
        headers=headers,
        data=json.dumps(body),
    )
    assert response.status == status
    assert set(await response.json()) == {"error"}


@pytest.mark.asyncio
async def test_mutation_rejects_bad_evidence_before_service_and_releases_safe_failures(
    onboarding_client: tuple[TestClient[Any, Any], FakeLifecycle],
) -> None:
    client, lifecycle = onboarding_client
    headers, nonce = await session_headers(client, OnboardingOperation.DISABLE, idempotency=True)
    bad = await client.post(
        f"/v1/onboarding/printers/{PRINTER_UUID}/disable",
        headers={**headers, "X-Klove-CSRF": "x" * 43},
        data=json.dumps({"flow_nonce": nonce, "expected_revision": 1}),
    )
    assert bad.status == 403 and lifecycle.calls == []

    lifecycle.failure = LifecycleServiceError(LifecycleFailureCode.STALE_REVISION)
    stale = await client.post(
        f"/v1/onboarding/printers/{PRINTER_UUID}/disable",
        headers=headers,
        data=json.dumps({"flow_nonce": nonce, "expected_revision": 1}),
    )
    assert stale.status == 409
    lifecycle.failure = None
    retry = await client.post(
        f"/v1/onboarding/printers/{PRINTER_UUID}/disable",
        headers=headers,
        data=json.dumps({"flow_nonce": nonce, "expected_revision": 1}),
    )
    assert retry.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        LifecycleFailureCode.CONFLICT,
        LifecycleFailureCode.BUSY,
        LifecycleFailureCode.INVALID_TRANSITION,
        LifecycleFailureCode.PRINTER_FENCED,
        LifecycleFailureCode.PROBE_FAILED,
        LifecycleFailureCode.FENCE_UNAVAILABLE,
        LifecycleFailureCode.STORAGE_UNAVAILABLE,
        LifecycleFailureCode.INTERNAL_FAILURE,
    ],
)
async def test_lifecycle_failures_are_bounded_and_redacted(
    onboarding_client: tuple[TestClient[Any, Any], FakeLifecycle],
    code: LifecycleFailureCode,
) -> None:
    client, lifecycle = onboarding_client
    lifecycle.failure = LifecycleServiceError(code)
    headers, nonce = await session_headers(client, OnboardingOperation.INSPECT)
    response = await client.post(
        "/v1/onboarding/inspect",
        headers=headers,
        data=json.dumps(
            {
                "flow_nonce": nonce,
                "printer_uuid": PRINTER_UUID,
                "endpoint": endpoint(),
                "moonraker_credential": "m" * 32,
            }
        ),
    )
    expected = 422 if code is LifecycleFailureCode.PROBE_FAILED else 503
    if code in {
        LifecycleFailureCode.CONFLICT,
        LifecycleFailureCode.BUSY,
        LifecycleFailureCode.INVALID_TRANSITION,
        LifecycleFailureCode.PRINTER_FENCED,
    }:
        expected = 409
    assert response.status == expected and await response.json() == {"error": code.value}
    assert "m" * 32 not in await response.text()


@pytest.mark.asyncio
async def test_hostile_json_duplicate_headers_and_wrong_methods_fail_closed(
    onboarding_client: tuple[TestClient[Any, Any], FakeLifecycle],
) -> None:
    client, lifecycle = onboarding_client
    headers, nonce = await session_headers(client, OnboardingOperation.CREATE, idempotency=True)
    for body in (
        b"[]",
        b"{",
        b"\xff",
        b'{"flow_nonce":"x","flow_nonce":"y"}',
        b'{"value":NaN}',
    ):
        response = await client.post("/v1/onboarding/printers", headers=headers, data=body)
        assert response.status == 400 and await response.json() == {"error": "invalid_request"}
    duplicate_origin = await client.post(
        "/v1/onboarding/printers",
        headers=[
            ("Origin", ORIGIN),
            ("Origin", ORIGIN),
            ("Content-Type", "application/json"),
            ("Cookie", headers["Cookie"]),
            ("X-Klove-CSRF", headers["X-Klove-CSRF"]),
            ("Idempotency-Key", IDEMPOTENCY_KEY),
        ],
        data=json.dumps({"flow_nonce": nonce}),
    )
    wrong_method = await client.get("/v1/onboarding/session")
    missing = await client.get("/v1/onboarding/missing")
    assert duplicate_origin.status == 403
    assert wrong_method.status == 405 and missing.status == 404
    assert lifecycle.calls == []


@pytest.mark.asyncio
async def test_exact_route_edge_failures_are_redacted_and_do_not_leak_leases(
    onboarding_client: tuple[TestClient[Any, Any], FakeLifecycle],
) -> None:
    client, lifecycle = onboarding_client
    common = {"Origin": ORIGIN, "Content-Type": "application/json"}
    bad_session = await client.post(
        "/v1/onboarding/session",
        headers=common,
        data=json.dumps({"owner_credential": OWNER_CREDENTIAL}),
    )
    numeric_credential = await client.post(
        "/v1/onboarding/session",
        headers=common,
        data=json.dumps({"owner_credential": 1, "operation": "create"}),
    )
    empty_origin = await client.post(
        "/v1/onboarding/session",
        headers={"Origin": "", "Content-Type": "application/json"},
        data=json.dumps({"owner_credential": OWNER_CREDENTIAL, "operation": "create"}),
    )
    oversized = await client.post(
        "/v1/onboarding/session",
        headers=common,
        data=b"{" + b"x" * (17 * 1024),
    )
    assert [
        bad_session.status,
        numeric_credential.status,
        empty_origin.status,
        oversized.status,
    ] == [
        400,
        403,
        400,
        400,
    ]

    headers, nonce = await session_headers(client, OnboardingOperation.DISABLE)
    no_key = await client.post(
        f"/v1/onboarding/printers/{PRINTER_UUID}/disable",
        headers=headers,
        data=json.dumps({"flow_nonce": nonce, "expected_revision": 1}),
    )
    assert no_key.status == 400

    headers["Idempotency-Key"] = IDEMPOTENCY_KEY
    invalid_model = await client.post(
        f"/v1/onboarding/printers/{PRINTER_UUID}/disable",
        headers=headers,
        data=json.dumps({"flow_nonce": nonce, "expected_revision": "1"}),
    )
    assert invalid_model.status == 400

    lifecycle.failure = RuntimeError("private failure")
    unexpected = await client.post(
        f"/v1/onboarding/printers/{PRINTER_UUID}/disable",
        headers=headers,
        data=json.dumps({"flow_nonce": nonce, "expected_revision": 1}),
    )
    assert unexpected.status == 503 and await unexpected.json() == {"error": "internal_failure"}


@pytest.mark.asyncio
async def test_result_cancel_and_flow_decoders_reject_unknown_shapes(
    onboarding_client: tuple[TestClient[Any, Any], FakeLifecycle],
) -> None:
    client, lifecycle = onboarding_client
    headers, nonce = await session_headers(client, OnboardingOperation.CREATE)
    for document in (
        {"flow_nonce": nonce, "operation": "inspect"},
        {"flow_nonce": nonce, "operation": "unknown"},
        {"flow_nonce": nonce, "operation": 1},
    ):
        response = await client.post(
            "/v1/onboarding/result",
            headers=headers,
            data=json.dumps(document),
        )
        assert response.status == 400

    for document in (
        {"flow_nonce": nonce, "operation": "unknown"},
        {"flow_nonce": nonce, "operation": 1},
        {"flow_nonce": nonce, "operation": "create", "extra": True},
    ):
        response = await client.post(
            "/v1/onboarding/cancel",
            headers=headers,
            data=json.dumps(document),
        )
        assert response.status == 400

    not_text_nonce = await client.post(
        "/v1/onboarding/inspect",
        headers=headers,
        data=json.dumps({"flow_nonce": 1}),
    )
    assert not_text_nonce.status == 403 and lifecycle.calls == []
