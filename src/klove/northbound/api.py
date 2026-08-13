"""Authenticated native Klove HTTP API."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from klove.domain.control import ControlIntent, ControlOperation, ControlStatus
from klove.domain.models import PrinterSnapshot
from klove.orchestration.control import ControlService
from klove.registry import PrinterRegistry
from klove.security.auth import BearerAuthenticator, authorize

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def create_api(
    registry: PrinterRegistry,
    authenticator: BearerAuthenticator,
    controls: ControlService,
) -> web.Application:
    """Build the native monitoring and typed-control application."""
    app = web.Application(client_max_size=16 * 1024)
    app[registry_key] = registry
    app[authenticator_key] = authenticator
    app[control_key] = controls
    app[ready_key] = Readiness()
    app.router.add_get("/health/live", liveness)
    app.router.add_get("/health/ready", readiness)
    app.router.add_get("/v1/printers", require_scope("printers:read", list_printers))
    app.router.add_get("/v1/printers/{printer_id}", require_scope("printers:read", get_printer))
    app.router.add_post(
        "/v1/printers/{printer_id}/commands/{operation}",
        require_scope("printers:control", control_printer),
    )
    return app


registry_key = web.AppKey("registry", PrinterRegistry)
authenticator_key = web.AppKey("authenticator", BearerAuthenticator)
control_key = web.AppKey("controls", ControlService)


@dataclass(slots=True)
class Readiness:
    """Mutable lifecycle state stored safely after aiohttp freezes the app."""

    ready: bool = False


ready_key = web.AppKey("ready", Readiness)


async def liveness(_request: web.Request) -> web.Response:
    """Return a content-free process liveness signal."""
    return web.json_response({"status": "ok"})


async def readiness(request: web.Request) -> web.Response:
    """Report whether application startup completed, without printer details."""
    ready = request.app[ready_key].ready
    return web.json_response(
        {"status": "ready" if ready else "starting"}, status=200 if ready else 503
    )


def require_scope(scope: str, handler: Handler) -> Handler:
    """Wrap a handler in exact bearer authentication and scope authorization."""

    async def guarded(request: web.Request) -> web.StreamResponse:
        authorization_values = request.headers.getall("Authorization", [])
        authorization = authorization_values[0] if len(authorization_values) == 1 else None
        principal = request.app[authenticator_key].authenticate(authorization)
        if not authorize(principal, scope):
            raise web.HTTPUnauthorized(
                headers={"WWW-Authenticate": 'Bearer realm="klove"'},
                text='{"error":"unauthorized"}',
                content_type="application/json",
            )
        return await handler(request)

    return guarded


async def list_printers(request: web.Request) -> web.Response:
    """Return all canonical snapshots."""
    snapshots = await request.app[registry_key].list()
    return web.json_response({"printers": [_snapshot_document(snapshot) for snapshot in snapshots]})


async def get_printer(request: web.Request) -> web.Response:
    """Return one canonical snapshot without leaking credentials or remote URLs."""
    snapshot = await request.app[registry_key].get(request.match_info["printer_id"])
    if snapshot is None:
        raise web.HTTPNotFound(text='{"error":"not_found"}', content_type="application/json")
    return web.json_response(_snapshot_document(snapshot))


async def control_printer(request: web.Request) -> web.Response:
    """Execute one strictly typed, state-bound, idempotent job control."""
    try:
        operation = ControlOperation(request.match_info["operation"])
    except ValueError:
        raise web.HTTPNotFound(
            text='{"error":"not_found"}', content_type="application/json"
        ) from None
    idempotency_values = request.headers.getall("Idempotency-Key", [])
    if len(idempotency_values) != 1 or not _canonical_uuid4(idempotency_values[0]):
        raise _bad_request()
    content_types = request.headers.getall("Content-Type", [])
    if len(content_types) != 1 or request.content_type != "application/json":
        raise _bad_request()
    try:
        payload = json.loads(
            (await request.read()).decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
        )
    except (UnicodeError, json.JSONDecodeError):
        raise _bad_request() from None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"state_token"}
        or not isinstance(payload["state_token"], str)
        or re.fullmatch(r"[0-9a-f]{64}", payload["state_token"]) is None
    ):
        raise _bad_request()
    result = await request.app[control_key].execute(
        ControlIntent(
            printer_id=request.match_info["printer_id"],
            operation=operation,
            state_token=payload["state_token"],
            idempotency_key=idempotency_values[0],
        )
    )
    status = 200
    if result.status is ControlStatus.DENIED:
        status = 409
    elif result.status is ControlStatus.OUTCOME_UNKNOWN:
        status = 202
    return web.json_response(result.model_dump(mode="json"), status=status)


def _snapshot_document(snapshot: PrinterSnapshot) -> dict[str, object]:
    document = snapshot.model_dump(mode="json", exclude={"control_revision", "epoch", "job"})
    document["state_token"] = snapshot.state_token
    return document


def _canonical_uuid4(value: str) -> bool:
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.version == 4 and str(parsed) == value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise json.JSONDecodeError("duplicate object member", key, 0)
        result[key] = value
    return result


def _bad_request() -> web.HTTPBadRequest:
    return web.HTTPBadRequest(text='{"error":"invalid_request"}', content_type="application/json")
