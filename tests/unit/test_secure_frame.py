from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from multidict import CIMultiDict

from klove.domain.onboarding import RegisteredPrinter
from klove.domain.onboarding_requests import CreatePrinterRequest
from klove.northbound.api import create_api
from klove.northbound.onboarding_api import owner_sessions_key
from klove.northbound.secure_frame import (
    _challenge_preflight_is_exact,
    install_secure_frame_routes,
)
from klove.registry import PrinterRegistry
from klove.security.auth import BearerAuthenticator
from klove.security.frame_handshake import (
    FRAME_CHALLENGE_TYPE,
    FRAME_PROTOCOL_VERSION,
    FrameHandshakeStore,
)
from klove.security.owner_sessions import (
    SESSION_COOKIE_NAME,
    OnboardingOperation,
    OwnerCredentialAuthenticator,
    OwnerSessionStore,
)

from ..onboarding_helpers import IDEMPOTENCY_KEY, PRINTER_UUID, printer
from .test_api import FakeControls

PARENT_ORIGIN = "https://console.example.invalid"
FRAME_ORIGIN = "https://console.example.invalid:8443"
OWNER_CREDENTIAL = "o" * 32


class FrameLifecycle:
    def __init__(self) -> None:
        self.calls: list[CreatePrinterRequest] = []

    async def create(self, request: CreatePrinterRequest) -> RegisteredPrinter:
        self.calls.append(request)
        return printer()


@pytest.fixture
async def frame_client() -> AsyncIterator[tuple[TestClient[Any, Any], FrameLifecycle]]:
    lifecycle = FrameLifecycle()
    sessions = OwnerSessionStore(
        frozenset({PARENT_ORIGIN}),
        capacity=1,
        inactivity_timeout_seconds=900,
        absolute_timeout_seconds=1_800,
        cookie_secure=True,
        frame_origin=FRAME_ORIGIN,
    )
    handshakes = FrameHandshakeStore(frozenset({PARENT_ORIGIN}), capacity=8)
    app = create_api(
        PrinterRegistry([]),
        BearerAuthenticator("a" * 32),
        FakeControls(),  # type: ignore[arg-type]
        owner_authenticator=OwnerCredentialAuthenticator(OWNER_CREDENTIAL),
        owner_sessions=sessions,
        lifecycle=lifecycle,  # type: ignore[arg-type]
        frame_handshakes=handshakes,
    )
    async with TestClient(TestServer(app)) as client:
        yield client, lifecycle


def parent_nonce(value: str = "a") -> str:
    return value * 43


async def challenge(
    client: TestClient[Any, Any],
    *,
    nonce: str = parent_nonce(),
    operation: OnboardingOperation = OnboardingOperation.CREATE,
) -> dict[str, object]:
    response = await client.post(
        "/v1/onboarding/frame/challenge",
        headers={"Origin": PARENT_ORIGIN, "Content-Type": "application/json"},
        data=json.dumps(
            {
                "version": FRAME_PROTOCOL_VERSION,
                "type": FRAME_CHALLENGE_TYPE,
                "operation": operation.value,
                "parent_nonce": nonce,
            }
        ),
    )
    assert response.status == 201
    document = await response.json()
    assert isinstance(document, dict)
    return cast(dict[str, object], document)


async def frame_session(
    client: TestClient[Any, Any],
    grant: dict[str, object],
    *,
    credential: str = OWNER_CREDENTIAL,
    origin: str = FRAME_ORIGIN,
    parent: str = PARENT_ORIGIN,
) -> Any:
    return await client.post(
        "/v1/onboarding/frame/session",
        headers={"Origin": origin, "Content-Type": "application/json"},
        data=json.dumps(
            {
                "owner_credential": credential,
                "operation": grant["operation"],
                "parent_origin": parent,
                "parent_nonce": grant["parent_nonce"],
                "server_nonce": grant["server_nonce"],
            }
        ),
    )


@pytest.mark.asyncio
async def test_frame_documents_and_assets_have_exact_private_response_policy(
    frame_client: tuple[TestClient[Any, Any], FrameLifecycle],
) -> None:
    client, _lifecycle = frame_client
    setup = await client.get("/onboarding/setup")
    recovery = await client.get("/onboarding/recovery")
    script = await client.get("/onboarding/assets/secure-frame.js")
    stylesheet = await client.get("/onboarding/assets/secure-frame.css")

    for response in (setup, recovery, script, stylesheet):
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Cross-Origin-Resource-Policy"] == "same-site"
        assert "X-Frame-Options" not in response.headers
    csp = setup.headers["Content-Security-Policy"]
    assert f"frame-ancestors {PARENT_ORIGIN}" in csp
    assert "*" not in csp and "unsafe-inline" not in csp
    assert "script-src 'self'" in csp and "style-src 'self'" in csp
    assert "KloveSecureFrame" not in await script.text()
    assert "access_code" not in await script.text()
    assert "Set up a printer" in await setup.text()
    assert "Recover a printer connection" in await recovery.text()
    assert script.headers["Content-Security-Policy"] == "default-src 'none'; base-uri 'none'"
    assert stylesheet.content_type == "text/css"


