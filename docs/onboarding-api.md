# Protected onboarding API contract

Status: issue #72 — lifecycle routes implements the secret-free lifecycle HTTP
boundary. Issue #73 — runtime handoff remains responsible for activating a
committed result in the running fleet.

## Session exchange

`POST /v1/onboarding/session` accepts exactly one `application/json` object
containing `owner_credential` and one closed lifecycle `operation`. The raw
request must contain exactly one configured `Origin`. Authentication uses only
the independent owner credential; neither the native bearer token nor a Grove
login grants this scope.

Success returns the flow nonce, CSRF token, operation, and absolute expiry. The
session value appears only in the required `Secure`, `HttpOnly`,
`SameSite=Strict`, `/v1/onboarding` cookie. It is absent from the JSON body and
application logs. Every response is `no-store`, `no-referrer`, and `nosniff`.

## Protected routes

Every route below requires exactly one raw Cookie, `X-Klove-CSRF`, `Origin`,
and `Content-Type: application/json` header. The body includes the exact flow
nonce. Mutations also require one canonical UUIDv4 `Idempotency-Key` header.
Unknown, missing, duplicate, coerced, non-finite, oversized, or out-of-flow
input is denied before orchestration.

| Operation | Method and path |
| --- | --- |
| inspect | `POST /v1/onboarding/inspect` |
| create | `POST /v1/onboarding/printers` |
| update | `PUT /v1/onboarding/printers/{printer_uuid}` |
| rotate Moonraker credential | `POST /v1/onboarding/printers/{printer_uuid}/rotate/moonraker` |
| rotate compatibility credential | `POST /v1/onboarding/printers/{printer_uuid}/rotate/compatibility` |
| disable | `POST /v1/onboarding/printers/{printer_uuid}/disable` |
| remove | `DELETE /v1/onboarding/printers/{printer_uuid}` |
| committed result | `POST /v1/onboarding/result` |
| cancel session | `POST /v1/onboarding/cancel` |

The server derives the audit actor and exact request origin. It never accepts
either from browser JSON. Each lifecycle route calls only the typed
`PrinterLifecycleService`; it does not call a runtime supervisor or actuator.
Result recovery binds operation, printer UUID, idempotency key, actor, and
origin, finalizes any safe pending credential cleanup, and never repeats a
direct probe.

## Privacy and terminal behavior

Inspection returns only validated identity/capability evidence. Mutation and
result responses return a public registry record with both opaque credential
references removed. Moonraker credentials, compatibility credentials, raw
probe bodies, cookie values, and secret references are never JSON response or
log fields. Errors contain one bounded code only.

A successful operation, result, or cancellation invalidates its session and
expires the cookie. Safe pre-commit conflicts release the exclusive lease for
an exact retry. Storage or internal uncertainty invalidates it; durable result
recovery requires a new session. Process restart invalidates every session but
preserves mutation idempotency and committed result recovery in the registry.
