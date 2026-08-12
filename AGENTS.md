# Klove engineering rules

## Safety invariant

Klove fails closed. A command is permitted only when every required piece of
identity, target, capability, state, parameter, and transition evidence is
positive, current, internally consistent, and unambiguous. Unknown or missing
evidence is a denial. Never infer printer capabilities from names, models, or
near matches.

The read-only foundation must contain no Moonraker actuator RPC, generic G-code
execution path, or mutating HTTP route. Introducing actuation requires an
accepted architecture decision, a narrow typed interface, negative tests, and
100% statement and branch coverage across authentication, authorization,
decoding, translation, and policy code.

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