@pytest.mark.asyncio
async def test_frame_challenge_uses_only_exact_cors_parent_evidence(
    frame_client: tuple[TestClient[Any, Any], FrameLifecycle],
) -> None:
    client, _lifecycle = frame_client
    accepted = await client.options(
        "/v1/onboarding/frame/challenge",
        headers={
            "Origin": PARENT_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "CoNtEnT-TyPe",
        },
    )
    assert accepted.status == 204
    assert accepted.headers["Access-Control-Allow-Origin"] == PARENT_ORIGIN
    assert accepted.headers["Vary"] == "Origin"
    assert accepted.headers["Access-Control-Allow-Headers"] == "Content-Type"

    no_requested_headers = await client.options(
        "/v1/onboarding/frame/challenge",
        headers={"Origin": PARENT_ORIGIN, "Access-Control-Request-Method": "POST"},
    )
    assert no_requested_headers.status == 204

    for headers in (
        {},
        {"Origin": "https://hostile.invalid", "Access-Control-Request-Method": "POST"},
        {"Origin": PARENT_ORIGIN, "Access-Control-Request-Method": "GET"},
        {
            "Origin": PARENT_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, x-secret",
        },
        {
            "Origin": PARENT_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, content-type",
        },
    ):
        response = await client.options("/v1/onboarding/frame/challenge", headers=headers)
        assert response.status == 403 and await response.json() == {"error": "frame_denied"}

    duplicate = await client.options(
        "/v1/onboarding/frame/challenge",
        headers=[
            ("Origin", PARENT_ORIGIN),
            ("Access-Control-Request-Method", "POST"),
            ("Access-Control-Request-Headers", "content-type"),
            ("Access-Control-Request-Headers", "content-type"),
        ],
    )
    assert duplicate.status == 403
    whitespace_header = cast(
        web.Request,
        SimpleNamespace(
            headers=CIMultiDict(
                {
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": " content-type",
                }
            )
        ),
    )
    assert not _challenge_preflight_is_exact(whitespace_header)

    missing_origin = await client.post("/v1/onboarding/frame/challenge", json={})
    assert missing_origin.status == 403
    malformed = await client.post(
        "/v1/onboarding/frame/challenge", headers={"Origin": PARENT_ORIGIN}, json={}
    )
    assert malformed.status == 400
    assert malformed.headers["Access-Control-Allow-Origin"] == PARENT_ORIGIN
    invalid_operation = await client.post(
        "/v1/onboarding/frame/challenge",
        headers={"Origin": PARENT_ORIGIN},
        json={
            "version": FRAME_PROTOCOL_VERSION,
            "type": FRAME_CHALLENGE_TYPE,
            "operation": "unknown",
            "parent_nonce": parent_nonce("d"),
        },
    )
    assert invalid_operation.status == 400

    first = await challenge(client, nonce=parent_nonce("e"))
    repeated = await client.post(
        "/v1/onboarding/frame/challenge",
        headers={"Origin": PARENT_ORIGIN},
        json={
            "version": FRAME_PROTOCOL_VERSION,
            "type": FRAME_CHALLENGE_TYPE,
            "operation": OnboardingOperation.CREATE.value,
            "parent_nonce": parent_nonce("e"),
        },
    )
    assert first["parent_nonce"] == parent_nonce("e")
    assert repeated.status == 403
    assert repeated.headers["Access-Control-Allow-Origin"] == PARENT_ORIGIN


@pytest.mark.asyncio
async def test_server_proof_binds_parent_frame_owner_session_and_lifecycle_audit_origin(
    frame_client: tuple[TestClient[Any, Any], FrameLifecycle],
) -> None:
    client, lifecycle = frame_client
    grant = await challenge(client)
    session = await frame_session(client, grant)
    document = await session.json()

    assert session.status == 201
    assert set(document) == {"csrf_token", "flow_nonce", "operation", "expires_in_seconds"}
    assert session.cookies[SESSION_COOKIE_NAME]["httponly"]
    assert session.cookies[SESSION_COOKIE_NAME]["secure"]
    assert session.cookies[SESSION_COOKIE_NAME]["samesite"] == "Strict"
    assert OWNER_CREDENTIAL not in await session.text()

    create = await client.post(
        "/v1/onboarding/printers",
        headers={
            "Origin": FRAME_ORIGIN,
            "Content-Type": "application/json",
            "Cookie": f"{SESSION_COOKIE_NAME}={session.cookies[SESSION_COOKIE_NAME].value}",
            "X-Klove-CSRF": document["csrf_token"],
            "Idempotency-Key": IDEMPOTENCY_KEY,
        },
        data=json.dumps(
            {
                "flow_nonce": document["flow_nonce"],
                "printer_uuid": PRINTER_UUID,
                "display_name": "Workshop printer",
                "endpoint": {
                    "url": "http://127.0.0.1:7125",
                    "allow_insecure_http": False,
                    "verify_tls": False,
                },
                "moonraker_credential": "m" * 32,
                "safety_profiles": [],
                "control_enabled": False,
                "dispatch_enabled": False,
            }
        ),
    )
    assert create.status == 201
    assert lifecycle.calls[-1].request_origin == PARENT_ORIGIN


