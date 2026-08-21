# Testing policy

Klove treats authentication, authorization, command decoding, translation, and
safety policy as critical boundaries. They must maintain 100% statement and
branch coverage. The current package-wide gate is also held to that threshold.

Coverage is evidence that each implemented branch was exercised, not proof
that the protocol model is complete. The suite therefore also includes:

- negative and property tests for hostile or ambiguous Grove payloads;
- reducer tests for stale, malformed, contradictory, and incomplete state;
- deterministic WebSocket contract tests against a fake Moonraker peer;
- a confined real-process lane using pinned native Klipper with its
  Linux-process MCU, pinned Moonraker, and the production Klove image;
- API tests for missing, malformed, and insufficient credentials;
- registry and onboarding-core tests for exact UUID/endpoint/profile identity,
  canonical-origin parsing, complete-answer CIDR policy and connection pinning,
  bounded authenticated HTTP/JSON decoding, coherent direct probe snapshots,
  keyed operation collisions, external owner-only secret references,
  transactional create/update/rotate/disable/remove, composite actuator fences,
  retry serialization, restart and partial failure, post-commit cleanup
  ambiguity, canonical JSON, corrupt/future/weakened schema, quiesced
  backup/restore, active-reference cleanup denial, orphan/missing secrets,
  property inputs, and credential redaction; later runtime, API, authentication,
  and browser slices add coverage at their own boundaries;
- scripted browser tests for independent Klove owner authentication, session
  expiry/replay/concurrency, CSRF, exact Origin and CSP `frame-ancestors`, the
  sandboxed parent/frame ready-and-nonce state machine, strict versioned
  completion decoding, cancellation, hostile inputs, no browser persistence,
  responsive layouts, keyboard navigation, accessibility, and recovery outside
  the frame;
- Grove contract and browser tests pinned to the supported source revision for
  `KLOVE` create-field limits, stable proxy identity, successful and failed
  handoff, image fallback, exact-printer scheduling, model-compatibility bypass,
  unsupported Bambu feature suppression, and regression of existing Bambu
  printer types;
- control tests for exact token/job/state matching, per-printer serialization,
  history job-id/start-time bracketing, telemetry-stable control tokens, final
  exact-token rechecks, single dispatch, cross-key uncertainty fencing,
  idempotency conflicts and exhaustion, postcondition binding, and every
  post-dispatch ambiguity;
- accepted and rejected artifact-contract fixtures plus negative tests for
  canonical identities, separate inspection and trusted approval, exact
  UUID/profile/generation/fingerprint binding, every nozzle/volume/plate/dialect
  mismatch, config invalidation, manual-override denial, unknown, missing,
  stale, contradictory and ambiguous evidence, internally inconsistent metrics,
  and every configured intake limit;
- hostile ZIP/ZIP64 tests for traversal and aliases, duplicates, links,
  encryption, unsupported features, malformed local/central records, count,
  metadata, expanded-byte and exact ratio limits, missing exact selection, and
  selected-member CRC; bounded G-code tests cover controls, line endings,
  header/line limits, slicer structure, motion, comment handling, and known
  Bambu-only signatures, with property tests over arbitrary ZIP-like bytes;
- deterministic Moonraker upload tests for exact authenticated multipart
  fields, checksum and `print=false`, strict response/location decoding,
  immediate bounded metadata polling, metadata identity and nozzle checks,
  remote download size/digest, substitution bracketing, per-printer
  serialization, operation/key collisions, journal exhaustion, exact duplicate
  single dispatch, stale profile/source denial, every malformed response and
  every post-request ambiguity without retry;
- durable print-start tests for both opt-in gates, exact target/profile and
  remote metadata/digest rechecks, coherent idle history/object/history
  preflight, operation/key collisions, per-printer serialization, a committed
  pre-dispatch SQLite/WAL reservation, one exact authenticated
  `printer.print.start` request, strictly later event/history confirmation,
  terminal-state reconciliation, every lost-response and contradiction path,
  restart/reconnect gating, cross-key and cross-service printer fences, corrupt
  or weakened schemas, unsafe storage permissions, and every journal failure
  boundary;
- authenticated dispatch-ingress tests for exact principal/scope/printer
  grants, strict metadata and streamed-body bounds, digest/length mismatch,
  partial intake cleanup, operation/key collisions, private spool permissions,
  every upload/start durable checkpoint, cancellation races, client disconnect,
  restart and reconnect reconciliation without retry, exact terminal history,
  non-enumerating result authorization, redaction, capacity exhaustion, source
  retention, recovery-set consistency, and concurrent-printer isolation;
- repository policy tests that reject the enumerated prohibited RPC literals,
  confine the three accepted job-control RPC literals and the one accepted
  print-start literal to their separate typed adapters, constrain the native
  mutating route count, and reject unpinned CI actions;
- a preview-container contract assertion, vulnerability scan, SBOM, and
  provenance attestation.

