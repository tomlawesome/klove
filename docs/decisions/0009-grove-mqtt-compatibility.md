# ADR 0009: define a conservative Grove MQTT compatibility boundary

Status: proposed; implementation prohibited until the observation gates below
are complete and this decision is accepted

Date: 2026-08-21

## Context

ADR 0006 requires one small Bambu-shaped compatibility facade so Grove can
remain Klove's normal user interface. The repository already has a strict JSON
decoder for `pause`, `resume`, and `stop`, but that decoder deliberately cannot
actuate. No accepted decision currently defines an MQTT listener, its TLS and
authentication policy, topic authorization, QoS 1 replay handling, resource
bounds, or the translation into ADR 0001's control contract.

The supported Grove revision is pinned by ADR 0008. It publishes requests to
`device/{serial}/request`, subscribes to `device/{serial}/report`, and expects a
TLS MQTT service. The setup completion defined by ADR 0006 supplies one stable
`KLOVE-<UPPERCASE-UUID>` serial, one compatibility host, and one 20-character
access code. Those facts are not enough to infer the remaining wire protocol.

## Proposed decision

Accept a dedicated MQTT compatibility adapter only after its exact packet and
payload profile is completed from approved ADR-0008 black-box evidence. The
adapter remains a narrow transport around existing canonical state and ADR-0001
control. It is not a Bambu device model, broker for general clients, Grove
authentication service, or new actuator.

### Clean-room protocol gate

1. Before this decision can become accepted, approved observation fixtures for
   the exact supported Grove revision must record the minimum required MQTT
   version, `CONNECT` fields, access-code presentation, client identifier,
   clean-session behavior, keepalive, TLS behavior, topic/QoS/retain flags,
   request schemas, report schemas, and acknowledgement ordering. Each fixture
   follows ADR 0008's manifest, sanitization, separation, and revision rules.
   Existing decoder JSON files are not compatibility evidence.
2. The resulting protocol profile is versioned in Klove and enumerates every
   accepted packet, field, type, bound, and transition. Unknown MQTT features,
   packets, topics, wildcards, fields, commands, versions, and extensions fail
   closed. A Grove revision change invalidates the compatibility claim until
   the clean-room lane passes again.

### TLS, identity, and authorization

3. The listener is TLS-only and names certificate and private-key files through
   deployment configuration. Secret values never enter tracked configuration,
   URLs, logs, metrics, error text, fixtures, or process arguments. The exact
   TLS versions and certificate-validation behavior must be both compatible
   with the observed client and explicitly recorded before acceptance; no
   plaintext listener or opportunistic downgrade is permitted.
4. One successful connection authenticates exactly one active canonical
   registry printer. The access code is compared through the owner-only secret
   boundary without exposing whether another printer exists. Its record UUID,
   stable serial, presented MQTT identity, and every topic serial must all agree
   exactly. A credential never grants access to another printer, wildcard
   topic, owner/setup API, Moonraker credential, or dispatch operation.
5. Rotation, disable, removal, or record revision change invalidates new
   authentication immediately and closes existing affected sessions within a
   bounded interval. Authentication and session admission share the
   per-printer gate with lifecycle and actuation so a stale session cannot race
   a mutation. Failures use one non-enumerating result and never echo hostile
   identity material.

### MQTT session and resource policy

6. The adapter accepts only the exact request publish and report subscription
   topics for its authenticated serial. It denies wildcard subscriptions,
   cross-serial topics, retained requests, server-directed publishes, and every
   other topic. The exact supported QoS, duplicate flag, subscription options,
   session persistence, will handling, and acknowledgement behavior remain
   blocked on the observation gate; the implementation may support no broader
   MQTT feature set than Grove requires.
7. Configuration places fixed bounds on listeners, global and per-printer
   sessions, connection rate, packet and payload bytes, inflight publishes,
   queued reports, keepalive, idle duration, and shutdown time. Bounds have
   conservative hard maxima. Capacity exhaustion rejects or closes work rather
   than dropping an admitted command, evicting unresolved replay evidence, or
   publishing stale state as current.
8. Parsing is incremental and bounded before allocation. Malformed framing,
   invalid UTF-8, duplicate or non-canonical JSON members, truncated packets,
   unsupported packet flags, and timeouts fail closed. Client-controlled bytes
   are never used as log messages, metric labels, filesystem names, or routing
   keys before strict validation.

