# Secure embedded-frame boundary

#77 — secure frame implements only the browser authorization boundary required before
setup or recovery work may begin. It does not collect printer details, invoke
Moonraker, create a printer, expose a dashboard or controls, or hand a Grove
create bundle to a parent. Those workflows and the completion handoff are
separate slices.

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
security headers, CORS decoding, cancellation, replay, and the separate
parent/request-origin bindings. `tests/browser/secure-frame.spec.mjs` serves
the shipped Klove JS/CSS through a temporary protocol harness to exercise a
real Chromium parent/frame relationship, hostile and out-of-flow messages,
expiry/restart/cancellation/parallel flows, top-level and cross-site refusal,
privacy, keyboard operation, semantics, and responsive embedding. The harness
is independently authored test infrastructure, not Grove code.

Run browser verification with `npm ci`, `npx playwright install chromium`,
and `sh scripts/test-browser.sh`. The lockfile records the exact official
`@playwright/test` 1.62.1 package and integrity values; its direct and
transitive Playwright packages declare Apache-2.0, while its Darwin-only
optional `fsevents` dependency declares MIT. The CI browser job installs the
lockfile-pinned Chromium revision explicitly and does not upload Playwright
screenshots or traces. Local failure artifacts stay in ignored paths and must
not be committed.
