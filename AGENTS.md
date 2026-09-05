# Klove engineering rules

## Communication

- Keep every user-facing message short, direct, and easy to scan.
- Use simple terms. State only the result, blocker, or next action.
- Do not narrate routine work, repeat context, or add detail unless the user asks.

## Session handoff

If the user asks you to look at the handoff, it is located at
`/home/codex/projects/klove/.agents/handoffs/current.md`.

## Product boundary

Klove is a headless, automation-first security and translation layer. Grove
owns normal user interaction. Prefer direct structured evidence and automatic
reconciliation between Grove, Klove, and Moonraker over human input. Expose the
smallest practical machine-facing surface. ADR 0006 accepts one narrow Web UI
exception for irreducible printer setup and recovery: Klove owns the embedded
Grove-themed surface, runtime registry, Moonraker credentials, direct probes,
stable identity, and safety-profile binding. It must never grow into a
dashboard, routine configuration UI, printer control surface, or duplicate
Grove workflow.

Grove integration is limited to an explicit conservative `KLOVE` printer type,
one **Klipper via Klove** Add Printer path, the embedded setup frame, a strict
versioned completion handoff into Grove's existing printer-create flow, and
feature gates that hide unsupported Bambu behavior. Grove must never receive
Moonraker credentials or implement Klipper semantics. Do not revive the retired
native-provider programme or create a persistent Grove fork without a new
accepted decision. Any Grove contribution is developed in a fork and proposed
upstream normally.

The registry and onboarding core implemented by issues #62–#63 are the only
canonical product printer store and lifecycle boundary. Keep secret values
outside SQLite in its owner-only opaque-reference store, keep all lifecycle mutations typed,
revision-bound, idempotently reserved, and bound to fresh direct probe evidence
where required, and preserve its two-phase create/rotate/remove recovery
protocol. The probe accepts only a canonical origin and exact deployment CIDR
allowlist, validates every DNS answer, pins one peer address, bounds time and
bytes, and requires two equal strict identity/capability snapshots. Discovery
advertisements never authorize a route. Every existing-printer mutation must
prove the composite control/print-start fence clear; issue #64 runtime wiring
must serialize that proof and commit with all new actuator admission through
one shared per-printer gate. Reconciliation must prove that cleanup references
are disjoint from all active and disabled records before deletion.
A recoverable snapshot comprises the registry database, complete secret
directory and HMAC key, and every separate durable actuator fence from one
quiesced state; never restore or document any subset as sufficient. See
`docs/registry-storage.md`.
The probe and lifecycle transaction contract is `docs/onboarding-core.md`.

## Safety invariant

Klove fails closed. A command is permitted only when every required piece of
identity, target, capability, state, parameter, and transition evidence is
positive, current, internally consistent, and unambiguous. Unknown or missing
evidence is a denial. Never infer printer capabilities from names, models, or
near matches.

Actuation is limited to ADR 0001's dedicated pause, resume, and cancel RPCs and
ADR 0005's one exact `printer.print.start` RPC. Both require explicit global and
per-printer opt-in, exact target and current evidence, a direct pre-action poll,
per-printer serialization, single dispatch, and post-action reconciliation.
Job control additionally requires the exact authenticated scope, printer route,
state token, job identity, phase and capability; the token is checked both
before and after the direct poll, and every live poll is bound to Moonraker's
immutable history job id and start time. Once control dispatch may have
occurred, uncertainty is
`outcome_unknown`; the exact printer and state token remain fenced across all
idempotency keys until changed control evidence supplies a new token. Ordinary
telemetry and forward print progress do not rotate the token or cross that
fence. Printer owners are
responsible for the semantics and safety of their configured `PAUSE`, `RESUME`,
and `CANCEL_PRINT` macros.

Print start may consume only an exact ADR-0004 `VerifiedUpload`. It rechecks the
current target profile and brackets the remote size/SHA-256 with identical
metadata reads, then requires a coherent idle history/object/history preflight.
The complete operation is committed to the private SQLite/WAL journal before
one typed start request. A committed `dispatching` row always means the call may
have happened. Confirmation requires the exact operation path, a new immutable
history identity and strictly later monotonic evidence. Startup and reconnect
perform read-only reconciliation before opening the dispatch gate; unresolved
evidence fences that printer across all keys. After reservation there is never
a blind retry, including across process restart.

No generic G-code execution path or other print-start transport is permitted.
Any new actuator requires its own accepted architecture decision, narrow typed
interface, negative tests, and 100% statement and branch coverage across
authentication, authorization, control, decoding, translation, and policy.

