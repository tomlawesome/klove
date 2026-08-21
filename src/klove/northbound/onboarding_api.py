"""Strict secret-free owner onboarding HTTP routes."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from http import HTTPStatus
from typing import Any

from aiohttp import web
from pydantic import BaseModel, ValidationError

from klove.domain.onboarding import RegisteredPrinter, RegistryOperationKind
from klove.domain.onboarding_requests import (
    CreatePrinterRequest,
    DisablePrinterRequest,
    InspectPrinterRequest,
    LifecycleResultRequest,
    RemovePrinterRequest,
    RotateCompatibilityCredentialRequest,
    RotateMoonrakerCredentialRequest,
    UpdatePrinterRequest,
)
from klove.orchestration.onboarding import (
    LifecycleFailureCode,
    LifecycleServiceError,
    PrinterLifecycleService,
)
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

owner_authenticator_key = web.AppKey("owner_authenticator", OwnerCredentialAuthenticator)
owner_sessions_key = web.AppKey("owner_sessions", OwnerSessionStore)
lifecycle_key = web.AppKey("printer_lifecycle", PrinterLifecycleService)

_ACTOR = "owner"
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
_SAFE_RETRY_CODES = frozenset(
    {
        LifecycleFailureCode.CONFLICT,
        LifecycleFailureCode.BUSY,
        LifecycleFailureCode.STALE_REVISION,
        LifecycleFailureCode.INVALID_TRANSITION,
        LifecycleFailureCode.PROBE_FAILED,
        LifecycleFailureCode.PRINTER_FENCED,
        LifecycleFailureCode.FENCE_UNAVAILABLE,
    }
)


def install_onboarding_routes(app: web.Application) -> None:
    """Install the closed owner-session and lifecycle route set."""
    app.router.add_post("/v1/onboarding/session", issue_session)
    app.router.add_post("/v1/onboarding/inspect", inspect_printer)
    app.router.add_post("/v1/onboarding/printers", create_printer)
    app.router.add_put("/v1/onboarding/printers/{printer_uuid}", update_printer)
    app.router.add_post(
        "/v1/onboarding/printers/{printer_uuid}/rotate/moonraker",
        rotate_moonraker,
    )
    app.router.add_post(
        "/v1/onboarding/printers/{printer_uuid}/rotate/compatibility",
        rotate_compatibility,
    )
    app.router.add_post("/v1/onboarding/printers/{printer_uuid}/disable", disable_printer)
    app.router.add_delete("/v1/onboarding/printers/{printer_uuid}", remove_printer)
    app.router.add_post("/v1/onboarding/result", lifecycle_result)
    app.router.add_post("/v1/onboarding/cancel", cancel_session)


@web.middleware
async def onboarding_security_headers(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Redact onboarding HTTP failures and mark every response private."""
    if not request.path.startswith("/v1/onboarding"):
        return await handler(request)
    try:
        response = await handler(request)
    except web.HTTPRequestEntityTooLarge:
        response = _error(HTTPStatus.BAD_REQUEST, "invalid_request")
    except web.HTTPException as exc:
        if exc.status == HTTPStatus.METHOD_NOT_ALLOWED:
            response = _error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed")
        elif exc.status == HTTPStatus.NOT_FOUND:
            response = _error(HTTPStatus.NOT_FOUND, "not_found")
        else:
            response = _error(HTTPStatus.BAD_REQUEST, "invalid_request")
    for name, value in _SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


async def issue_session(request: web.Request) -> web.Response:
    """Exchange one independent owner credential for one bounded setup session."""
    payload = await _json_object(request)
    origin = _one_header(request, "Origin")
    if set(payload) != {"owner_credential", "operation"} or origin is None:
        return _error(HTTPStatus.BAD_REQUEST, "invalid_request")
    credential = payload["owner_credential"]
    try:
        operation = OnboardingOperation(payload["operation"])
    except (TypeError, ValueError):
        return _error(HTTPStatus.BAD_REQUEST, "invalid_request")
    if not isinstance(credential, str) or not request.app[owner_authenticator_key].authenticate(
        credential
    ):
        return _error(HTTPStatus.FORBIDDEN, "owner_denied")
    try:
        grant = request.app[owner_sessions_key].issue(origin, operation)
    except OwnerSessionDenied:
        return _error(HTTPStatus.FORBIDDEN, "owner_denied")
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


async def inspect_printer(request: web.Request) -> web.Response:
    payload = await _json_object(request)
    return await _run(
        request,
        payload,
        OnboardingOperation.INSPECT,
        InspectPrinterRequest,
        request.app[lifecycle_key].inspect,
        lambda evidence: {"identity": evidence.model_dump(mode="json")},
    )


