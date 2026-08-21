# Grove black-box observation fixtures

This directory is reserved for independently captured, sanitized Grove
protocol observations. It intentionally contains no observation fixtures yet.

Every future regular fixture must have a sibling
`<fixture-name>.manifest.json`. Run
`python scripts/validate_grove_observations.py` to validate the exact version-1
manifest schema, fixture digest, and provenance review assertions before
committing it. Do not place Grove source, screenshots, assets, certificates,
private keys, credentials, raw traces, or copied upstream fixtures here.
Fixtures may not exceed 8 MiB.

The older `tests/fixtures/grove/` JSON files remain Klove decoder fixtures.
They are not Grove observation evidence and are deliberately outside this
directory.
