# Testing policy

Klove treats authentication, authorization, command decoding, translation, and
safety policy as critical boundaries. They must maintain 100% statement and
branch coverage. During the read-only foundation, the entire Python package is
held to that threshold.

Coverage is evidence that each implemented branch was exercised, not proof
that the protocol model is complete. The suite therefore also includes:

- negative and property tests for hostile or ambiguous Grove payloads;
- reducer tests for stale, malformed, contradictory, and incomplete state;
- deterministic WebSocket contract tests against a fake Moonraker peer;
- API tests for missing, malformed, and insufficient credentials;
- repository policy tests that reject mutating transports and unpinned CI
  actions in the read-only slice;
- a preview-container contract assertion, vulnerability scan, SBOM, and
  provenance attestation.

Run the fast gate with `scripts/test-fast.ps1` or `scripts/test-fast.sh` after
installing `requirements-dev.lock` using pip's `--require-hashes` option. GitHub
Actions is the authoritative clean environment. The expensive exact-container
job runs only on protected `preview` and `hotfix/**` pushes, never on arbitrary
pull-request code.