async def create_printer(request: web.Request) -> web.Response:
    payload = await _json_object(request)
    return await _run_mutation(
        request,
        payload,
        OnboardingOperation.CREATE,
        CreatePrinterRequest,
        request.app[lifecycle_key].create,
        status=HTTPStatus.CREATED,
    )


async def update_printer(request: web.Request) -> web.Response:
    payload = await _json_object(request)
    return await _run_mutation(
        request,
        payload,
        OnboardingOperation.UPDATE,
        UpdatePrinterRequest,
        request.app[lifecycle_key].update,
        printer_uuid=request.match_info["printer_uuid"],
    )


async def rotate_moonraker(request: web.Request) -> web.Response:
    payload = await _json_object(request)
    return await _run_mutation(
        request,
        payload,
        OnboardingOperation.ROTATE_MOONRAKER,
        RotateMoonrakerCredentialRequest,
        request.app[lifecycle_key].rotate_moonraker,
        printer_uuid=request.match_info["printer_uuid"],
    )


async def rotate_compatibility(request: web.Request) -> web.Response:
    payload = await _json_object(request)
    return await _run_mutation(
        request,
        payload,
        OnboardingOperation.ROTATE_COMPATIBILITY,
        RotateCompatibilityCredentialRequest,
        request.app[lifecycle_key].rotate_compatibility,
        printer_uuid=request.match_info["printer_uuid"],
    )


async def disable_printer(request: web.Request) -> web.Response:
    payload = await _json_object(request)
    return await _run_mutation(
        request,
        payload,
        OnboardingOperation.DISABLE,
        DisablePrinterRequest,
        request.app[lifecycle_key].disable,
        printer_uuid=request.match_info["printer_uuid"],
    )


async def remove_printer(request: web.Request) -> web.Response:
    payload = await _json_object(request)
    return await _run_mutation(
        request,
        payload,
        OnboardingOperation.REMOVE,
        RemovePrinterRequest,
        request.app[lifecycle_key].remove,
        printer_uuid=request.match_info["printer_uuid"],
    )


async def lifecycle_result(request: web.Request) -> web.Response:
    payload = await _json_object(request)
    operation = _operation(payload)
    if operation is None or operation is OnboardingOperation.INSPECT:
        return _error(HTTPStatus.BAD_REQUEST, "invalid_request")
    registry_operation = RegistryOperationKind(operation.value)
    return await _run(
        request,
        payload,
        operation,
        LifecycleResultRequest,
        _async_result(request.app[lifecycle_key]),
        _public_printer,
        extra={"operation": registry_operation},
    )


async def cancel_session(request: web.Request) -> web.Response:
    payload = await _json_object(request)
    operation = _operation(payload)
    if operation is None or set(payload) != {"flow_nonce", "operation"}:
        return _error(HTTPStatus.BAD_REQUEST, "invalid_request")
    lease = _authorize(request, operation, payload.get("flow_nonce"))
    if lease is None:
        return _error(HTTPStatus.FORBIDDEN, "owner_denied")
    lease.invalidate()
    response = _json_response({"status": "cancelled"})
    response.del_cookie(SESSION_COOKIE_NAME, path=SESSION_COOKIE_PATH)
    return response


async def _run_mutation(  # noqa: PLR0913 -- all route bindings remain explicit.
    request: web.Request,
    payload: dict[str, Any],
    operation: OnboardingOperation,
    model: type[BaseModel],
    invoke: Callable[[Any], Awaitable[RegisteredPrinter]],
    *,
    printer_uuid: str | None = None,
    status: int = HTTPStatus.OK,
) -> web.Response:
    return await _run(
        request,
        payload,
        operation,
        model,
        invoke,
        _public_printer,
        printer_uuid=printer_uuid,
        status=status,
        idempotency=True,
    )


