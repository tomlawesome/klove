# Klove implementation plan

Status: active  
Last updated: 2026-08-14

## Delivery policy

- Ordinary work starts from and targets protected `develop`.
- `preview` is the protected, exact-container acceptance lane.
- A successful `preview` push publishes a unique `preview-<run>-<attempt>`
  candidate tag and moves the `preview` pointer to that exact recorded digest.
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
  [Embedded-onboarding decision #57](https://github.com/tomlawesome/klove/issues/57)
  accepts that exception for setup/recovery only and retires the broad native
  provider programme.
- [Secure-registry epic #58](https://github.com/tomlawesome/klove/issues/58)
  is split into independently reviewable foundation
  [#62](https://github.com/tomlawesome/klove/issues/62), direct-probe lifecycle
  [#63](https://github.com/tomlawesome/klove/issues/63), dynamic runtime
  [#64](https://github.com/tomlawesome/klove/issues/64), and protected API
  [#65](https://github.com/tomlawesome/klove/issues/65) slices. Foundation #62
  is merged and #63's probe/lifecycle library boundary is implemented; #64 is
  the next dependent slice. Embedded
  setup/recovery [#59](https://github.com/tomlawesome/klove/issues/59) and the
  minimal Grove contribution
  [#60](https://github.com/tomlawesome/klove/issues/60) follow under epic #32.
- [Artifact-contract issue #7](https://github.com/tomlawesome/klove/issues/7)
  and [PR #46](https://github.com/tomlawesome/klove/pull/46) record the completed
  safe-dispatch contract prerequisite.
- [Hostile artifact issue #6](https://github.com/tomlawesome/klove/issues/6)
  and [PR #47](https://github.com/tomlawesome/klove/pull/47) record the completed
  non-actuating validation slice.
- [Target-qualification issue #5](https://github.com/tomlawesome/klove/issues/5)
  records the v3 exact printer UUID and safety-profile approval boundary.
- [Bounded upload issue #8](https://github.com/tomlawesome/klove/issues/8) and
  [PR #55](https://github.com/tomlawesome/klove/pull/55) are complete on
  protected `develop`.
- [Durable print-start issue #9](https://github.com/tomlawesome/klove/issues/9)
  is implemented under accepted ADR 0005 with no northbound route; issue #12
  remains the end-to-end lifecycle slice.
- [Native integration issue #48](https://github.com/tomlawesome/klove/issues/48)
  and [RatOS emulation spike #50](https://github.com/tomlawesome/klove/issues/50)
  are complete on protected `develop`.
- [RatOS contract spike #51](https://github.com/tomlawesome/klove/issues/51)
  has an implemented controlled host-MCU contract lane. Exact-release runs
  proved the controlled configuration, production observation, and the three
  dedicated controls, but no single uninterrupted run has yet completed the
  lost-response case and archived `contract-passed`; the issue remains open.
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

- [x] Boot-scoped opaque state tokens bind a dedicated control revision,
  capability, phase, filename, and unique Moonraker history job identity while
  remaining stable across unrelated telemetry and forward progress.
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
- [x] The caller token must match exactly at admission and final cached recheck.
  Cached and live evidence must match phase, filename, unique history job id and
  start time with current capability, monotonic event time, and non-regressing
  position. Equal history reads bracket every accepted direct object sample;
  up to three read-only samples may obtain coherence without ever retrying an
  action. A generic monitor update is accepted only when the exact token is
  unchanged and its evidence is fully bounded by that preflight.
- [x] Operations that dispatched or may have dispatched remain in the bounded
  process-epoch journal. An uncertain printer/state-token pair is fenced across
  all idempotency keys; ordinary progress cannot cross the fence, and exhaustion
  fails closed rather than permitting replay.
- [x] Post-action polling binds the target phase to the preflight filename and
  monotonic evidence. Any ambiguity becomes `outcome_unknown` with no retry.
- [x] Repository policy confines the three dedicated control RPCs to one
  adapter and rejects generic G-code, print start, and other actuators in this
  completed slice.
- [x] Complete documentation review and the full local gate.
- [x] Obtain clean GitHub PR CI and merge through the protected workflow into
  `develop`.

Exit criterion: one authenticated, current, exact pause/resume/cancel request is
dispatched at most once and reported confirmed only from later evidence for the
same job. Every missing, stale, contradictory, or post-dispatch ambiguous state
fails closed.

## Completed slice: native integration and RatOS procedure

- [x] Build an amd64 test-only image from immutable Python, Klipper, and
  Moonraker identities with hash-pinned dependencies and fixture integrity
  checks. Retain upstream source and licence files; do not publish the image.
- [x] Run real Klipper with its Linux-process MCU, `kinematics: none`, no
  heaters/motors/GPIO/devices, and one bounded virtual-SD dwell fixture.
- [x] Run the production Klove image and real Moonraker behind an internal-only
  network. Require Moonraker API-key authentication; expose no host port,
  device, Docker socket, host network, privilege, or generic command route.
- [x] Confirm typed pause/resume/cancel, unique history job-id/start-time
  binding, exact final token recheck, exact single dispatch, duplicate result
  reuse, stale-token pre-dispatch denial, post-action reconciliation, and
  invalid northbound/southbound authentication.
- [x] Drop one response after Moonraker receives pause and prove
  `outcome_unknown` remains fenced across idempotency keys without another
  dispatch.
- [x] Restart the printer host while Klove stays live and prove that ambiguity
  remains fenced after reconnect. Restart Klove and prove its new boot epoch
  denies the prior token without dispatch.
- [x] Bind each run to an exact validated run id, Compose project, Docker
  context, daemon ID, and daemon mode. Use rootless Docker locally, bounded
  operations, narrow volumes, generated credentials, and exact teardown.
- [x] Define a separate RatOS v2.1.0 ARM hardware procedure with immutable asset
  sizes/checksums, macro ownership, attended safety checks, and an evidence
  template. Do not call the native stack RatOS; full-system emulation is
  supplemental only when it faithfully boots the exact image.
- [x] Obtain clean GitHub PR CI and merge through protected `develop` in
  [PR #49](https://github.com/tomlawesome/klove/pull/49).

Exit criterion: a reproducible confined run passes against the pinned real
Klipper/Moonraker processes and retains negative, fault, and restart evidence;
the exact RatOS hardware procedure is reviewable and remains visibly pending
until actually executed.

## Active supplemental slice: RatOS controlled host-MCU contract

- [x] Keep the exact RatOS v2.1.0 raw image immutable and put every guest change
  in one fresh private COW that is destroyed after identity-checked teardown.
- [x] Install an exact test-only `kinematics: none` configuration and finite
  dwell job through Moonraker into that COW, then verify their bytes and the
  RatOS-supported Linux-process host MCU before starting production Klove.
- [x] Build and bind exact source-labelled tool and production Klove images,
  generated per-run credentials, rootless daemon identity, confinement,
  sidecars, and sanitized evidence to one deterministic lane-source digest.
- [x] Demonstrate on real RatOS-managed Klipper/Moonraker that Klippy reaches
  ready, production Klove observes the exact immutable history job, and pause,
  duplicate replay, stale denial, resume, and cancel retain single-dispatch and
  post-action production semantics.
- [x] Preserve fail-closed behaviour when a second job's northbound phase is
  visible before its immutable Moonraker history identity; a bounded fixture
  retry may re-snapshot only that pre-dispatch denial and never retries an
  action or `outcome_unknown`.
- [x] Make HUP, INT, and TERM exit every cleanup-owning lifecycle child so the
  wrapper always executes exact teardown; interrupted-run verification left no
  container, credential volume, COW, or active runtime state.
- [ ] Complete one uninterrupted exact-release run that also proves the
  deliberately lost response becomes `outcome_unknown` with no second dispatch
  and archives the atomic `contract-passed` evidence marker.
- [x] Prepare the fixture for protected `develop` with clean fast and native
  integration validation, without treating partial emulation evidence as
  acceptance.

Exit criterion: one exact source-bound run archives `contract-passed` after all
positive, negative, idempotency, stale-token, and lost-response assertions, then
destroys the credential volume and COW. Until then issue #51 stays open and the
partial observations are diagnostic evidence only.

## Completed slice: versioned artifact contracts

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
- [x] Obtain clean GitHub PR CI and merge through protected `develop`.

Exit criterion: untrusted artifact metadata can be represented and evaluated
without aliases or inference, while no archive extraction, Moonraker upload, or
print-start transport exists.

## Completed implementation: hostile `.gcode.3mf` validation

- [x] Supersede the unexposed v1 intent with a strict v2 contract carrying the
  exact canonical selected member path; no plate filename is inferred.
- [x] Require one immutable byte snapshot whose physical size and SHA-256 match
  intake, and retain that exact object in the successful candidate.
- [x] Bound EOCD/ZIP64 central-directory metadata before ZIP parsing and reject
  multi-disk archives, comments, unsupported versions/compression/encryption,
  traversal and cross-platform aliases, duplicates/case collisions, links and
  special files, malformed local headers, excess entries/bytes/ratios, and
  selected-member CRC failure.
- [x] Stream only the exact selected regular member through SHA-256, byte and
  line bounds, canonical ASCII checks, recognized supported-slicer structure,
  actual motion, and conservative known Bambu-only signature denial. Do not
  retain a second expanded G-code body or rewrite any command.
- [x] Return only byte-exact evidence or a bounded non-reflective denial. Treat
  success as a candidate for later target/profile policy, never dispatch
  authority.
- [x] Add deterministic adversarial fixtures, ZIP64/local-header/CRC cases, and
  bounded property fuzzing while retaining package-wide 100% statement and
  branch coverage.
- [x] Preserve the repository prohibition on extraction, artifact transport,
  generic G-code, and print start.

Exit criterion: hostile input becomes either one byte-exact, bounded selected
G-code candidate tied to its immutable source or a structured denial, without
filesystem, network, printer, or actuation effects.

## Completed implementation: exact target and safety-profile qualification

- [x] Supersede the unexposed v2 artifact contract with v3, separating hostile
  byte inspection from independently trusted target approval.
- [x] Give every configured printer a canonical UUID distinct from its route
  slug and reject duplicate UUIDs. Register zero or more explicit current
  safety profiles per printer without inventing compatibility from a model name.
- [x] Bind each profile to an exact slicer-profile id, positive generation,
  Klipper dialect, nozzle diameter, build volume, and plate identity using
  bounded integer micrometres and a canonical SHA-256 fingerprint.
- [x] Require exactly one approval binding its authority to the operation,
  idempotency key, immutable archive, selected path and G-code digest, exact
  target, and all safety fields. Missing, unknown, multiple, stale,
  contradictory, and every field mismatch fail closed with bounded denials.
- [x] Emit one immutable non-actuating qualification only after the inspection,
  approval, intent, and current configuration agree exactly.
- [x] Define an explicit per-file manual review record with actor, time, and
  reason while fixing automatic authority to false and denying every override
  from the automatic qualification path.
- [x] Record the trust boundary and config-invalidation rule in ADR 0003, the
  threat model, testing policy, examples, and repository engineering rules.
- [x] Retain 100% statement and branch coverage for the expanded configuration
  and artifact policy without adding upload, print start, generic G-code, or a
  user interface.

Exit criterion: one exact inspected artifact can become a target-bound,
non-actuating qualification only through independent current controller/slicer
approval; no human or inferred compatibility path can authorize automation.

## Completed implementation: bounded non-actuating Moonraker upload

- [x] Accept ADR 0004's stock-Moonraker file boundary without a host-installed
  Klove component, print-start method, generic G-code path, or user interface.
- [x] Recheck the exact current printer UUID/profile generation/fingerprint and
  re-inspect the retained immutable archive immediately before opening its exact
  selected member.
- [x] Serialize per printer and dispatch one checksum-verified multipart upload
  to `gcodes/klove/<operation-id>.gcode` with literal `print=false`.
- [x] Deduplicate exact operation/idempotency identities through one shared
  task, deny collisions and bounded-journal exhaustion, and retain every
  terminal result without a blind retry.
- [x] Require the exact HTTP 201 location and non-starting response identity,
  poll metadata immediately within bounded policy, and require exact path,
  timestamp, byte size, metadata UUID, command offsets, empty processor list,
  no prior job identity, and the configured nozzle diameter.
- [x] Stream the remote file back through exact size and SHA-256 verification
  between two identical metadata reads. Emit immutable `VerifiedUpload`
  evidence only after the whole bracket agrees.
- [x] Map every lost response, disconnect, timeout, mismatch, cancellation,
  metadata delay, and substitution after request start to `outcome_unknown` and
  never retry or delete an uncertain path.
- [x] Cover authentication headers, transport decoding, policy, idempotency,
  serialization, source/target rechecks, success, and every failure class with
  deterministic tests at 100% statement and branch coverage.

Exit criterion: one exact qualified selected G-code can be placed on one exact
Moonraker host and independently verified without starting it; no ambiguous
upload can become start authority.

## Completed implementation: durable at-most-once Moonraker print start

- [x] Accept ADR 0005's stock-Moonraker `printer.print.start` boundary without
  generic G-code, a host-installed component, a northbound route, or a user
  interface.
- [x] Require both installation-wide and per-printer opt-in, then recheck the
  exact printer UUID, current safety-profile generation/fingerprint and the
  complete ADR-0004 verified-upload identity under one per-printer lock.
- [x] Bracket the exact remote file's bounded size/SHA-256 stream with identical
  metadata reads and require a coherent idle history/object/history sample
  immediately before reservation.
- [x] Commit the operation id, idempotency key, complete verified file, target
  and idle preflight as `dispatching` before any network action. Validate an
  owner-only SQLite `STRICT` database, exact keys/schema, WAL mode and
  `synchronous=FULL`; persist it on the container state volume.
- [x] Send one authenticated typed start RPC for only the operation-derived
  filename. Treat a committed reservation as possibly dispatched across every
  timeout, cancellation, disconnect, malformed response, internal failure and
  process crash; never retry it.
- [x] Ignore the RPC response as success evidence. Confirm only from a strictly
  later event time and exact operation path plus a new immutable history
  identity and compatible live or terminal phase. Contradiction or bounded
  timeout is durably `outcome_unknown`.
- [x] Close the dispatch gate during startup/reconnect read-only reconciliation.
  Exact duplicates reuse the durable result, identity collisions deny, and any
  unresolved row atomically fences only that printer across all new keys and
  service instances.
- [x] Fail closed on missing, public, symlinked, corrupt, weakened-schema or
  unavailable storage and cover the domain, adapter, orchestration, restart and
  persistence policy at 100% statement and branch coverage.

Exit criterion: one exact verified upload can produce at most one typed
Moonraker start request and is reported confirmed only from later exact
history/live evidence. Every unresolved outcome survives Klove restart and
prevents another start on that printer without a blind retry.

## Accepted decision: embedded onboarding and minimal Grove KLOVE type

- [x] Confirm from current Grove source that serial and access-code limits fit a
  stable `KLOVE-<UUID>` proxy identity and a 20-character high-entropy access
  code, while model values affect scheduling and hardware feature visibility.
- [x] Keep Moonraker discovery, credentials, identity/capability probes,
  safety-profile binding, runtime registry, secrets, and setup/recovery in
  Klove.
- [x] Limit Grove changes to an explicit conservative `KLOVE` type, **Klipper
  via Klove** Add Printer path, sandboxed frame, strict versioned completion
  handoff, and unsupported-feature gates.
- [x] Freeze independent Klove owner authentication, bounded server-side setup
  sessions, CSRF/exact-origin policy, browser privacy rules, completion fields,
  upstream fork workflow, and exact-revision compatibility testing in ADR 0006.
- [x] Retire the broad native Grove provider epic and its slices as not planned;
  do not replace them with a persistent private Grove fork.

Exit criterion: the repository and GitHub roadmap agree on the smallest secure
onboarding boundary, with no new actuator or unauthenticated setup path
authorized by the decision.

## Implemented slice: direct probe and lifecycle orchestration

- [x] Accept only canonical HTTP(S) origins and deployment-allowed DNS/IP
  answers; validate every answer and pin one exact address for the complete
  bounded probe.
- [x] Authenticate only through `X-Api-Key`, bound time and response bytes, and
  require two equal strict `server.info`, `printer.info`, and
  `printer.objects.list` snapshots before deriving capabilities.
- [x] Bind typed create, update, both credential rotations, disable, and removal
  to exact UUIDs, endpoints, profile sets, revisions, actor/origin evidence,
  keyed request fingerprints, and durable idempotency reservations.
- [x] Re-probe immediately before every create/update/Moonraker-credential
  commit; require composite control/print-start fence evidence for every
  existing-printer mutation; preserve disable-before-remove tombstones.
- [x] Generate compatibility credentials from exactly 15 random bytes, keep
  their values out of service results, reject no-op rotations, and reconcile
  pre-commit and post-commit secret failures without repeating a probe.
- [x] Cover endpoint/SSRF, transport, decoding, drift, concurrency, restart,
  redaction, lifecycle, persistence, and fault paths at 100% statement and
  branch coverage without adding a route, UI, or actuator.

Exit criterion: issue #63's library boundary can transact one exact canonical
printer lifecycle from fresh direct evidence, while #64–#65 remain required
before any product caller can reach it. The complete contract is
`docs/onboarding-core.md`.

## Next slices

The completed authorized component slices are #5, #8, #9, #62, and #63. ADR
0006 makes the secure runtime registry the current implementation chain. Its
private persistence
and secret-store foundation is implemented under #62 — registry storage, the direct
probe/orchestration library is implemented under #63 — onboarding core, and
registry-backed monitor activation is implemented under #68 — runtime
supervisor. Shared admission remains #69 — shared runtime gate, startup wiring
remains #70 — runtime bootstrap, and protected API work remains #65 — protected
onboarding API. The end-to-end lifecycle proof in #12 — onboarded lifecycle
proof consumes that canonical onboarded printer.
The implemented RatOS contract fixture remains a separate incomplete
exact-release acceptance follow-up under #51 and does not block focused
development. Each safety-critical prerequisite is delivered through its own
protected `develop` pull request and must merge with required checks green
before work begins on the next dependent implementation. ADR 0004 upload
remains non-actuating by itself; ADR 0005 print start is internal and is not a
public dispatch workflow.

1. Keep the native integration lane as a required regression gate and execute
   the RatOS v2.1.0 procedure on supported ARM hardware before stable
   promotion. Track this under [integration #48](https://github.com/tomlawesome/klove/issues/48)
   and [stable promotion #3](https://github.com/tomlawesome/klove/issues/3).
   The rootless full-system feasibility work under
   [spike #50](https://github.com/tomlawesome/klove/issues/50) now boots and
   probes the exact RatOS v2.1.0 release kernel/base image and managed services
   under a confined Pi 3B QEMU lane. The separate
   [virtual-MCU/configuration spike #51](https://github.com/tomlawesome/klove/issues/51)
   is required before the Klove contract can run there; supported hardware
   remains the release-acceptance authority. This validation work authorizes no
   new actuator.
2. Complete the one canonical runtime printer registry chain under
   [#58](https://github.com/tomlawesome/klove/issues/58). Foundation
   [#62](https://github.com/tomlawesome/klove/issues/62) supplies the private
   exact-schema database, external owner-only secrets, typed lifecycle journal,
   reconciliation, and backup boundary. #63 supplies the direct probe and typed
   lifecycle service. Then #64–#65 add dynamic fleet activation with shared
   lifecycle/actuator admission and the owner-protected API. The
   chain replaces per-printer TOML as the normal product onboarding path and
   adds no actuator or dashboard.
3. Integrate exact target/safety-profile binding, verified upload, durable
   print start, and reconciliation from that onboarded registry entry. Each
   actuator remains limited to its accepted ADR; roadmap placement alone is not
   authorization. Track the lifecycle proof in
   [#12](https://github.com/tomlawesome/klove/issues/12) under
   [artifact-dispatch epic #33](https://github.com/tomlawesome/klove/issues/33).
4. Complete the current-Grove bridge in dependency order: licence/provenance
   [#11](https://github.com/tomlawesome/klove/issues/11), embedded setup/recovery
   [#59](https://github.com/tomlawesome/klove/issues/59), conservative MQTT/TLS
   and FTPS facade issues #10/#14, minimal upstream `KLOVE` contribution
   [#60](https://github.com/tomlawesome/klove/issues/60), and operations guidance
   #13. Track the full order under
   [Grove-bridge epic #32](https://github.com/tomlawesome/klove/issues/32).
5. Separately decide and test bounded temperature/speed plus explicitly
   mapped fan/light controls. Keep jog and extrusion disabled until proven;
   [decision #15](https://github.com/tomlawesome/klove/issues/15) gates
   [live-control epic #37](https://github.com/tomlawesome/klove/issues/37).
6. Fleet hardening: durable journal, multi-printer fault isolation, cameras,
   metrics, backup/restore, migrations, and restart/fault/soak tests, tracked in
   [fleet epic #34](https://github.com/tomlawesome/klove/issues/34).
7. Later adapters: exclude-object, richer cameras, MMU/toolchanger support,
   optional outbound host agent, and additional printer stacks, tracked in
   [adapter epic #35](https://github.com/tomlawesome/klove/issues/35).
