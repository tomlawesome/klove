import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from klove.domain.control import ControlIntent, ControlOperation, ControlResult, ControlStatus
from klove.northbound.api import create_api, owner_authenticator_key, owner_sessions_key, ready_key
from klove.orchestration.admission import PrinterAdmissionGates
from klove.orchestration.control import ControlService
from klove.registry import PrinterRegistry
from klove.security.auth import BearerAuthenticator
from klove.security.owner_sessions import OwnerCredentialAuthenticator, OwnerSessionStore

TOKEN = "a" * 32
KEY = "00000000-0000-4000-8000-000000000000"


@pytest.fixture
async def client() -> AsyncIterator[TestClient[Any, Any]]:
    registry = PrinterRegistry(["voron"])
    controls = ControlService(
        registry,
        {},
        confirmation_timeout_seconds=1,
        poll_interval_seconds=0.1,
        idempotency_capacity=10,
        admissions=PrinterAdmissionGates(),
    )
    app = create_api(registry, BearerAuthenticator(TOKEN), controls)
    async with TestClient(TestServer(app)) as result:
        yield result


@pytest.mark.asyncio
async def test_health_is_content_free_and_tracks_startup(client: TestClient[Any, Any]) -> None:
    live = await client.get("/health/live")
    starting = await client.get("/health/ready")
    client.app[ready_key].ready = True
    ready = await client.get("/health/ready")

    assert live.status == 200 and await live.json() == {"status": "ok"}
    assert starting.status == 503 and await starting.json() == {"status": "starting"}
    assert ready.status == 200 and await ready.json() == {"status": "ready"}


@pytest.mark.asyncio
async def test_printer_routes_fail_closed_and_return_only_snapshots(
    client: TestClient[Any, Any],
) -> None:
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        response = await client.get("/v1/printers", headers=headers)
        assert response.status == 401
        assert response.headers["WWW-Authenticate"] == 'Bearer realm="klove"'
        assert json.loads(await response.text()) == {"error": "unauthorized"}

    headers = {"Authorization": f"Bearer {TOKEN}"}
    listing = await client.get("/v1/printers", headers=headers)
    detail = await client.get("/v1/printers/voron", headers=headers)
    missing = await client.get("/v1/printers/missing", headers=headers)

    assert listing.status == 200
    assert (await listing.json())["printers"][0]["printer_id"] == "voron"
    detail_document = await detail.json()
    assert detail.status == 200 and detail_document["phase"] == "offline"
    assert len(detail_document["state_token"]) == 64
    assert "epoch" not in detail_document
    assert "control_revision" not in detail_document
    assert "job" not in detail_document
    assert missing.status == 404 and await missing.json() == {"error": "not_found"}


@pytest.mark.asyncio
async def test_duplicate_authorization_headers_are_rejected(client: TestClient[Any, Any]) -> None:
    response = await client.get(
        "/v1/printers",
        headers=[
            ("Authorization", f"Bearer {TOKEN}"),
            ("Authorization", f"Bearer {TOKEN}"),
        ],
    )

    assert response.status == 401
    assert await response.json() == {"error": "unauthorized"}


def test_owner_security_is_configured_as_one_independent_pair() -> None:
    registry = PrinterRegistry([])
    controls = FakeControls()
    owner_authenticator = OwnerCredentialAuthenticator("o" * 32)
    owner_sessions = OwnerSessionStore(
        frozenset({"https://grove.example.invalid"}),
        capacity=1,
        inactivity_timeout_seconds=900,
        absolute_timeout_seconds=1_800,
    )
    app = create_api(
        registry,
        BearerAuthenticator(TOKEN),
        controls,  # type: ignore[arg-type]
        owner_authenticator=owner_authenticator,
        owner_sessions=owner_sessions,
    )
    assert app[owner_authenticator_key] is owner_authenticator
    assert app[owner_sessions_key] is owner_sessions

    for owner, sessions in ((owner_authenticator, None), (None, owner_sessions)):
        with pytest.raises(ValueError, match="configured together"):
            create_api(
                registry,
                BearerAuthenticator(TOKEN),
                controls,  # type: ignore[arg-type]
                owner_authenticator=owner,
                owner_sessions=sessions,
            )


class FakeControls:
    def __init__(self) -> None:
        self.status = ControlStatus.CONFIRMED
        self.intents: list[ControlIntent] = []

    async def execute(self, intent: ControlIntent) -> ControlResult:
        self.intents.append(intent)
        return ControlResult(operation=intent.operation, status=self.status, code=self.status)


@pytest.fixture
async def control_client() -> AsyncIterator[tuple[TestClient[Any, Any], FakeControls]]:
    registry = PrinterRegistry(["voron"])
    controls = FakeControls()
    app = create_api(
        registry,
        BearerAuthenticator(TOKEN, scopes=frozenset({"printers:read", "printers:control"})),
        controls,  # type: ignore[arg-type]
    )
    async with TestClient(TestServer(app)) as result:
        yield result, controls