async def _run(  # noqa: PLR0913,PLR0917 -- all request-security inputs stay explicit.
    request: web.Request,
    payload: dict[str, Any],
    operation: OnboardingOperation,
    model: type[BaseModel],
    invoke: Callable[[Any], Awaitable[Any]],
    render: Callable[[Any], dict[str, Any]],
    *,
    printer_uuid: str | None = None,
    status: int = HTTPStatus.OK,
    idempotency: bool = False,
    extra: Mapping[str, object] | None = None,
) -> web.Response:
    flow_nonce = payload.pop("flow_nonce", None)
    lease = _authorize(request, operation, flow_nonce)
    if lease is None:
        return _error(HTTPStatus.FORBIDDEN, "owner_denied")
    origin = _one_header(request, "Origin")
    values: dict[str, object] = {**payload, **(extra or {})}
    profiles = values.get("safety_profiles")
    if isinstance(profiles, list):
        values["safety_profiles"] = tuple(profiles)
    if printer_uuid is not None:
        values["printer_uuid"] = printer_uuid
    if idempotency:
        key = _one_header(request, "Idempotency-Key")
        if key is None:
            lease.release()
            return _error(HTTPStatus.BAD_REQUEST, "invalid_request")
        values["idempotency_key"] = key
    values.update({"actor": _ACTOR, "request_origin": origin})
    try:
        typed = model.model_validate(values)
    except (ValidationError, ValueError):
        lease.release()
        return _error(HTTPStatus.BAD_REQUEST, "invalid_request")
    try:
        result = await invoke(typed)
    except LifecycleServiceError as exc:
        if exc.code in _SAFE_RETRY_CODES:
            lease.release()
        else:
            lease.invalidate()
        return _lifecycle_error(exc.code)
    except Exception:
        lease.invalidate()
        return _error(HTTPStatus.SERVICE_UNAVAILABLE, "internal_failure")
    lease.invalidate()
    response = _json_response(render(result), status=status)
    response.del_cookie(SESSION_COOKIE_NAME, path=SESSION_COOKIE_PATH)
    return response


def _authorize(
    request: web.Request,
    operation: OnboardingOperation,
    flow_nonce: object,
) -> OwnerSessionLease | None:
    if not isinstance(flow_nonce, str):
        return None
    try:
        return request.app[owner_sessions_key].authorize(
            cookie_headers=request.headers.getall("Cookie", []),
            csrf_headers=request.headers.getall(CSRF_HEADER_NAME, []),
            origin_headers=request.headers.getall("Origin", []),
            operation=operation,
            flow_nonce=flow_nonce,
        )
    except OwnerSessionDenied:
        return None


async def _json_object(request: web.Request) -> dict[str, Any]:
    content_types = request.headers.getall("Content-Type", [])
    if content_types != ["application/json"]:
        raise _invalid_request()
    try:
        payload = json.loads(
            (await request.read()).decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
    except (UnicodeError, json.JSONDecodeError):
        raise _invalid_request() from None
    if not isinstance(payload, dict):
        raise _invalid_request()
    return payload


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise json.JSONDecodeError("duplicate object member", key, 0)
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise json.JSONDecodeError("non-finite number", value, 0)


def _operation(payload: Mapping[str, object]) -> OnboardingOperation | None:
    value = payload.get("operation")
    if not isinstance(value, str):
        return None
    try:
        return OnboardingOperation(value)
    except ValueError:
        return None


def _one_header(request: web.Request, name: str) -> str | None:
    values = request.headers.getall(name, [])
    if len(values) != 1 or not values[0] or values[0] != values[0].strip():
        return None
    return values[0]


def _public_printer(record: RegisteredPrinter) -> dict[str, Any]:
    return {
        "printer": record.model_dump(
            mode="json",
            exclude={"moonraker_credential_ref", "compatibility_credential_ref"},
        )
    }


def _lifecycle_error(code: LifecycleFailureCode) -> web.Response:
    if code in {
        LifecycleFailureCode.CONFLICT,
        LifecycleFailureCode.BUSY,
        LifecycleFailureCode.STALE_REVISION,
        LifecycleFailureCode.INVALID_TRANSITION,
        LifecycleFailureCode.PRINTER_FENCED,
    }:
        status = HTTPStatus.CONFLICT
    elif code is LifecycleFailureCode.PROBE_FAILED:
        status = HTTPStatus.UNPROCESSABLE_ENTITY
    else:
        status = HTTPStatus.SERVICE_UNAVAILABLE
    return _error(status, code.value)


def _json_response(document: Mapping[str, object], *, status: int = HTTPStatus.OK) -> web.Response:
    return web.json_response(document, status=status, headers=_SECURITY_HEADERS)


def _error(status: int, code: str) -> web.Response:
    return _json_response({"error": code}, status=status)


def _invalid_request() -> web.HTTPBadRequest:
    return web.HTTPBadRequest()


def _async_result(
    lifecycle: PrinterLifecycleService,
) -> Callable[[LifecycleResultRequest], Awaitable[RegisteredPrinter]]:
    async def result(request: LifecycleResultRequest) -> RegisteredPrinter:
        return lifecycle.result(request)

    return result
