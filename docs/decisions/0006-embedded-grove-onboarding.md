# ADR 0006: accept embedded onboarding and a minimal Grove KLOVE type

Status: accepted

Date: 2026-08-14

## Context

ADR 0002 keeps Klove headless and automation-first because Grove owns normal
printer interaction and controller evidence is safer than operator
transcription. That boundary still leaves a small set of irreducible setup and
recovery actions: identifying an endpoint when discovery is unavailable or
ambiguous, supplying a Moonraker credential directly to Klove, naming a
printer, confirming an exact discovered safety-profile binding and opt-in, and
later rotating credentials, disabling, removing, or recovering that binding.
Requiring per-printer TOML edits for those actions is not an acceptable product
workflow.

Current Grove is Bambu-specific below its fleet UI. At the reviewed revision,
its create schema accepts a 1–100 character name, an uppercased 1–50 character
serial, a DNS host or IPv4 address, an optional model, and a 1–20 character
access code. The model is not cosmetic: Grove uses it for file matching,
scheduling, images, maintenance, firmware, AMS, enclosure, fan, drying, and
other hardware feature decisions. Assigning a Klipper printer an existing
Bambu model would therefore display false capabilities and could route
unsupported commands.

A broad provider abstraction and native Klove backend inside Grove would avoid
Bambu-shaped transport, but it would also make Grove maintain Klipper-specific
API, event, state, scheduling, and migration contracts. That maintenance
surface is not acceptable. Grove already has an Add Printer workflow and can
host a bounded embedded Web UI, so the smaller sustainable boundary is one
explicit printer type plus a Klove-owned setup surface.

The supported Grove source reviewed for this decision is commit
`cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4`. Klove has read-only access to that
upstream repository. Any Grove change must be developed in a fork and proposed
upstream through its normal contribution process; Klove must not depend on an
unmerged private fork.

## Decision

### Product and ownership boundary

1. Klove owns Moonraker discovery and manual endpoint entry, Moonraker
   credentials, direct identity and capability probes, canonical printer UUIDs,
   safety-profile binding, control/dispatch opt-in, runtime registry state,
   compatibility credentials, and setup/recovery behavior.
2. Grove owns normal printer and queue interaction. Its only Klove-specific
   additions are an explicit `KLOVE` printer type displayed as **Klipper via
   Klove**, a custom Add Printer entry, an embedded Klove frame, conservative
   feature gates, and a narrow handoff into Grove's existing printer-create
   path.
3. Grove never receives a Moonraker URL credential, API key, password, probe
   transcript, configuration body, safety-profile evidence, or Klipper command
   semantics. Klove never asks Grove to infer capabilities from a name or model.
4. The Klove browser surface is setup and recovery only. It has no dashboard,
   monitoring view, queue, print control, generic configuration editor, file
   browser, terminal, or duplicate Grove workflow. A standalone rendering of
   the same route may support recovery when embedding is unavailable, but it is
   not promoted as a second application.
5. Existing file configuration remains a deployment/bootstrap input for such
   items as listeners, state paths, secret references, and allowed Grove
   origins. The normal product path for adding or changing printers is the
   runtime registry accepted here and tracked in issue #58, not per-printer
   TOML.

### Registry and credentials

6. One versioned SQLite/WAL registry owns canonical printer identity, exact
   endpoints, discovered evidence, confirmed safety-profile bindings, lifecycle
   state, and opaque credential references. There is no second UI-specific or
   fleet-specific registry.
7. Klove's Moonraker credentials and generated Grove-compatibility access-code
   copies live in owner-only secret storage outside tracked configuration and
   the registry. Grove necessarily stores its access-code copy in the existing
   protected printer credential field, which is not exposed to read-only roles.
   Neither secret is written to URLs, browser storage, telemetry, screenshots,
   exceptions, or logs. Backup, rotation, disable, removal, and restore preserve
   unresolved operation fences and fail closed on a missing or mismatched secret
   reference.
