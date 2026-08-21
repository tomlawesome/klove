# Secure embedded-frame boundary

#77 — secure frame establishes the browser authorization boundary. #78 — setup
and recovery flow extends it with only the owner choices needed for canonical
lifecycle operations. It remains neither a dashboard nor a printer-control
surface, and #79 — completion handoff remains a separate slice.

## Deployment boundary

Embedded routes are disabled unless onboarding is enabled and configuration
sets one exact `frame_origin`. Each configured `allowed_grove_origins` member
and `frame_origin` must use the same canonical scheme and host while remaining
different origins (normally different HTTPS ports). This deliberately stricter
rule is an enforceable subset of ADR 0006's same-trusted-site requirement: it
avoids guessing registrable domains and guarantees that `SameSite=Strict`
cookies and `Cross-Origin-Resource-Policy: same-site` have compatible browser
semantics. Loopback HTTP is available only through the existing explicit
development flag and exact same-loopback-host configuration.

For example, a production configuration can use a dedicated Klove HTTPS port
on the Grove host:

```toml
[onboarding]
enabled = true
owner_credential_file = "/run/secrets/klove-owner"
allowed_grove_origins = ["https://console.example.test"]
frame_origin = "https://console.example.test:8443"
```

The parent must use an iframe with only `allow-scripts`, `allow-forms`, and
`allow-same-origin`. The last permission preserves Klove's own distinct origin;
it does not grant parent DOM access because the two configured origins differ.
The parent must not grant popups, downloads, top navigation, presentation,
pointer lock, or a broader sandbox capability.

## Protocol and session proof

The setup and recovery documents are fixed routes. They refuse top-level use;
the owner-credential form remains disabled until this exact sequence completes:

1. The configured parent sends the version 1 `klove.frame.init` message to the
   iframe's exact origin. It contains the fixed operation and one 256-bit
   parent nonce.
2. The frame accepts only an event whose `source` is `window.parent` and whose
   origin is one configured parent. It strictly decodes the complete schema and
   replies with `klove.frame.ready` using that exact origin.
3. The parent verifies the ready message's iframe window reference, origin,
   version, operation, and nonce. It then requests a CORS challenge from Klove.
   Only an exact configured parent origin can make that request; CORS permits
   only `Content-Type` and `POST`.
4. Klove returns a short-lived, one-time server nonce bound to the parent
   origin, operation, and parent nonce. The parent strictly decodes that
   response and sends a separate `klove.frame.challenge` message containing
   only the five fields accepted by the frame. Response-only fields are never
   forwarded.
5. The frame rechecks the parent window, origin, version, operation, parent
   nonce, server nonce, state order, and exact schema. It then submits the
   owner credential only to Klove. The server consumes the proof before
   credential exchange; replay, expiration, cancellation, restart, altered
   origin, and parallel reuse deny. A consumed parent nonce remains tombstoned
   through its challenge expiry, including for capacity accounting.

Klove records distinct exact origins for the session: the configured Grove
parent origin and the configured Klove frame request origin. Protected browser
requests must carry the latter in their `Origin` header with the existing
session cookie and CSRF token; lifecycle audit evidence retains the former.
This avoids treating a configured parent origin as an API request origin while
preserving the owner-session contract. Sessions remain in memory only and
retain the existing 15-minute inactivity and 30-minute absolute bounds.

Before authorization, Cancel consumes the live one-time server proof. After
authorization, it uses the existing CSRF-protected session cancellation route.
After either cancellation or a failed owner exchange the controls remain
disabled. A new challenge is rejected once a frame is pending, authorized, or
cancelled.

## Bounded setup and recovery actions

Each frame route is fixed to exactly one lifecycle operation and its matching
owner session. `/onboarding/setup` creates one printer; `/onboarding/recovery`
repairs one endpoint/profile binding; the four recovery subroutes rotate the
Moonraker credential, rotate private compatibility access, disable, or remove
one printer. They never present a printer list, monitoring status, queue,
generic settings editor, or controls.

