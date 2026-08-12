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

## Active slice: read-only foundation

- [ ] Package, configuration, secret-file, logging, and container foundation.
- [ ] Canonical printer state and capability schemas.
- [ ] Strict Grove MQTT command decoder that produces typed requests or a
  structured denial; no actuator transport exists.
- [ ] Authenticated read-only HTTP API with content-free liveness/readiness.
- [ ] Moonraker WebSocket identification, discovery, subscription, diff
  reduction, disconnect handling, and bounded reconnect.
- [ ] Deterministic fake Moonraker contract tests.
- [ ] Critical-boundary 100% coverage gate and project-wide coverage evidence.
- [ ] Three-lane GitHub Actions and protected branch rules.
- [ ] Non-root, read-only-root compatible OCI image.

Exit criterion: Klove can monitor one or more simulated Moonraker printers
through restarts and malformed messages, while every Grove command remains
denied or classified without reaching an actuator.

## Next slices

1. Typed pause, resume, and cancel using Moonraker's dedicated RPC methods.
2. Explicitly bounded heater and live-control operations.
3. Hostile-3MF validation and idempotent target-bound dispatch.
4. Current-Grove MQTT/FTPS compatibility facade.
5. Native Grove provider integration.