8. Discovery is advisory. Adding a printer requires a direct bounded Moonraker
   probe and exact positive identity, capability, endpoint, and safety-profile
   evidence. An operator confirmation records intent but cannot replace, widen,
   or manufacture that evidence. Ambiguous discovery, identity drift, unknown
   capabilities, stale evidence, or a collision is a denial.

### Browser and frame security

9. Setup and recovery require an independently authenticated Klove owner scope.
   A Grove login or private-network location is not Klove authorization. The
   owner credential is submitted only to Klove over authenticated HTTPS and is
   exchanged for a short-lived server-side setup session. The credential itself
   never crosses `postMessage`, enters Grove, or persists in browser storage.
10. A setup session is bound to one exact configured Grove origin, one browser
    flow nonce, and one operation class. It expires after at most 15 minutes of
    inactivity and 30 minutes absolutely, is invalidated on completion or
    cancellation, cannot authorize runtime printer actions, and is never
    accepted from a URL. Replay, parallel use, origin change, or privilege
    mismatch fails closed. The standalone recovery route uses the same Klove
    authentication and session rules without granting an embedding exception.
11. The supported embedded deployment uses distinct Grove and Klove origins on
    the same trusted HTTPS site so Klove can use a `Secure`, `HttpOnly`,
    path-scoped, `SameSite=Strict` session cookie without depending on third-party
    cookies. Loopback HTTP is permitted only in explicit development mode. A
    deployment that cannot meet this browser boundary uses the standalone
    recovery route until a separately reviewed session transport exists.
12. Klove emits a route-specific Content Security Policy whose
    `frame-ancestors` contains only configured exact Grove origins. Its scripts,
    styles, images, forms, and connections are self-hosted and restricted to the
    minimum required origins; inline script and wildcard sources are absent.
    Grove gives the frame only the sandbox capabilities needed for scripts,
    forms, and its distinct origin. Popups, downloads, top navigation,
    presentation, pointer lock, and parent DOM access remain disabled.
13. Every state-changing browser request requires the exact session, an
    unguessable CSRF token, and an exact allowed `Origin`; missing or multiple
    headers are denied. Setup responses use `Cache-Control: no-store`, a
    no-referrer policy, no MIME sniffing, and redacted bounded errors. The UI
    contains no third-party assets, analytics, telemetry, or service worker.

### Completion contract

14. Parent and frame communicate only after an explicit ready/nonce handshake.
    Each side checks the other window reference and exact configured origin,
    uses an exact `targetOrigin`, and strictly decodes a versioned schema that
    rejects unknown, missing, duplicate, malformed, oversized, or out-of-flow
    messages. Wildcard origins are prohibited.
15. A successful version 1 completion contains only the flow nonce and this
    Grove create bundle:

    - `name`: the confirmed display name, 1–100 characters;
    - `serial_number`: `KLOVE-` followed by the uppercase canonical UUID, 42
      characters in total and stable for that printer;
    - `ip_address`: the Klove compatibility DNS host or IPv4 address only, with
      no scheme, port, path, user information, or credential; and
    - `access_code`: exactly 20 unpadded Base64url characters generated from 15
      cryptographically random bytes.

    Grove sets `model` to the literal `KLOVE` because the user selected that
    trusted path; the frame cannot choose another printer type. It immediately
    submits the bundle through the existing authorized printer-create endpoint
    and clears the access code from transient UI state. Failures and cancellation
    create no Grove printer. Moonraker data and credentials never appear in the
    completion message.

### Conservative runtime behavior

16. At runtime Grove uses its existing MQTT/TLS and implicit-FTPS client paths
    against Klove's per-printer compatibility facade. Initial support is limited
    to conservative status, exact-printer artifact dispatch once issue #12 is
    complete, and the separately accepted pause, resume, and cancel operations.
