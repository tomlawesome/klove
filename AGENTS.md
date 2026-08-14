# Klove engineering rules

## Product boundary

Klove is a headless, automation-first security and translation layer. Grove
owns normal user interaction. Prefer direct structured evidence and automatic
reconciliation between Grove, Klove, and Moonraker over human input. Expose the
smallest practical machine-facing surface. A Klove-local Web UI is a last resort
for a demonstrated irreducible human choice or recovery action and requires its
own accepted architecture decision; never add a dashboard, configuration UI, or
duplicate workflow for convenience.

## Safety invariant

Klove fails closed. A command is permitted only when every required piece of
identity, target, capability, state, parameter, and transition evidence is
positive, current, internally consistent, and unambiguous. Unknown or missing
evidence is a denial. Never infer printer capabilities from names, models, or
near matches.

Actuation is limited to the accepted job-control contract in ADR 0001:
Moonraker's dedicated pause, resume, and cancel RPCs behind explicit global and
per-printer opt-in. Every request requires exact authenticated scope, printer
route, state token, job identity, phase, capability, a direct pre-action poll,
per-printer serialization, single-dispatch idempotency, and post-action
reconciliation. The exact token is required both before and after the direct
poll; every live poll is bound to Moonraker's immutable history job id and
start time. Once dispatch may have occurred, uncertainty is
`outcome_unknown`; the exact printer and state token remain fenced across all
idempotency keys until changed control evidence supplies a new token. Ordinary
telemetry and forward print progress do not rotate the token or cross that
fence. Printer owners are
responsible for the semantics and safety of their configured `PAUSE`, `RESUME`,
and `CANCEL_PRINT` macros.

No generic G-code execution path or print-start transport is permitted. Any new
actuator requires its own accepted architecture decision, narrow typed
interface, negative tests, and 100% statement and branch coverage across
authentication, authorization, control, decoding, translation, and policy.

Hostile artifact validation accepts only one bounded immutable archive snapshot
with an exact selected member path. It never extracts the archive, infers a
plate, or rewrites G-code. Target qualification additionally requires one
independently trusted approval bound to the exact inspected bytes, operation,
canonical printer UUID, current safety-profile generation and fingerprint,
registered slicer profile, nozzle, build volume, plate, and Klipper dialect.
Names, models, near matches, self-asserted archive text, and manual overrides
never authorize automation. Upload and print start remain prohibited until
their own accepted decision and later safety slices are complete.

## Delivery lanes

- Ordinary work branches from and targets protected `develop`.
- A release candidate is merged from `develop` to protected `preview` only
  after fast and integration checks pass.
- A `preview` push builds, scans, attests, and publishes one uniquely identified
  container digest.
- Protected `main` accepts only a PR from the exact tested `preview` revision.
- Stable promotion re-tags the accepted digest; it never rebuilds it.
- The repository has one maintainer. Pull requests and resolved conversations
  are required, but approving reviews are not.

Do not bypass required tests. Production approval may be self-reviewed and
administrators may bypass protection when consciously handling an emergency.

## Local checks

Install `requirements-dev.lock` with `--require-hashes`, then run
`scripts/test-fast.ps1` on Windows or `scripts/test-fast.sh` elsewhere. Keep CI
actions, base images, and scanner images pinned to reviewed immutable digests.

Run `scripts/test-moonraker-sim.sh` after changing Moonraker
protocol/control, preflight or reconciliation behavior, the integration
fixture, its container pins, or restart/fault handling. Local execution requires
rootless Docker. Use only the unique run-id lifecycle scripts so cleanup stays
bound to the recorded Docker context, daemon, and exact Compose project. The
fixture may prepare its private harmless virtual-SD job, but that test mechanism
must never become a production Klove upload, print-start, or generic G-code
path. The native stack is not RatOS; RatOS acceptance uses the separate pinned
ARM hardware procedure in `docs/ratos-acceptance.md`.

Use `scripts/test-ratos-emulation.sh` only for the opt-in supplemental RatOS
v2.1.0 host/service and controlled job-control contract lane. It must remain
rootless, outer-networkless, capability-free, resource-bounded, digest-verified,
COW-backed, and absent from routine CI and release artifacts. Each evidence run
must start with a fresh COW over the immutable verified base; that COW is the
only writable guest disk, and raw guest serial output is not retained. The outer
container also receives one separately writable, exact-labelled credential
volume. The test-only fixture may use Moonraker to place the exact controlled
configuration and harmless finite-dwell job in that COW, but production Klove
still exposes no upload, print-start, or generic G-code path. Per-run credentials
live only in an exact labelled volume; teardown destroys that volume and the
secret-bearing COW while retaining bounded sanitized evidence.
Direct-loading the exact kernel and device tree bypasses the physical Pi firmware
path. The lane may be described only as exact-release Linux-process host-MCU and
dedicated job-control contract evidence—not board, configured-macro, motion,
physical-MCU, or physical acceptance.

Treat each ARM-on-x86 TCG cold boot as a scarce acceptance run. After a failure,
diagnose from bounded evidence and reproduce the defect with focused or native
tests before changing the lane. Never repeat an unchanged boot; use a new fresh
COW only for an exact-boundary fix or one final complete acceptance record.

Never commit credentials. Configuration names secret files; secret values live
only in untracked, narrowly mounted files.
