"""Bounded browser routes for the Klove embedded setup and recovery frame."""

from __future__ import annotations

import html
import json
from http import HTTPStatus
from importlib import resources

from aiohttp import web

from klove.northbound.onboarding_api import (
    _SECURITY_HEADERS,
    _json_object,
    _one_header,
    owner_authenticator_key,
    owner_sessions_key,
)
from klove.security.frame_handshake import (
    FRAME_CHALLENGE_TYPE,
    FRAME_PROTOCOL_VERSION,
    FrameHandshakeDenied,
    FrameHandshakeStore,
)
from klove.security.owner_sessions import (
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_PATH,
    OnboardingOperation,
    OwnerSessionDenied,
)

frame_handshakes_key = web.AppKey("frame_handshakes", FrameHandshakeStore)

_FRAME_SESSION_PATH = "/v1/onboarding/frame/session"
_FRAME_CHALLENGE_PATH = "/v1/onboarding/frame/challenge"
_FRAME_CANCEL_PATH = "/v1/onboarding/frame/cancel"
_ASSET_PREFIX = "/onboarding/assets/"
_FRAME_HEADERS = {
    **_SECURITY_HEADERS,
    "Cross-Origin-Resource-Policy": "same-site",
    "Permissions-Policy": (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), microphone=(), "
        "payment=(), usb=()"
    ),
}


def install_secure_frame_routes(app: web.Application, handshakes: FrameHandshakeStore) -> None:
    """Install browser routes only when exact frame and parent origins are configured."""
    sessions = app[owner_sessions_key]
    if sessions.frame_origin is None:
        raise ValueError("secure frame routes require one exact configured frame origin")
    app[frame_handshakes_key] = handshakes
    app.router.add_options(_FRAME_CHALLENGE_PATH, frame_challenge_options)
    app.router.add_post(_FRAME_CHALLENGE_PATH, issue_frame_challenge)
    app.router.add_post(_FRAME_SESSION_PATH, issue_frame_session)
    app.router.add_post(_FRAME_CANCEL_PATH, cancel_frame_handshake)
    app.router.add_get("/onboarding/setup", setup_frame)
    app.router.add_get("/onboarding/recovery", recovery_frame)
    app.router.add_get(f"{_ASSET_PREFIX}secure-frame.js", secure_frame_javascript)
    app.router.add_get(f"{_ASSET_PREFIX}secure-frame.css", secure_frame_stylesheet)


async def frame_challenge_options(request: web.Request) -> web.Response:
    """Allow the one required non-credentialed parent CORS preflight exactly."""
    origin = _allowed_parent_origin(request)
    if origin is None or not _challenge_preflight_is_exact(request):
        return _frame_error(HTTPStatus.FORBIDDEN, "frame_denied")
    return web.Response(status=HTTPStatus.NO_CONTENT, headers=_cors_headers(origin))


async def issue_frame_challenge(request: web.Request) -> web.Response:
    """Mint one server nonce for a ready frame after exact parent-origin evidence."""
    origin = _allowed_parent_origin(request)
    if origin is None:
        return _frame_error(HTTPStatus.FORBIDDEN, "frame_denied")
    payload = await _json_object(request)
    operation = _frame_operation(payload)
    parent_nonce = payload.get("parent_nonce")
    if (
        set(payload) != {"version", "type", "operation", "parent_nonce"}
        or payload.get("version") != FRAME_PROTOCOL_VERSION
        or payload.get("type") != FRAME_CHALLENGE_TYPE
        or operation is None
        or not isinstance(parent_nonce, str)
    ):
        return _frame_error(HTTPStatus.BAD_REQUEST, "invalid_request", origin=origin)
    try:
        grant = request.app[frame_handshakes_key].issue(
            parent_origin=origin,
            parent_nonce=parent_nonce,
            operation=operation,
        )
    except FrameHandshakeDenied:
        return _frame_error(HTTPStatus.FORBIDDEN, "frame_denied", origin=origin)
    response = _json_response(
        {
            "version": FRAME_PROTOCOL_VERSION,
            "type": FRAME_CHALLENGE_TYPE,
            "operation": operation.value,
            "parent_nonce": parent_nonce,
            "server_nonce": grant.server_nonce,
            "expires_in_seconds": grant.expires_in_seconds,
        },
        status=HTTPStatus.CREATED,
    )
    response.headers.update(_cors_headers(origin))
    return response


