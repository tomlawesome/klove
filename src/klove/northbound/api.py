"""Authenticated, read-only native Klove HTTP API."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiohttp import web

from klove.domain.models import PrinterSnapshot
from klove.registry import PrinterRegistry
from klove.security.auth import BearerAuthenticator, authorize

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def create_api(
    registry: PrinterRegistry,
    authenticator: BearerAuthenticator,
) -> web.Application:
    """Build an application that deliberately exposes no mutating route."""
    app = web.Application(client_max_size=16 * 1024)
    app[registry_key] = registry
    app[authenticator_key] = authenticator
    app[ready_key] = Readiness()
    app.router.add_get("/health/live", liveness)
    app.router.add_get("/health/ready", readiness)
    app.router.add_get("/v1/printers", require_scope("printers:read", list_printers))
    app.router.add_get("/v1/printers/{printer_id}", require_scope("printers:read", get_printer))
    return app


registry_key = web.AppKey("registry", PrinterRegistry)
authenticator_key = web.AppKey("authenticator", BearerAuthenticator)


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


def _snapshot_document(snapshot: PrinterSnapshot) -> dict[str, object]:
    document = snapshot.model_dump(mode="json", exclude={"epoch"})
    document["state_token"] = snapshot.state_token
    return document