@pytest.mark.asyncio
async def test_frame_session_and_handshake_replays_cancellation_and_hostile_input_fail_closed(
    frame_client: tuple[TestClient[Any, Any], FrameLifecycle],
) -> None:
    client, _lifecycle = frame_client
    malformed = await client.post(
        "/v1/onboarding/frame/session", headers={"Origin": FRAME_ORIGIN}, json={}
    )
    assert malformed.status == 400
    grant = await challenge(client)
    wrong_origin = await frame_session(client, grant, origin="https://hostile.invalid")
    assert wrong_origin.status == 403

    wrong_parent = await frame_session(client, grant, parent="https://hostile.invalid")
    assert wrong_parent.status == 403
    accepted = await frame_session(client, grant)
    assert accepted.status == 201
    replay = await frame_session(client, grant)
    assert replay.status == 403 and await replay.json() == {"error": "frame_denied"}

    grant = await challenge(client, nonce=parent_nonce("b"))
    wrong_credential = await frame_session(client, grant, credential="x" * 32)
    assert wrong_credential.status == 403 and await wrong_credential.json() == {
        "error": "owner_denied"
    }
    assert (await frame_session(client, grant)).status == 403

    capacity_grant = await challenge(client, nonce=parent_nonce("f"))
    capacity_denied = await frame_session(client, capacity_grant)
    assert capacity_denied.status == 403 and await capacity_denied.json() == {
        "error": "owner_denied"
    }

    grant = await challenge(client, nonce=parent_nonce("c"))
    cancelled = await client.post(
        "/v1/onboarding/frame/cancel",
        headers={"Origin": FRAME_ORIGIN, "Content-Type": "application/json"},
        data=json.dumps(
            {
                "operation": grant["operation"],
                "parent_origin": PARENT_ORIGIN,
                "parent_nonce": grant["parent_nonce"],
                "server_nonce": grant["server_nonce"],
            }
        ),
    )
    assert cancelled.status == 200 and await cancelled.json() == {"status": "cancelled"}
    assert (await frame_session(client, grant)).status == 403
    assert (
        await client.post(
            "/v1/onboarding/frame/cancel",
            headers={"Origin": "https://hostile.invalid", "Content-Type": "application/json"},
            json={
                "operation": grant["operation"],
                "parent_origin": PARENT_ORIGIN,
                "parent_nonce": grant["parent_nonce"],
                "server_nonce": grant["server_nonce"],
            },
        )
    ).status == 403
    replay_cancel = await client.post(
        "/v1/onboarding/frame/cancel",
        headers={"Origin": FRAME_ORIGIN, "Content-Type": "application/json"},
        json={
            "operation": grant["operation"],
            "parent_origin": PARENT_ORIGIN,
            "parent_nonce": grant["parent_nonce"],
            "server_nonce": grant["server_nonce"],
        },
    )
    assert replay_cancel.status == 403
    malformed_cancel = await client.post(
        "/v1/onboarding/frame/cancel", headers={"Origin": FRAME_ORIGIN}, json={}
    )
    assert malformed_cancel.status == 400
    assert (
        await client.post("/v1/onboarding/frame/cancel", headers={"Origin": FRAME_ORIGIN})
    ).status == 400


@pytest.mark.asyncio
async def test_frame_route_requires_matching_complete_composition() -> None:
    sessions = OwnerSessionStore(
        frozenset({PARENT_ORIGIN}),
        capacity=1,
        inactivity_timeout_seconds=900,
        absolute_timeout_seconds=1_800,
    )
    handshakes = FrameHandshakeStore(frozenset({PARENT_ORIGIN}), capacity=1)
    with pytest.raises(ValueError, match="secure frame routes require matching"):
        create_api(
            PrinterRegistry([]),
            BearerAuthenticator("a" * 32),
            FakeControls(),  # type: ignore[arg-type]
            owner_authenticator=OwnerCredentialAuthenticator(OWNER_CREDENTIAL),
            owner_sessions=sessions,
            lifecycle=object(),  # type: ignore[arg-type]
            frame_handshakes=handshakes,
        )


def test_frame_route_rejects_a_session_store_without_one_frame_origin() -> None:
    sessions = OwnerSessionStore(
        frozenset({PARENT_ORIGIN}),
        capacity=1,
        inactivity_timeout_seconds=900,
        absolute_timeout_seconds=1_800,
    )
    app = web.Application()
    app[owner_sessions_key] = sessions
    with pytest.raises(ValueError, match="exact configured frame origin"):
        install_secure_frame_routes(
            app, FrameHandshakeStore(frozenset({PARENT_ORIGIN}), capacity=1)
        )