async def issue_frame_session(request: web.Request) -> web.Response:
    """Consume the parent proof before exchanging a Klove owner credential for a session."""
    sessions = request.app[owner_sessions_key]
    if _one_header(request, "Origin") != sessions.frame_origin:
        return _frame_error(HTTPStatus.FORBIDDEN, "frame_denied")
    payload = await _json_object(request)
    operation = _frame_operation(payload)
    parent_origin = payload.get("parent_origin")
    parent_nonce = payload.get("parent_nonce")
    server_nonce = payload.get("server_nonce")
    credential = payload.get("owner_credential")
    if (
        set(payload)
        != {"owner_credential", "operation", "parent_origin", "parent_nonce", "server_nonce"}
        or operation is None
        or not isinstance(parent_origin, str)
        or not isinstance(parent_nonce, str)
        or not isinstance(server_nonce, str)
        or not isinstance(credential, str)
    ):
        return _frame_error(HTTPStatus.BAD_REQUEST, "invalid_request")
    try:
        request.app[frame_handshakes_key].consume(
            parent_origin=parent_origin,
            parent_nonce=parent_nonce,
            server_nonce=server_nonce,
            operation=operation,
        )
    except FrameHandshakeDenied:
        return _frame_error(HTTPStatus.FORBIDDEN, "frame_denied")
    if not request.app[owner_authenticator_key].authenticate(credential):
        return _frame_error(HTTPStatus.FORBIDDEN, "owner_denied")
    try:
        grant = sessions.issue_framed(parent_origin=parent_origin, operation=operation)
    except OwnerSessionDenied:
        return _frame_error(HTTPStatus.FORBIDDEN, "owner_denied")
    response = _json_response(
        {
            "csrf_token": grant.csrf_token,
            "flow_nonce": grant.flow_nonce,
            "operation": grant.operation.value,
            "expires_in_seconds": grant.max_age_seconds,
        },
        status=HTTPStatus.CREATED,
    )
    response.set_cookie(
        SESSION_COOKIE_NAME,
        grant.cookie_value,
        path=SESSION_COOKIE_PATH,
        secure=grant.cookie_secure,
        httponly=True,
        samesite="Strict",
        max_age=grant.max_age_seconds,
    )
    return response


async def cancel_frame_handshake(request: web.Request) -> web.Response:
    """Consume a live parent proof without issuing a browser session."""
    sessions = request.app[owner_sessions_key]
    if _one_header(request, "Origin") != sessions.frame_origin:
        return _frame_error(HTTPStatus.FORBIDDEN, "frame_denied")
    payload = await _json_object(request)
    operation = _frame_operation(payload)
    parent_origin = payload.get("parent_origin")
    parent_nonce = payload.get("parent_nonce")
    server_nonce = payload.get("server_nonce")
    if (
        set(payload) != {"operation", "parent_origin", "parent_nonce", "server_nonce"}
        or operation is None
        or not isinstance(parent_origin, str)
        or not isinstance(parent_nonce, str)
        or not isinstance(server_nonce, str)
    ):
        return _frame_error(HTTPStatus.BAD_REQUEST, "invalid_request")
    try:
        request.app[frame_handshakes_key].consume(
            parent_origin=parent_origin,
            parent_nonce=parent_nonce,
            server_nonce=server_nonce,
            operation=operation,
        )
    except FrameHandshakeDenied:
        return _frame_error(HTTPStatus.FORBIDDEN, "frame_denied")
    return _json_response({"status": "cancelled"}, status=HTTPStatus.OK)


async def setup_frame(request: web.Request) -> web.Response:
    """Render the original, bounded setup authorization frame."""
    return _frame_document(request, OnboardingOperation.CREATE, "Set up a printer")


async def recovery_frame(request: web.Request) -> web.Response:
    """Render the original, bounded recovery authorization frame."""
    return _frame_document(request, OnboardingOperation.UPDATE, "Recover a printer connection")


async def secure_frame_javascript(_request: web.Request) -> web.Response:
    """Serve the self-hosted frame protocol implementation without browser caching."""
    return _asset_response("secure-frame.js", "application/javascript")


