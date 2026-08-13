# Release lanes

Klove uses three protected branches:

1. `develop` is the integration lane for ordinary pull requests.
2. `preview` is the production-like acceptance lane. A push builds one image,
   runs its immutable runtime contract, rejects high or critical
   vulnerabilities, emits an SPDX SBOM, and publishes provenance and SBOM
   attestations for the resulting digest.
3. `main` is the stable source lane. Its PR gate verifies that the candidate
   image label names the exact `preview` commit being proposed.

After a `preview` candidate is manually accepted and merged to `main`, the
promotion workflow must be dispatched with its `sha256:` digest. The workflow
verifies the attestation, source-branch label, Git ancestry, and exact source
tree before moving `latest` to that digest. Promotion does not rebuild.

Klove is currently pre-release. ADR 0001 accepts the first actuation slice, but
do not run stable promotion until the exact candidate has production-like
acceptance evidence for that control contract.