### Status and control translation

9. Reports are derived only from the canonical runtime snapshot and bounded
   operation results. They conservatively map idle, preparing, printing,
   paused, completed, cancelled, failed, unavailable, stale, denial, and
   `outcome_unknown` evidence through the exact observed report schema.
   Unsupported Bambu capabilities and fabricated values are omitted. Unknown,
   stale, contradictory, or disconnected printer evidence never becomes an
   operable or successful report.
10. Request payloads first pass the existing exact decoder. Only `pause`,
    `resume`, and `stop` may reach policy; `print.project_file`, generic
    `gcode_line`, parameters, and every other command remain denied. This
    decision does not authorize FTPS, artifact upload, print start, temperature,
    speed, fan, light, camera, motion, extrusion, firmware, maintenance, AMS,
    MMU, or calibration behavior.
11. The adapter is a separately authenticated machine principal limited to the
    exact printer and ADR-0001 control scope. On the first valid command it
    obtains the current canonical control token, reserves a durable ingress
    identity, and then invokes only the existing control service. It cannot
    construct Moonraker methods or bypass global/per-printer opt-in, current
    evidence, live preflight, shared admission, single dispatch, confirmation,
    or uncertainty fencing.

### QoS 1 replay and recovery

12. MQTT delivery acknowledgement is not semantic command success. Before
    invoking ADR 0001, Klove durably binds the authenticated printer, command
    kind, exact observed sequence/task identifier, canonical payload hash,
    selected control token, and one generated canonical idempotency key. An
    exact QoS 1 duplicate returns the same bounded operation result and never
    dispatches again. Reuse of an identity with any different field is denied.
13. The ingress journal is owner-only, capacity-bounded, crash-safe, and part of
    the same quiesced backup/restore set as the registry, secrets, runtime
    fences, and control state. Entries that dispatched or may have dispatched
    are not evicted. A crash after reservation cannot cause redispatch; missing
    exact proof becomes `outcome_unknown`. A new process epoch, reconnect, new
    MQTT packet identifier, or changed telemetry never converts an unresolved
    record into authority.
14. Report publication may be retried as data delivery, but a report retry
    cannot repeat control. Per-printer ordering is monotonic and bounded; a
    slow subscriber cannot block admission, overwrite a newer terminal result,
    or make an old state appear current. Disconnect and reconnect perform
    read-only reconciliation before accepting another command for that printer.

## Required validation before acceptance

- ADR-0008-compliant black-box fixtures and manifests define every retained
  Grove protocol token and observed behavior at the pinned revision.
- Protocol, authentication, authorization, decoding, translation, replay,
  lifecycle-race, shutdown, and policy code has 100% statement and branch
  coverage with negative, property, fuzz, restart, and resource-exhaustion
  tests.
- Contract tests prove exact topic isolation, credential rotation and disable,
  QoS 1 duplicate/conflict behavior, stale-token denial, lost-control-response
  fencing, report ordering, reconnect reconciliation, and absence of secrets.
- The dependency review either selects a maintained server implementation with
  acceptable provenance and a deliberately restricted surface, or separately
  justifies and tests the bounded parser. No Grove source or dependency graph
  becomes a Klove implementation input.
- The exact supported Grove client passes in an isolated private network while
  unsupported commands remain denied and existing Bambu behavior is unchanged.

## Consequences

- Issue #10 — MQTT compatibility facade may perform clean-room observation and
  protocol design, but runtime implementation remains blocked while this ADR is
  proposed or its exact wire profile is incomplete.
- Issue #79 — completion handoff supplies the compatibility identity material;
  it grants no runtime authority by itself.
- Issue #76 — native dispatch proof and issue #12 — end-to-end dispatch are
  complete and remain separate. This decision does not expose their artifact
  path through MQTT.
- Issue #32 — Grove bridge retains Grove as the user interface without making
  Klove emulate unsupported Bambu hardware.

## Tracking

- [MQTT compatibility facade #10](https://github.com/tomlawesome/klove/issues/10)
- [End-to-end dispatch #12](https://github.com/tomlawesome/klove/issues/12)
- [Grove bridge #32](https://github.com/tomlawesome/klove/issues/32)