async def secure_frame_stylesheet(_request: web.Request) -> web.Response:
    """Serve the self-hosted frame stylesheet without browser caching."""
    return _asset_response("secure-frame.css", "text/css")


def _frame_document(
    request: web.Request,
    operation: OnboardingOperation,
    heading: str,
) -> web.Response:
    origins = request.app[frame_handshakes_key].allowed_parent_origins
    encoded_origins = html.escape(json.dumps(sorted(origins), separators=(",", ":")), quote=True)
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Klove authorization</title>
  <link rel="stylesheet" href="{_ASSET_PREFIX}secure-frame.css">
  <script src="{_ASSET_PREFIX}secure-frame.js" defer></script>
</head>
<body>
  <main class="frame-shell" data-operation="{operation.value}"
    data-parent-origins="{encoded_origins}">
    <section class="authorization-card" aria-labelledby="frame-title">
      <p class="eyebrow">Klove secure connection</p>
      <h1 id="frame-title">{heading}</h1>
      <p id="frame-guidance">Waiting for the host application to establish a secure connection.</p>
      <form id="owner-session" novalidate>
        <label for="owner-credential">Klove owner credential</label>
        <input id="owner-credential" type="password" autocomplete="off" inputmode="text"
          spellcheck="false" maxlength="4096" required disabled>
        <button id="authorize" type="submit" disabled>Authorize this session</button>
      </form>
      <button id="cancel" class="secondary" type="button" disabled>Cancel</button>
      <p id="frame-status" role="status" aria-live="polite"></p>
      <p class="privacy-note">This authorization stays in Klove and is never sent to the host.</p>
    </section>
  </main>
</body>
</html>"""
    response = web.Response(text=document, content_type="text/html", charset="utf-8")
    response.headers.update(_FRAME_HEADERS)
    response.headers["Content-Security-Policy"] = _document_csp(origins)
    return response


def _asset_response(filename: str, content_type: str) -> web.Response:
    text = (
        resources.files("klove.northbound").joinpath("assets", filename).read_text(encoding="utf-8")
    )
    response = web.Response(text=text, content_type=content_type, charset="utf-8")
    response.headers.update(_FRAME_HEADERS)
    response.headers["Content-Security-Policy"] = "default-src 'none'; base-uri 'none'"
    return response


def _document_csp(origins: frozenset[str]) -> str:
    return "; ".join(
        (
            "default-src 'none'",
            "base-uri 'none'",
            "connect-src 'self'",
            "form-action 'self'",
            f"frame-ancestors {' '.join(sorted(origins))}",
            "frame-src 'none'",
            "img-src 'self'",
            "manifest-src 'none'",
            "object-src 'none'",
            "script-src 'self'",
            "style-src 'self'",
            "worker-src 'none'",
        )
    )


def _allowed_parent_origin(request: web.Request) -> str | None:
    origin = _one_header(request, "Origin")
    return (
        origin
        if origin is not None and origin in request.app[frame_handshakes_key].allowed_parent_origins
        else None
    )


def _challenge_preflight_is_exact(request: web.Request) -> bool:
    methods = request.headers.getall("Access-Control-Request-Method", [])
    headers = request.headers.getall("Access-Control-Request-Headers", [])
    if methods != ["POST"] or len(headers) > 1:
        return False
    if not headers:
        return True
    value = headers[0]
    if not value or value != value.strip():
        return False
    requested = [header.strip().casefold() for header in value.split(",")]
    return requested == ["content-type"]


def _frame_operation(payload: dict[str, object]) -> OnboardingOperation | None:
    value = payload.get("operation")
    if not isinstance(value, str):
        return None
    try:
        return OnboardingOperation(value)
    except ValueError:
        return None


def _cors_headers(origin: str) -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Allow-Methods": "POST",
        "Access-Control-Max-Age": "0",
        "Vary": "Origin",
    }


def _json_response(document: dict[str, object], *, status: HTTPStatus) -> web.Response:
    return web.json_response(document, status=status, headers=_SECURITY_HEADERS)


def _frame_error(
    status: HTTPStatus,
    code: str,
    *,
    origin: str | None = None,
) -> web.Response:
    response = _json_response({"error": code}, status=status)
    if origin is not None:
        response.headers.update(_cors_headers(origin))
    return response
