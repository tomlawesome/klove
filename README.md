# Klove

Klove is a fail-closed interface between Grove Control and Klipper printers
managed through Moonraker.

The first delivery slice is intentionally read-only. It discovers Moonraker
capabilities, maintains a canonical printer state, authenticates monitoring API
clients, and classifies Grove commands without exposing any printer-changing
transport.

This is pre-release software. It cannot yet start, pause, resume, or cancel a
print. That absence is a safety boundary: no Moonraker actuator RPC exists in
the package.

## Run the read-only service

1. Copy `config.example.toml` to `config.toml` and use an explicit Moonraker URL
   for each printer.
2. Create the configured secret files. Each must contain exactly one token of
   at least 32 characters. Do not put credentials in TOML or Compose files.
3. Install the hash-pinned dependencies and package, then start Klove:

   ```console
   python -m pip install --require-hashes -r requirements.lock
   python -m pip install --require-hashes -r requirements-build.lock
   python -m pip install --no-build-isolation --no-deps .
   klove --config config.toml
   ```

The native API binds to loopback by default. `/health/live` and
`/health/ready` contain no printer data. `/v1/printers` and
`/v1/printers/{id}` require the configured bearer token. Each printer document
includes an opaque, boot-scoped `state_token`; future commands will have to bind
to exact observed state rather than a reusable integer revision.

For containers, copy `compose.example.yml`, replace its image placeholder with
an accepted immutable digest, and mount configuration and secrets read-only.

## Development

Create `.venv`, install `requirements-dev.lock` with `--require-hashes`, and
install Klove editable with `--no-build-isolation --no-deps`. Then run
`scripts/test-fast.ps1` on Windows or `scripts/test-fast.sh` elsewhere. The gate
includes formatting, linting, strict typing, and 100% statement and branch
coverage.

See the [architecture and delivery plan](docs/architecture-plan.md),
[active implementation plan](docs/implementation-plan.md),
[threat model](docs/threat-model.md), and [testing policy](docs/testing.md).
