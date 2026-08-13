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
- [Delivery-lane issue #2](https://github.com/tomlawesome/klove/issues/2) is
  complete. Protected preview run `31741422470` published the attested candidate
  `ghcr.io/tomlawesome/klove@sha256:7be3122ef7427fe9d49a27980de16363f4be2ed8f28ec1ab73b2e18dab690fc8`.
- [Stable-promotion issue #3](https://github.com/tomlawesome/klove/issues/3)
  remains blocked on documented production-like printer acceptance; the
  candidate has not been promoted to `main`, a version tag, or `latest`.
- [Headless-boundary decision #40](https://github.com/tomlawesome/klove/issues/40)
  records that Grove owns normal interaction and a Klove UI is a last resort.
- [Artifact-contract issue #7](https://github.com/tomlawesome/klove/issues/7)
  is the active safe-dispatch prerequisite.
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
- [x] Non-root, read-only-root compatible OCI image definition, proven by the
  protected preview runtime contract, vulnerability scan, SBOM, and provenance
  attestation for the immutable candidate recorded above.

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

## Active slice: versioned artifact contracts

- [x] Define strict v1 models for archive intake, one selected plate, exact
  printer/profile binding, operation identity/state, validation evidence, and
  bounded structured denials.
- [x] Require canonical UUIDv4 identities, lowercase SHA-256 digests, one known
  artifact format, bounded visible-ASCII identifiers, and canonical relative
  archive paths.
- [x] Add finite configuration limits for ZIP entry count, archive compressed
  and expanded bytes, compression ratio, selected G-code bytes, and metadata
  wait time. Compression evidence uses exact entry byte counts and integer
  cross-products rather than a trusted or rounded ratio claim.
- [x] Require exactly one complete validation candidate matching both the
  intent and the current target. Unknown, absent, multiple, stale, or
  contradictory evidence is denied.
- [x] Add accepted and rejected JSON fixtures for every contract boundary plus
  positive, negative, and configuration tests.
- [x] Complete independent review and the full local gate.
- [ ] Obtain clean GitHub PR CI and merge through protected `develop`.

Exit criterion: untrusted artifact metadata can be represented and evaluated
without aliases or inference, while no archive extraction, Moonraker upload, or
print-start transport exists.

## Next slices

1. Hostile-3MF/G-code validation, followed by exact target/profile binding,
   safe upload, metadata verification, idempotent print start, and durable
   reconciliation. Print-start implementation and transport are blocked until
   a dedicated ADR is accepted; roadmap placement is not authorization.
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
