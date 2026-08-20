# Product boundary and architecture

Klove is one centrally managed service beside Grove Control, normally a
container in the same Docker deployment. It connects outward to each printer's
Moonraker HTTP and WebSocket APIs; it does not scrape Mainsail, modify Klipper,
or require an installation on every printer host.

Klove is headless and automation-first for routine operation. Grove owns the
normal operator experience. Prefer direct, structured controller-to-controller
evidence over human input; reconcile automatically when a bounded proof exists
and otherwise fail closed. [ADR 0006](../decisions/0006-embedded-grove-onboarding.md)
accepts one narrow exception to [ADR 0002](../decisions/0002-headless-automation-boundary.md):
a Klove-owned setup/recovery page embedded by Grove's **Klipper via Klove** Add
Printer path. It is limited to irreducible onboarding and recovery choices and
must not become a dashboard or printer-control surface.

## Northbound boundaries

Use three deliberately narrow northbound surfaces:

1. A Bambu-shaped MQTT/TLS and implicit-FTPS facade supplies only conservative
   runtime behavior already accepted by Klove's own safety contracts.
2. A setup/recovery HTTP surface owns authenticated onboarding, direct
   Moonraker probes, the runtime registry, secret handling, and a strict
   completion handoff to Grove.
3. Existing versioned Klove HTTP endpoints remain bounded machine interfaces;
   they are not a commitment to a broad native Grove provider.

Grove receives only an explicit conservative `KLOVE` printer type, the embedded
Add Printer path, completion-message decoding, and feature gates. It does not
receive Moonraker credentials or implement Klipper semantics. The earlier broad
native-provider programme is retired as not planned.

Do not translate `printer.cfg` into arbitrary commands and do not attempt to
convert Bambu-sliced G-code into Klipper G-code. Read Klipper's exposed objects
and selected configuration values to construct a capability and safety profile.
Accept only typed operations, validate them against current state and that
profile, and reject unknown or ambiguous operations.

## Deployment choice

Moonraker is already the supported network API in front of Klipper. It exposes
printer control, object subscriptions, file upload, job history, cameras, and
authentication. Mainsail is another Moonraker client, so putting Klove behind or
inside Mainsail adds no useful control surface. Klipper's own API is a local Unix
socket; a per-host Klove agent would only recreate functionality Moonraker
already provides.

| Option | Advantages | Costs and risks | Decision |
| --- | --- | --- | --- |
| Central Klove container | One install and upgrade; one audit/logging point; manages many printers; uses official Moonraker APIs; natural future-adapter boundary | Requires network reachability and credentials to Moonraker; central failure affects Grove integration | Default |
| systemd service on every Klipper host | Can use loopback or the Klipper Unix socket; each host has a distinct IP for Bambu-compatible ports | Fleet-wide installation, upgrades, secrets, certificates, and support burden; competes for small SBC resources | Optional later for isolated/outbound-only networks |
| Moonraker component/plugin | Close integration and local access | Couples Klove to Moonraker internals and its release cadence; must be installed everywhere | Reject for v1 |
| Implement Klipper directly in Grove | Fewest runtime services | Expands Grove's Bambu-specific core, couples release cycles, and creates a new Klipper maintenance surface | Reject; permit only the minimal `KLOVE` type, embedded Add Printer path, handoff, and conservative feature gates in ADR 0006 |

An optional lightweight agent can be designed later for sites where Moonraker
cannot accept an authenticated connection from the Grove host. It should make an
outbound mTLS connection to central Klove and preserve the same canonical API;
it is not a separate implementation.

## Target architecture

```text
Grove Control
  |  setup: exact-origin iframe + strict completion message
  |  runtime: MQTT/TLS + implicit FTPS
  v
Klove northbound boundaries
  -> owner-authenticated setup/recovery and runtime registry
  -> compatibility authentication and exact per-printer routing
  -> command decoder / conservative state encoder
  v
Canonical printer domain
  -> typed state and capabilities
  -> command policy and state machine
  -> idempotency, operation journal, reconciliation
  -> artifact validation and dispatch coordinator
  v
Moonraker adapter (one session per printer)
  -> WebSocket JSON-RPC subscriptions and commands
  -> HTTP file upload, metadata, history, typed start, and webcams
  v
Moonraker -> Klipper -> MCU

Mainsail remains a parallel Moonraker client.
```

Suggested package boundaries:

```text
src/klove/
  domain/          state, capabilities, typed commands, policy
  orchestration/   registry, dispatch, reconciliation, idempotency
  adapters/
    moonraker/     HTTP/WS client, discovery, state and file mapping
  northbound/
    bambu_compat/  MQTT, FTPS, Bambu payload mapping
    onboarding/    bounded setup/recovery HTTP and browser contract
    api/           bounded versioned machine API
  persistence/     configuration, operation journal, migrations
```

Python with `asyncio` is the path of least resistance: Grove and Moonraker are
Python systems, the workload is I/O-heavy, and one event loop can supervise many
Moonraker WebSockets. Use a shared HTTP/WebSocket client session per endpoint,
Pydantic models at protocol boundaries, SQLite/WAL for small durable state, and
an OCI image for amd64 and arm64. Keep MQTT and FTPS implementations behind
interfaces so the exact server libraries can be changed after protocol contract
tests.
