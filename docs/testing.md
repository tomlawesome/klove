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
- control tests for exact token/job/state matching, per-printer serialization,
  history job-id/start-time bracketing, telemetry-stable control tokens, final
  exact-token rechecks, single dispatch, cross-key uncertainty fencing,
  idempotency conflicts and exhaustion, postcondition binding, and every
  post-dispatch ambiguity;
- accepted and rejected artifact-contract fixtures plus negative tests for
  canonical identities, exact target/profile binding, unknown, missing, stale,
  contradictory and ambiguous evidence, internally inconsistent metrics, and
  every configured intake limit;
- hostile ZIP/ZIP64 tests for traversal and aliases, duplicates, links,
  encryption, unsupported features, malformed local/central records, count,
  metadata, expanded-byte and exact ratio limits, missing exact selection, and
  selected-member CRC; bounded G-code tests cover controls, line endings,
  header/line limits, slicer structure, motion, comment handling, and known
  Bambu-only signatures, with property tests over arbitrary ZIP-like bytes;
- repository policy tests that reject the enumerated prohibited RPC literals,
  confine the three accepted job-control RPC literals to one adapter, constrain
  the native mutating route count, and reject unpinned CI actions;
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
preparation does not authorize an upload, print-start, or generic G-code path in
`src/klove`. The native amd64 stack also does not claim RatOS coverage. RatOS
v2.1.0 acceptance separately records the exact ARM release asset checksum,
supported board, running software identities, controlled configuration and
macro hashes, and attended physical results under `docs/ratos-acceptance.md`.

An opt-in supplemental full-system lane is available as
`scripts/test-ratos-emulation.sh`. It verifies the exact RatOS v2.1.0 Raspberry
Pi archive, preserves its expanded raw disk read-only, direct-loads the matching
release kernel and DTB into QEMU's Pi 3B model, and writes only to a private COW
overlay created fresh for each evidence run. Its fixed-snapshot tool image and
unprivileged QEMU process receive the release inputs as separate read-only
mounts; only the run COW is writable. The rootless outer container is
networkless, capability-free, resource-bounded, and exposes guest services only
to its own loopback. The lane records bounded non-secret
RatOS/Moonraker/service identities and performs exact teardown, overlay checking,
and derived-COW digesting without retaining raw guest serial output. Every
archived run carries its own prepared-input, tool-image, daemon, Git-revision,
and deterministic lane-source provenance, plus an atomic probe-success marker.
It requires x86-64 Linux, GNU coreutils, rootless Docker, and at least 16 GB free
for a fresh preparation. It is not in CI because the input is about 2.1 GB and
ARM-on-x86 TCG boot is slow. It does not emulate a printer MCU, prove macro
semantics, or replace attended hardware acceptance.

Pytest prepends `src` to its import path, and a repository-policy assertion
verifies that the suite imported Klove from the working tree. This prevents a
non-editable or stale environment installation from producing misleading
coverage for code other than the source under review.