An endpoint displayed or supplied by discovery is advisory only. The owner may
also enter one exact manual HTTP(S) origin. The browser calls no Moonraker
address: create, update, and Moonraker-credential rotation call only their
matching protected lifecycle route, whose canonical service repeats the direct
bounded probe immediately before commit. The frame never calls `inspect` from a
create, update, or rotation session; a future standalone inspection screen
would require its own terminal `inspect` session.

The create and update screens accept one exact UUID, name, endpoint, optional
Klipper safety-profile binding, and explicit control/dispatch opt-ins. The
profile fields are sent as the typed canonical profile; dispatch remains denied
unless its profile and fresh positive capability evidence are present. Recovery
screens require the exact registered UUID and current revision supplied by the
owner. Compatibility rotation, disablement, and removal each require an
explicit local acknowledgement; the lifecycle API remains the authorization
and transition authority. Successful lifecycle
responses display only the one public record needed to confirm the operation;
they never expose credentials, opaque references, raw probe material, or a
compatibility access value.

An owner session is one operation only. Mutation idempotency is generated in
transient frame memory, not browser storage, and the required secure UUIDs are
generated before the owner-session exchange. A safe retry must reuse the same
exact submitted evidence; changing evidence requires a new secure connection.
The frame clears a submitted Moonraker credential input immediately after
starting its same-origin request. Cancellation, terminal success, and uncertain
cancellation remove lifecycle form DOM, clear owner/Moonraker inputs, and
discard local session and nonce material. It creates no completion message and
never sends lifecycle, Moonraker, or compatibility data through `postMessage`.

Before it can issue a lifecycle request, the frame rejects malformed UUIDs,
revisions, exact text, canonical origins, Moonraker credentials, and safety
profile fields locally. These locally correctable errors neither dispatch an
API request nor consume the exact owner session; the owner can correct the
fields or cancel that same session. Any Moonraker credential input is cleared
on a local validation failure as well.

## Browser policy and privacy

Frame documents emit an exact, route-specific CSP with only configured
`frame-ancestors`, self-hosted scripts/styles/connections, no inline source,
and no wildcard. Documents and assets emit `Cross-Origin-Resource-Policy:
same-site`, `Cache-Control: no-store`, `Referrer-Policy: no-referrer`,
`X-Content-Type-Options: nosniff`, and a restrictive Permissions Policy.
`X-Frame-Options` is intentionally absent because it cannot express the
approved exact-origin allowlist.

The original Klove HTML, CSS, and JavaScript use system fonts and contain no
Grove code, branding, visual assets, wording, source-derived test data, or
third-party runtime asset. They register no service worker and use no local or
session storage, analytics, telemetry, popups, downloads, or URL session
material. Owner credentials, session cookies, CSRF values, flow nonces,
Moonraker values, compatibility credentials, and future completion values are
not sent through `postMessage` or rendered in the parent. In particular, this
slice contains no completion-message encoder or access-code handoff.

## Verification and browser dependency

`tests/unit/test_secure_frame.py` exercises the actual aiohttp routes,
security headers, CORS decoding, cancellation, replay, all fixed lifecycle
frame documents, and the separate parent/request-origin bindings.
`tests/browser/secure-frame.spec.mjs` serves the shipped Klove JS/CSS through a
temporary protocol harness to exercise a real Chromium parent/frame
relationship, hostile and out-of-flow messages, lifecycle errors, restart,
cancellation, all bounded operation forms, privacy, keyboard operation,
semantics, and responsive embedding. The harness is independently authored
test infrastructure, not Grove code.

Run browser verification with `npm ci`, `npx playwright install chromium`,
and `sh scripts/test-browser.sh`. The lockfile records the exact official
`@playwright/test` 1.62.1 package and integrity values; its direct and
transitive Playwright packages declare Apache-2.0, while its Darwin-only
optional `fsevents` dependency declares MIT. The CI browser job installs the
lockfile-pinned Chromium revision explicitly and does not upload Playwright
screenshots or traces. Local failure artifacts stay in ignored paths and must
not be committed.
