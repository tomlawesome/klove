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
reconciliation. Once dispatch may have occurred, uncertainty is
`outcome_unknown`; the exact printer and state token remain fenced across all
idempotency keys until a newly observed token is supplied. Printer owners are
responsible for the semantics and safety of their configured `PAUSE`, `RESUME`,
and `CANCEL_PRINT` macros.

No generic G-code execution path or print-start transport is permitted. Any new
actuator requires its own accepted architecture decision, narrow typed
interface, negative tests, and 100% statement and branch coverage across
authentication, authorization, control, decoding, translation, and policy.

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

Never commit credentials. Configuration names secret files; secret values live
only in untracked, narrowly mounted files.