def command_headers(**updates: str) -> dict[str, str]:
    result = {
        "Authorization": f"Bearer {TOKEN}",
        "Idempotency-Key": KEY,
        "Content-Type": "application/json",
    }
    result.update(updates)
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", list(ControlOperation))
async def test_control_route_forwards_exact_typed_intent(
    control_client: tuple[TestClient[Any, Any], FakeControls],
    operation: ControlOperation,
) -> None:
    client, controls = control_client
    state_token = "a" * 64
    response = await client.post(
        f"/v1/printers/voron/commands/{operation}",
        headers=command_headers(),
        data=json.dumps({"state_token": state_token}),
    )

    assert response.status == 200
    assert await response.json() == {
        "operation": operation,
        "status": ControlStatus.CONFIRMED,
        "code": ControlStatus.CONFIRMED,
    }
    assert controls.intents == [
        ControlIntent(
            printer_id="voron",
            operation=operation,
            state_token=state_token,
            idempotency_key=KEY,
        )
    ]


@pytest.mark.asyncio
async def test_control_route_maps_confirmed_denied_and_unknown_results(
    control_client: tuple[TestClient[Any, Any], FakeControls],
) -> None:
    client, controls = control_client
    for expected_status, status in (
        (200, ControlStatus.CONFIRMED),
        (409, ControlStatus.DENIED),
        (202, ControlStatus.OUTCOME_UNKNOWN),
    ):
        controls.status = status
        response = await client.post(
            "/v1/printers/voron/commands/pause",
            headers=command_headers(),
            data=json.dumps({"state_token": "a" * 64}),
        )
        assert response.status == expected_status
        assert (await response.json())["status"] == status
    assert len(controls.intents) == 3


@pytest.mark.asyncio
async def test_control_requires_scope_and_known_operation(
    client: TestClient[Any, Any],
    control_client: tuple[TestClient[Any, Any], FakeControls],
) -> None:
    unauthorized = await client.post(
        "/v1/printers/voron/commands/pause",
        headers=command_headers(),
        data=json.dumps({"state_token": "a" * 64}),
    )
    authorized, _controls = control_client
    unknown = await authorized.post(
        "/v1/printers/voron/commands/home",
        headers=command_headers(),
        data=json.dumps({"state_token": "a" * 64}),
    )

    assert unauthorized.status == 401
    assert unknown.status == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}, b"{}"),
        (command_headers(**{"Idempotency-Key": "bad"}), b"{}"),
        (
            command_headers(**{"Idempotency-Key": "00000000-0000-5000-8000-000000000000"}),
            b"{}",
        ),
        (
            command_headers(**{"Idempotency-Key": "00000000-0000-4000-8000-00000000000A"}),
            b"{}",
        ),
        (
            {
                "Authorization": f"Bearer {TOKEN}",
                "Idempotency-Key": KEY,
                "Content-Type": "text/plain",
            },
            b"{}",
        ),
        (command_headers(), b"{"),
        (command_headers(), b"\xff"),
        (command_headers(), b'{"state_token":"a","state_token":"b"}'),
        (command_headers(), b"[]"),
        (command_headers(), b'{"state_token":"a"}'),
        (command_headers(), b'{"state_token":1}'),
        (
            command_headers(),
            b'{"state_token":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","extra":1}',
        ),
    ],
)
async def test_malformed_control_requests_never_reach_service(
    control_client: tuple[TestClient[Any, Any], FakeControls],
    headers: dict[str, str],
    body: bytes,
) -> None:
    client, controls = control_client
    response = await client.post("/v1/printers/voron/commands/pause", headers=headers, data=body)

    assert response.status == 400
    assert await response.json() == {"error": "invalid_request"}
    assert controls.intents == []


@pytest.mark.asyncio
async def test_duplicate_control_headers_are_rejected(
    control_client: tuple[TestClient[Any, Any], FakeControls],
) -> None:
    client, controls = control_client
    common = [("Authorization", f"Bearer {TOKEN}")]
    body = json.dumps({"state_token": "a" * 64})
    duplicate_key = await client.post(
        "/v1/printers/voron/commands/pause",
        headers=[
            *common,
            ("Idempotency-Key", KEY),
            ("Idempotency-Key", KEY),
            ("Content-Type", "application/json"),
        ],
        data=body,
    )
    duplicate_type = await client.post(
        "/v1/printers/voron/commands/pause",
        headers=[
            *common,
            ("Idempotency-Key", KEY),
            ("Content-Type", "application/json"),
            ("Content-Type", "application/json"),
        ],
        data=body,
    )

    assert duplicate_key.status == 400
    assert duplicate_type.status == 400
    assert controls.intents == []
