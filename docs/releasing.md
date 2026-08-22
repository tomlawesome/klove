# Release lanes

Klove uses three protected branches:

1. `dev` is the integration lane for ordinary pull requests.
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

Klove is currently pre-release. Exact pinned Moonraker/Klipper integration and
the supplemental exact-release RatOS emulation lane are the software acceptance
boundary during implementation. They exercise the production Klove protocol,
state, control, ambiguity, and restart contracts without requiring visible
physical motion after every slice.

Do not run stable promotion during the intermediate milestones. After the full
milestone suite is complete, run the attended supported-ARM RatOS procedure
against the designated physical printer to validate board integration,
configured macro semantics, and real motion. Promote only that final accepted
preview digest; never rebuild it.
