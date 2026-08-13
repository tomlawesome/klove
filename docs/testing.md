# Testing policy

Klove treats authentication, authorization, command decoding, translation, and
safety policy as critical boundaries. They must maintain 100% statement and
branch coverage. The current package-wide gate is also held to that threshold.

Coverage is evidence that each implemented branch was exercised, not proof
that the protocol model is complete. The suite therefore also includes:

- negative and property tests for hostile or ambiguous Grove payloads;
- reducer tests for stale, malformed, contradictory, and incomplete state;
- deterministic WebSocket contract tests against a fake Moonraker peer;
- API tests for missing, malformed, and insufficient credentials;
- control tests for exact token/job/state matching, per-printer serialization,
  single dispatch, cross-key uncertainty fencing, idempotency conflicts and
  exhaustion, postcondition binding, and every post-dispatch ambiguity;
- repository policy tests that reject the enumerated prohibited RPC literals,
  confine the three accepted job-control RPC literals to one adapter, constrain
  the native mutating route count, and reject unpinned CI actions;
- a preview-container contract assertion, vulnerability scan, SBOM, and
  provenance attestation.

Run the fast gate with `scripts/test-fast.ps1` or `scripts/test-fast.sh` after
installing `requirements-dev.lock` using pip's `--require-hashes` option. GitHub
Actions is the authoritative clean environment. The expensive exact-container
job runs only on protected `preview` and `hotfix/**` pushes, never on arbitrary
pull-request code.

Pytest prepends `src` to its import path, and a repository-policy assertion
verifies that the suite imported Klove from the working tree. This prevents a
non-editable or stale environment installation from producing misleading
coverage for code other than the source under review.
