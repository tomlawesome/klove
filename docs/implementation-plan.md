# Klove implementation plan

Status: active  
Last updated: 2026-08-21

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
- [#11 — Grove provenance](https://github.com/tomlawesome/klove/issues/11)
  accepts ADR 0008's clean-room compatibility implementation, independent
  visual work, exact-revision fixture provenance, licence/SBOM gates, and
  normal upstream contribution terms for #32 — Grove bridge, #59 — embedded
  setup/recovery, and #60 — minimal Grove contribution.
- [#58 — registry epic](https://github.com/tomlawesome/klove/issues/58) is
  complete through #62 — registry storage, #63 — onboarding core, #64 —
  runtime fleet, and #65 — protected onboarding API. #71 — owner session, #72
  — lifecycle routes, and #73 — runtime handoff complete the protected API.
  #59 — embedded setup/recovery is also complete through #77–#79. #10 — MQTT
  facade, #14 — FTPS ingress, #60 — minimal Grove contribution, and #13 —
  deployment guidance remain under #32 — Grove bridge.
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
  is implemented under accepted ADR 0005 with no northbound route. ADR 0007's
  authenticated dispatch-ingress contract is implemented by #75 — dispatch
  coordinator and proven by #76 — native dispatch proof, completing issue #12
  — end-to-end dispatch.
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
- [x] Split the RatOS orchestration shell into explicit ownership, QEMU,
  evidence, preparation, probe, contract, and teardown boundaries without
  changing the accepted lane or consuming a QEMU boot.

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

## Accepted decision: clean-room Grove provenance

- [x] Pin the supported upstream to Grove commit
  `cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4` and review its root AGPL
  declaration, missing per-file exceptions, vendored notices, dependencies,
  assets, and contribution process.
- [x] Prohibit Grove source, test, fixture, dependency, generated-bundle, and
  asset reuse in Klove; require isolated black-box observation and independent
  implementation.
- [x] Classify Grove branding, icons, fonts, screenshots, exact design tokens,
  layouts, and prose as non-reusable; permit only independently authored
  semantic visual roles, functional patterns, and ADR-defined factual labels.
- [x] Define exact-revision fixture manifests, sanitization, SHA-256 identity,
  notice inventory, complete release SBOM/licence evaluation, and drift gates.
- [x] Require #60 — minimal Grove contribution to use upstream's issue,
  assignment, short-lived fork, repository-licence, test, documentation, and
  pull-request process without creating a supported private fork.

Exit criterion: #32 — Grove bridge, #59 — embedded setup/recovery, and #60 —
minimal Grove contribution can proceed without copying Grove implementation or
visual expression into Klove, and every compatibility claim is bound to
auditable exact-revision evidence.

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

## Accepted decision: authenticated dispatch ingress

- [x] Accept one asynchronous internal contract spanning intake,
  qualification, verified upload, durable start, and exact terminal history.
- [x] Require an independently authenticated `printers:dispatch` principal
  bound to one canonical active registry printer; Grove, browser, setup,
  private-network, model, name, and caller target claims grant nothing.
- [x] Freeze strict operation/idempotency identity, exact current
  target/profile binding, bounded streamed input, private spool ownership,
  retention, durable result lookup, and non-enumerating authorization.
- [x] Make `uploading` a durable no-retry boundary, preserve ADR 0005's start
  journal authority, and require read-only restart/completion reconciliation.
- [x] Limit cancellation to preventing an upload or start that has not crossed
  its durable action boundary; it never becomes remote cleanup or a second job
  cancel path.
- [x] Implement #75 — dispatch coordinator as the internal composition layer:
  it reserves streamed hostile input before reading it, retains an owner-only
  exact source and durable coordinator row, composes qualification/upload/start
  under shared admission, and closes restart ambiguity without a blind retry.
- [x] Implement #76 — native dispatch proof in the confined real Moonraker
  fixture. It starts only from a bracketed coherent idle state, drives the
  production coordinator/upload/start/journal/admission stack with one bounded
  test artifact, proves grant/profile/source-byte/idempotency binding,
  cancellation, duplicate/substitution and cross-printer isolation, then
  resolves a deliberately lost typed-start response from a fresh process
  without another upload or start. Local native acceptance and required CI
  passed in PR #102.

Exit criterion: #75 — dispatch coordinator is implemented without a northbound
adapter or additional actuator. #76 — native dispatch proof passed against the
real pinned Moonraker fixture and the complete chain is merged into protected
`develop`.

## Next slices

The canonical runtime registry, protected onboarding API, embedded
setup/recovery surface, authenticated dispatch coordinator, and native
intake-through-completion proof are complete. ADR 0004 upload remains
non-actuating by itself; ADR 0005 print start remains internal; no public or
generic G-code path exists.

1. Finish [#51 — RatOS virtual-MCU proof](https://github.com/tomlawesome/klove/issues/51)
   only after focused diagnosis justifies one fresh-COW exact-release run. The
   run must prove the lost-response fence and archive `contract-passed`.
   Supported hardware remains release-acceptance authority.
2. Complete [#32 — Grove bridge](https://github.com/tomlawesome/klove/issues/32).
   Obtain exact ADR-0008-compliant black-box wire evidence before accepting and
   implementing proposed ADR 0009 under #10 — MQTT facade or proposed ADR 0010
   under #14 — FTPS ingress. Then deliver #60 — minimal Grove contribution and
   #13 — deployment guidance against one pinned supported Grove revision.
3. Complete [#3 — stable promotion](https://github.com/tomlawesome/klove/issues/3)
   only after documented production-like printer acceptance. Promote the exact
   tested preview digest without rebuilding. Resolve #39 — delivery roadmap
   project when account-level GitHub Projects write permission is available.
4. Separately decide and test bounded temperature/speed plus explicitly mapped
   fan/light controls. Keep jog and extrusion disabled until proven;
   [#15 — live-control safety envelope](https://github.com/tomlawesome/klove/issues/15)
   gates [#37 — live-control programme](https://github.com/tomlawesome/klove/issues/37).
5. Harden fleet recovery, fault isolation, observability, backup/restore,
   migrations, and soak behavior under
   [#34 — fleet hardening](https://github.com/tomlawesome/klove/issues/34).
   ADR 0011 accepts the first ordered boundary: registry-only transactional
   migrations and an immutable exact-version harness, with no production schema
   change in the framework slice. #108 implements that runner and exact
   version-1 harness. ADR 0012 and #110 implement the next exact version-2
   slice: append-only capability-mapping and safety-profile history, atomic
   current/history commits, deterministic v1 backfill, immutable triggers, and
   bounded per-printer audit reads. History adds no current authority, fence
   reference, or backup behavior. ADR 0013 and #112 define the next
   decision-only slice: a registry-owned durable fence catalogue, durable
   control uncertainty, conservative cross-store commit ordering, and exact
   recovery-set identities. Its implementation remains a separate issue;
   operational backup/restore remains later work.
6. Evaluate exclude-object, richer cameras, MMU/toolchanger support, an
   outbound host agent, and other printer stacks only through separately
   accepted adapter boundaries under
   [#35 — optional adapters](https://github.com/tomlawesome/klove/issues/35).

The retired native Grove-provider programme remains closed and must not be
revived without a new accepted decision.
