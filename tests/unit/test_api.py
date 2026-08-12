import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from klove.northbound.api import create_api, ready_key
from klove.registry import PrinterRegistry
from klove.security.auth import BearerAuthenticator

TOKEN = "a" * 32


@pytest.fixture
async def client() -> AsyncIterator[TestClient[Any, Any]]:
    app = create_api(PrinterRegistry(["voron"]), BearerAuthenticator(TOKEN))
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
