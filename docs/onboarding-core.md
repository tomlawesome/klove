# Moonraker onboarding core contract

Status: onboarding is implemented under issue #63 — onboarding core, and issue
#68 — runtime supervisor adds registry-backed monitoring. Startup wiring, the
shared actuator gate, API, and browser route remain later slices.

## Direct probe

`MoonrakerOnboardingProbe` accepts one validated `MoonrakerEndpoint`, one
bounded visible-ASCII Moonraker API key, and one deployment-owned exact address
allowlist. The endpoint is a canonical HTTP(S) origin: it has a lowercase
canonical DNS name or canonical IP literal, omits default ports, contains no
credentials, path (including `/`), query, fragment, IPv6 scope, or IPv4-mapped
IPv6 alias, and requires explicit consent for non-loopback cleartext HTTP.

The address policy contains 1–64 canonical CIDRs, prohibits an all-addresses
route, and bounds the number of resolver answers. Every answer—not merely the
selected answer—must be canonical numeric TCP evidence inside the allowlist and
must not be unspecified, multicast, link-local, or reserved. The probe chooses
one deterministic address, supplies only that address to a private resolver for
the HTTP connector, and preserves the configured DNS host for the HTTP `Host`
header and TLS hostname verification. A mixed allowed/disallowed DNS result is
a denial. The selected peer IP is retained in the identity evidence so a later
DNS target change changes the evidence.

The probe sends the API key only as `X-Api-Key`, disables redirects, ambient
proxy configuration, cookies, and automatic decompression, and enforces both a
whole-probe deadline and a per-request deadline. Each response must be one
bounded UTF-8 JSON object with one `result` member, one JSON content type, no
unsupported content encoding, no duplicate members, and no non-finite number.
It reads this exact sequence:

1. `GET /server/info`
2. `GET /printer/info`
3. `GET /printer/objects/list`
4. `GET /printer/objects/list`
5. `GET /printer/info`
6. `GET /server/info`

Both complete snapshots must agree exactly. Moonraker and Klippy must report
ready, identity/version text and every object name must be bounded and exact,
and duplicate or changing object evidence is denied. The conservative
capability snapshot is derived only after those checks. Discovery
advertisements and display/model names are not inputs to this proof.

## Lifecycle transactions

`PrinterLifecycleService` is the sole typed orchestration boundary over the
canonical `PrinterStore` and owner-only `SecretStore`. Every request binds a
canonical printer UUID and idempotency UUID, authenticated actor and request
origin, and every operation-specific field. Existing-printer requests also
bind the exact current revision. A keyed HMAC fingerprint includes the complete
request and submitted Moonraker secret without persisting either request or
secret material.

The supported transitions are deliberately closed:

| Operation | Required state | Direct probe | Result |
| --- | --- | --- | --- |
| create | UUID and endpoint absent | immediately before commit | active revision 1 with two new secret references |
| update | active, or disabled with explicit reactivation | immediately before commit using the stored key and requested endpoint | exact replacement at revision + 1 |
| rotate Moonraker credential | active | immediately before commit using the new, non-identical key and existing endpoint | refreshed identity and new reference at revision + 1 |
| rotate compatibility credential | active | none | new, non-identical 20-character credential and reference at revision + 1 |
| disable | active | none | disabled, with control and dispatch off, at revision + 1 |
| remove | disabled | none | retained tombstone with no credential references at revision + 1 |

Create generates the compatibility credential from exactly 15 cryptographically
random bytes. Rotation rejects a value identical to the current credential.
This service never returns a compatibility secret; it returns only the
secret-free `RegisteredPrinter`. The one-flow completion boundary in issue #59
is the only future component allowed to expose the active compatibility value
to Grove.

Every mutation of an existing printer currently requires a composite proof
that both in-process job control and the durable print-start journal have no
unresolved outcome. This is stronger than the minimum identity-changing and
disable/remove rule. Issue #64 must place lifecycle admission and all new
control/start admission behind one shared per-printer runtime gate, hold that
gate through the proof and registry commit, and deactivate or replace the
runtime only from the committed result. The inspector is not permission to
wire this library behind an uncoordinated route.

## Idempotency and recovery

The store first reserves one secret-free `preparing` operation, which serializes
the printer. New secret files are written only under its reserved opaque
references. The final direct probe is performed after the reservation and
before the synchronous registry commit. The database transaction applies the
exact revisioned transition and records the complete public result; only then
are retired secret files deleted.

An exact committed duplicate returns and finalizes the durable result without
another probe, secret generation, or remote request. A preparing duplicate is
busy, a changed use of the same key is a conflict, and an aborted exact request
may make a new bounded attempt. Cancellation or any pre-commit failure aborts
the reservation and removes only its uncommitted new references. If the
database commit succeeded but local secret retirement is ambiguous, the
operation remains committed: the caller receives a bounded storage failure and
the same key later completes cleanup and returns the result without repeating
the probe. Startup reconciliation applies the same rule.

Errors leave the boundary as one bounded code and contain no endpoint response,
credential, secret reference value, or hostile exception text. The core adds no
Moonraker mutation, printer actuator, upload, print start, generic G-code,
listener, browser route, or UI.

## Validation and dependent slices

Unit and local HTTP contract tests cover positive and hostile endpoint, DNS,
authentication-header, HTTP, JSON, evidence-drift, lifecycle, concurrency,
restart/cleanup, redaction, and storage-failure behavior at 100% statement and
branch coverage. Changes to this protocol boundary also run the existing native
Moonraker simulation as a regression gate.

- Issue #68 — runtime supervisor consumes complete active records through a
  fail-closed dynamic monitor supervisor. Issue #69 — shared admission adds the
  per-printer lifecycle/actuator gate, and issue #70 — runtime bootstrap owns
  startup reconciliation and bootstrap wiring.
- Issue #65 — protected onboarding API adds the separately authenticated,
  authorized, bounded API.
- Issue #59 may add the accepted setup/recovery browser flow only after those
  machine boundaries are complete.
