# Klove implementation plan

Status: active  
Last updated: 2026-08-12

## Delivery policy

- Ordinary work starts from and targets protected `develop`.
- `preview` is the protected, exact-container acceptance lane.
- `main` receives only a tested `preview` revision and stable promotion reuses
  the accepted digest without rebuilding it.
- This is deliberately a single-maintainer repository. Pull requests and
  resolved conversations are required, but approving reviews are not.
- Production self-review and administrator bypass remain available.
- The repository default `GITHUB_TOKEN` is read-only. Individual publication
  jobs receive only their required write scopes.

## Safety invariant

A command is permitted only when Klove can positively prove that the
authenticated source, target printer, current state, discovered capability,
configured safety policy, parameter bounds, and requested transition are all
valid. Missing, stale, malformed, unknown, contradictory, or ambiguous evidence
is a denial. Klove never guesses.

Authentication, authorization, command decoding, translation, and policy code
must retain 100% statement and branch coverage. Coverage is supplemented by
property tests and negative protocol fixtures; it is not treated as proof of
semantic completeness.

## Completed local slice: read-only foundation

- [x] Package, configuration, secret-file, logging, and container foundation.
- [x] Canonical printer state and capability schemas.
- [x] Strict Grove MQTT command decoder that produces typed requests or a
  structured denial; no actuator transport exists.
- [x] Authenticated read-only HTTP API with content-free liveness/readiness.
- [x] Moonraker WebSocket identification, discovery, subscription, diff
  reduction, disconnect handling, and bounded reconnect.
- [x] Deterministic fake Moonraker contract tests.
- [x] Critical-boundary 100% coverage gate and project-wide coverage evidence.
- [ ] Three-lane GitHub Actions and protected branch rules. Workflows are
  implemented; remote rules and clean CI validation remain.
- [x] Non-root, read-only-root compatible OCI image definition. A local build
  is unavailable on the current host and remains to be proven in clean CI.

Exit criterion: Klove can monitor one or more simulated Moonraker printers
through restarts and malformed messages, while every Grove command remains
denied or classified without reaching an actuator.

## Active slice: control safety prerequisites

- [x] Boot-scoped opaque state tokens bind a revision to its observed job data.
- [x] Local monotonic receipt times make evidence freshness measurable.
- [x] Condition-based revision waits provide race-safe future reconciliation.
- [x] Duplicate authentication headers are rejected.
- [x] Threat analysis covers job substitution, lost acknowledgements, custom
  macros, remote identity, and preflight/control races.
- [ ] Define an atomic conditional-control contract at the Moonraker/Klipper
  boundary; stock Moonraker endpoints are insufficient.
- [ ] Decide whether the atomic gate is a small Moonraker component, Klipper
  extra, or another host-local mechanism.
- [ ] Bind the gate to verified remote identity and an operator-approved
  control-profile fingerprint.

Exit criterion: Klove has a host-side primitive that atomically verifies target,
job identity, state, and approved control implementation before applying one
typed transition. Until that exists, every command remains denied and Klove
contains no actuator transport.

## Next slices

1. Atomic, fingerprint-bound host control gate.
2. Typed pause, resume, and cancel through that gate.
3. Explicitly bounded heater and live-control operations.
4. Hostile-3MF validation and idempotent target-bound dispatch.
5. Current-Grove MQTT/FTPS compatibility facade.
6. Native Grove provider integration.