The native integration lane verifies real Moonraker API-key rejection,
WebSocket discovery, the exact observed job, typed pause/resume/cancel, stale
token denial, unique Moonraker history identity, duplicate single dispatch, and
a dropped post-dispatch response that becomes a cross-key `outcome_unknown`
fence. It then restarts the printer
host while Klove remains live to prove the fence survives remote reconnect, and
restarts Klove to prove its new boot epoch rejects the prior token without a
dispatch.

Run the fast gate with `scripts/test-fast.ps1` or `scripts/test-fast.sh` after
installing `requirements-dev.lock` using pip's `--require-hashes` option. GitHub
Actions is the authoritative clean environment. The expensive exact-container
publication job runs only on protected `preview` pushes, never on hotfix or
arbitrary pull-request code. It first publishes a unique human-readable
`preview-<run>-<attempt>` candidate tag, records its digest, and moves the
mutable `preview` pointer to that exact digest without rebuilding.

Run the separate Docker integration gate with
`scripts/test-moonraker-sim.sh`. It is not part of the coverage process, but its
dedicated CI job gates Moonraker-affecting delivery and all preview publication.
Local runs require rootless Docker; hosted CI's rootful exception is explicit
and confined to that least-privilege job. The fixture uses an internal-only
network, generated ephemeral credentials, unique project identity, exact
context/daemon-bound cleanup, and finite Docker timeouts.

Only the private fixture prepares its harmless virtual-SD dwell job. That
preparation is not the production upload or ADR-0005 durable start path and
does not authorize generic G-code in `src/klove`. The new start adapter and
journal are covered by deterministic protocol/fault tests; issue #12 —
end-to-end dispatch owns the complete real-process intake-through-completion
contract. The native amd64
stack also does not claim RatOS coverage. RatOS
v2.1.0 acceptance separately records the exact ARM release asset checksum,
supported board, running software identities, controlled configuration and
macro hashes, and attended physical results under `docs/ratos-acceptance.md`.

An opt-in supplemental full-system lane is available as
`scripts/test-ratos-emulation.sh`. It verifies the exact RatOS v2.1.0 Raspberry
Pi archive, preserves its expanded raw disk read-only, direct-loads the matching
release kernel and DTB into QEMU's Pi 3B model, and writes only to a private COW
overlay created fresh for each evidence run. Its fixed-snapshot tool image and
unprivileged QEMU process receive the release inputs as separate read-only
mounts; the run COW is the only writable guest disk. One separately writable,
exact-labelled volume carries only per-run credentials and configuration to the
unprivileged processes. The rootless outer container is
networkless, capability-free, resource-bounded, and exposes guest services only
to its own loopback. After the identity probe, a test-only helper places one exact
controlled `kinematics: none` configuration and finite-dwell job into the fresh
COW through Moonraker, verifies their bytes, and uses RatOS's supported
Linux-process host MCU. The exact production Klove image then runs behind the
shared fault proxy and exercises the existing pause/resume/cancel contract,
including invalid Klove bearer authentication, stale and duplicate requests,
single dispatch, and lost-response fencing. Stock RatOS treats the isolated
slirp/loopback path as a trusted Moonraker client; this lane requires that exact
behavior and does not claim invalid downstream API-key rejection. The native
simulation retains that negative southbound test on an untrusted transport.
The test-only RatOS preparation is not production upload authority. ADR 0004's
production file transport and ADR 0005's durable start service are not
exercised by this lane; no generic G-code behavior is added.

The lane records bounded non-secret RatOS/Moonraker/Klipper/MCU and contract
identities. Exact teardown checks the overlay, removes only source-bound
containers, destroys the per-run credential volume and secret-bearing COW, and
retains only their digests plus sanitized evidence. Every archived run carries
its own prepared-input, tool-image, production-image, daemon, Git-revision, and
deterministic lane-source provenance, plus atomic probe and contract markers.
Probe and contract execution require an exact current source match. Teardown
intentionally uses the recorded origin and exact target identities instead, so a
later local edit cannot strand the credential volume or COW; any cleanup failure
is printed and retained for explicit recovery.
It requires x86-64 Linux, GNU coreutils, rootless Docker, and at least 16 GB free
for a fresh preparation. It is not in CI because the input is about 2.1 GB and
ARM-on-x86 TCG boot is slow. It does not prove configured macro semantics,
physical MCU behavior, motion, or attended hardware acceptance.

Treat a RatOS cold boot as a scarce acceptance operation. After a failure,
archive or inspect only the bounded evidence, reproduce the defect in the
native fixture or focused tests, and verify the fix there first. Do not repeat
an unchanged boot. Run another fresh COW only when a deterministic change needs
the exact-release boundary or when one final complete acceptance record is
required.

Pytest prepends `src` to its import path, and a repository-policy assertion
verifies that the suite imported Klove from the working tree. This prevents a
non-editable or stale environment installation from producing misleading
coverage for code other than the source under review.

The registry and onboarding core add no browser dependency or UI implementation.
Issue #59 must introduce a repeatable Playwright entry point only after
#64–#65's runtime and registry/authentication boundaries are merged. Browser
results supplement—not replace—the package-wide 100% statement and branch gate
for authentication, authorization, decoding, translation, control, and policy.