Hostile artifact validation accepts only one bounded immutable archive snapshot
with an exact selected member path. It never extracts the archive, infers a
plate, or rewrites G-code. Target qualification additionally requires one
independently trusted approval bound to the exact inspected bytes, operation,
canonical printer UUID, current safety-profile generation and fingerprint,
registered slicer profile, nozzle, build volume, plate, and Klipper dialect.
Names, models, near matches, self-asserted archive text, and manual overrides
never authorize automation.

The accepted ADR-0004 upload boundary is file-only and non-actuating. It may
consume only an exact current qualification, re-inspect the retained immutable
archive immediately before its request, and write once to the operation-unique
`gcodes/klove/<operation-id>.gcode` path with Moonraker checksum verification
and literal `print=false`. Per-printer serialization and bounded idempotency
apply. Returned identity, metadata, configured nozzle, byte count and remote
SHA-256 must agree, with identical metadata reads bracketing the remote-file
download. Once the upload request may have begun, every ambiguity is
`outcome_unknown` and must never cause a blind retry or uncertain-path deletion.
Verified upload evidence grants no authority by itself. Only ADR 0005 may
consume it after repeating exact target, file and live-state checks and making
its durable pre-dispatch reservation.

## Delivery lanes

- Ordinary work branches from and targets protected `dev`.
- A release candidate is merged from `dev` to protected `preview` only
  after fast and integration checks pass.
- A `preview` push builds, scans, attests, and publishes one uniquely identified
  container digest.
- Protected `main` accepts only a PR from the exact tested `preview` revision
  (`verify:preview` in `.gitlab-ci.yml` proves it against the published digest).
- Stable promotion re-tags the accepted digest; it never rebuilds it.
- The repository has one maintainer. Pull requests and resolved conversations
  are required, but approving reviews are not.

Do not bypass required tests. Production approval may be self-reviewed and
administrators may bypass protection when consciously handling an emergency.

## Where development happens: GitLab, with GitHub as the mirror

Owner decision, 2026-09-04, tracked on #171: Klove is developed on the
self-hosted GitLab, project `ai/klove` (id 52), remote name `gitlab`. Branches,
merge requests and the `dev` → `preview` → `main` promotions happen there;
GitHub (`origin`) is the public mirror. Issues, the delivery board
(`docs/board-views.md`) and decisions live on GitLab too; the GitHub tracker
was imported keeping every issue and pull request number, and the GitHub
Projects board is closed.

The default branch is `dev`, so `Closes #N` on an ordinary merge closes its
issue; check the issue after every merge anyway.

Git access from the agent host: SSH to the instance is unreachable, so the
`gitlab` remote is HTTPS and pushes hand git the calling assistant's own
`glab` token through `~/.local/bin/gl-git-askpass`:

    GIT_ASKPASS=gl-git-askpass GIT_TERMINAL_PROMPT=0 git push gitlab <branch>

The helper reads `GLAB_CONFIG_DIR`, so each assistant pushes with its own
credential (github-credentials skill). No other credential is specific to this
project.

## Local checks

`.gitlab-ci.yml` is the gate that runs on GitLab; container publishing,
scanning and attestation still happen on GitHub from the mirrored `preview` push.

Install `requirements-dev.lock` with `--require-hashes`, then run
`scripts/test-fast.ps1` on Windows or `scripts/test-fast.sh` elsewhere. Keep CI
actions, base images, and scanner images pinned to reviewed immutable digests.

Run `scripts/test-moonraker-sim.sh` after changing Moonraker protocol/control,
print-start, preflight or reconciliation behavior, the integration fixture, its
container pins, or restart/fault handling. Local execution requires rootless
Docker. Use only the unique run-id lifecycle scripts so cleanup stays bound to
the recorded Docker context, daemon, and exact Compose project. The fixture may
prepare its private harmless virtual-SD job, but that test mechanism
must never become a production Klove upload or generic G-code path and must not
bypass ADR 0005's journal when testing production print start. The native stack
is not RatOS; RatOS acceptance uses the separate pinned ARM hardware procedure
in `docs/ratos-acceptance.md`.

Use `scripts/test-ratos-emulation.sh` only for the opt-in supplemental RatOS
v2.1.0 host/service and controlled job-control contract lane. It must remain
rootless, outer-networkless, capability-free, resource-bounded, digest-verified,
COW-backed, and absent from routine CI and release artifacts. Each evidence run
must start with a fresh COW over the immutable verified base; that COW is the
only writable guest disk, and raw guest serial output is not retained. The outer
container also receives one separately writable, exact-labelled credential
volume. The test-only fixture may use Moonraker to place the exact controlled
configuration and harmless finite-dwell job in that COW, but the lane does not
invoke production upload or print start and production Klove exposes no generic
G-code path. Per-run credentials
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

Configuration names secret files; secret values live only in untracked,
narrowly mounted files.
