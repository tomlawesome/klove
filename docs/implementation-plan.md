# Klove implementation plan

Status: active  
Last updated: 2026-08-13

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

## Delivery tracking

- [Programme roadmap #38](https://github.com/tomlawesome/klove/issues/38) is the
  top-level GitHub execution register.
- [Typed job-control issue #4](https://github.com/tomlawesome/klove/issues/4)
  and its epic are complete on protected `develop`.
- [Headless-boundary decision #40](https://github.com/tomlawesome/klove/issues/40)
  records that Grove owns normal interaction and a Klove UI is a last resort.
- [Project-view issue #39](https://github.com/tomlawesome/klove/issues/39)
  records the remaining account-level GitHub Projects permission blocker.
- This document remains the architecture and sequencing source of truth; GitHub
  issues hold delivery status and evidence.

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

## Completed slice: read-only foundation

- [x] Package, configuration, secret-file, logging, and container foundation.
- [x] Canonical printer state and capability schemas.
- [x] Strict Grove MQTT command decoder that produces typed requests or a
  structured denial; no actuator transport exists.
- [x] Authenticated read-only HTTP API with content-free liveness/readiness.
- [x] Moonraker WebSocket identification, discovery, subscription, diff
  reduction, disconnect handling, and bounded reconnect.
- [x] Deterministic fake Moonraker contract tests.
- [x] Critical-boundary 100% coverage gate and project-wide coverage evidence.
- [x] `main` is the default branch; `develop`, `preview`, and `main` require pull
  requests and resolved conversations and reject force-pushes and deletion.
  Default Actions permissions are read-only and `production` is restricted to
  reviewed `main` deployments.
- [x] Land the implemented workflows, obtain clean CI, confirm the exact check
  contexts, and make those contexts required on each protected lane.
- [x] Non-root, read-only-root compatible OCI image definition. A local build
  is unavailable on the current host and remains to be proven in clean CI.

Exit criterion: Klove can monitor one or more simulated Moonraker printers
through restarts and malformed messages, while every Grove command remains
denied or classified without reaching an actuator.

## Completed slice: typed pause, resume, and cancel

- [x] Boot-scoped opaque state tokens bind a revision to its observed job data.
- [x] Local monotonic receipt times make evidence freshness measurable.
- [x] Condition-based revision waits provide race-safe future reconciliation.
- [x] Duplicate authentication headers are rejected.
- [x] Threat analysis covers job substitution, lost acknowledgements, custom
  macros, remote identity, and preflight/control races.
- [x] ADR 0001 accepts stock Moonraker pause, resume, and cancel with explicit
  operator ownership of Klipper macro correctness and the residual non-atomic
  race.
- [x] Installation-wide and per-printer opt-in gate one exact control scope and
  one configured transport route.
- [x] The native route accepts only pause, resume, and cancel with canonical
  idempotency keys and exact boot-scoped state tokens.
- [x] Per-printer locking serializes cached authorization, immediate live poll,
  one dispatch, and post-action reconciliation.
- [x] Cached and live evidence must match phase and filename, with current token,
  capability, monotonic event time, and non-regressing preflight position.
- [x] Operations that dispatched or may have dispatched remain in the bounded
  process-epoch journal. An uncertain printer/state-token pair is fenced across
  all idempotency keys; exhaustion fails closed rather than permitting replay.
- [x] Post-action polling binds the target phase to the preflight filename and
  monotonic evidence. Any ambiguity becomes `outcome_unknown` with no retry.
- [x] Repository policy permits only the three dedicated actuator RPCs in one
  adapter and rejects generic G-code, print start, and other actuators.
- [x] Complete documentation review and the full local gate.
- [x] Obtain clean GitHub PR CI and merge through the protected workflow into
  `develop`.

Exit criterion: one authenticated, current, exact pause/resume/cancel request is
dispatched at most once and reported confirmed only from later evidence for the
same job. Every missing, stale, contradictory, or post-dispatch ambiguous state
fails closed.

## Next slices

1. Hostile-3MF/G-code validation, exact target/profile binding, safe upload,
   metadata verification, idempotent print start, and durable reconciliation.
   Print-start implementation and transport are blocked until a dedicated ADR
   is accepted; roadmap placement is not authorization.
2. Current-Grove MQTT/TLS and FTPS compatibility facade with conservative state
   projection and specific-printer queueing.
3. Separately decided and tested bounded temperature/speed plus explicitly
   mapped fan/light controls. Keep jog and extrusion disabled until proven.
4. Fleet hardening: durable journal, multi-printer fault isolation, cameras,
   metrics, backup/restore, migrations, and restart/fault/soak tests.
5. Native Grove provider integration with provider-neutral capabilities,
   structured errors, and target-profile scheduling.
6. Later adapters: exclude-object, richer cameras, MMU/toolchanger support,
   optional outbound host agent, and additional printer stacks.