17. `KLOVE` disables model-derived file compatibility and scheduling. Queue
    selection is by the exact registered printer only; Klove's target and
    safety-profile evidence remains the dispatch authority. Grove must hide or
    disable Bambu firmware, maintenance, HMS, AMS, drying, calibration, camera
    protocol, model-derived scheduling, temperature, speed, fan, light, jog,
    home, extrusion, and every other unsupported hardware control. Klove also
    rejects them server-side.
18. The compatibility contract is tested against an exact supported Grove
    revision. Grove-source drift blocks a claimed compatibility release until
    its create schema, feature gates, MQTT/FTPS behavior, and browser flow pass
    again. No persistent Grove fork or native provider programme is created.

This decision authorizes architecture and prerequisite work only. It does not
authorize a new printer actuator, generic G-code, public print-start route, or
an unauthenticated setup endpoint.

## Consequences

- Klove gains one deliberately narrow Web UI because setup and recovery contain
  demonstrated human choices. The rest of the product remains headless.
- The normal user no longer edits per-printer TOML once the registry and UI
  slices ship. Until then, the current file path remains a developer/bootstrap
  limitation and must not be described as the finished onboarding experience.
- Grove carries a small explicit maintenance surface for `KLOVE` presentation,
  embedding, handoff, and conservative feature gates, but no Moonraker or
  Klipper implementation.
- The browser completion includes one generated compatibility secret because
  Grove's existing MQTT/FTPS client requires it. Exact-origin messaging,
  one-flow binding, short lifetime, no storage, and immediate authorized create
  minimize exposure; Moonraker credentials never cross that boundary.
- A deployment may need a dedicated same-site Klove origin and certificate.
  Cross-site embedding that relies on third-party cookies is not supported.
- The earlier broad native Grove provider plan is retired as not planned.
  Existing native Klove machine APIs may remain narrow internal interfaces, but
  they are not a new Grove backend commitment.

## Validation

- Registry, secret, authentication, authorization, session, CSRF, origin,
  message-decoding, translation, and policy code retain 100% statement and
  branch coverage, including negative and restart cases.
- Scripted browser tests exercise the real parent/frame handshake, exact-origin
  and schema rejection, session expiry/replay, cancellation, successful create,
  hostile input, responsive layouts, keyboard navigation, and accessibility.
- Grove contract tests assert the exact create-field limits, `KLOVE` display and
  image fallback, exact-printer scheduling, unsupported-feature suppression,
  existing Bambu regressions, and compatibility transport behavior at the
  recorded revision.
- Privacy tests assert that Moonraker credentials, owner credentials, access
  codes, probe bodies, and setup session material are absent from URLs, history,
  storage, logs, telemetry, screenshots, and error bodies as applicable.

## Primary references

- [Grove printer create schema at the supported revision](https://github.com/EdwardChamberlain/grove-control/blob/cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4/backend/app/schemas/printer.py)
- [Grove printer model at the supported revision](https://github.com/EdwardChamberlain/grove-control/blob/cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4/backend/app/models/printer.py)
- [Grove Add Printer and model-gated UI at the supported revision](https://github.com/EdwardChamberlain/grove-control/blob/cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4/frontend/src/pages/PrintersPage.tsx)
- [Content Security Policy Level 3](https://www.w3.org/TR/CSP3/)
- [HTML iframe sandbox and cross-document messaging](https://html.spec.whatwg.org/multipage/iframe-embed-object.html)

## Tracking

- [Decision #57](https://github.com/tomlawesome/klove/issues/57)
- [Secure registry #58](https://github.com/tomlawesome/klove/issues/58)
- [Direct probe and lifecycle #63](https://github.com/tomlawesome/klove/issues/63)
- [Embedded setup and recovery #59](https://github.com/tomlawesome/klove/issues/59)
- [Minimal Grove contribution #60](https://github.com/tomlawesome/klove/issues/60)
- [Current-Grove epic #32](https://github.com/tomlawesome/klove/issues/32)
