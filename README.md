# Klove

Klove is a fail-closed interface between Grove Control and Klipper printers
managed through Moonraker.

Klove discovers Moonraker capabilities, maintains a canonical printer state,
authenticates native API clients, and classifies Grove commands. Its first
opt-in control slice exposes only typed pause, resume, and cancel operations.

This is pre-release software. It cannot start a print, execute arbitrary G-code,
or provide any other motion, heating, fan, light, or macro control.

## Run the service

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
includes an opaque, boot-scoped `state_token`.

Job control is disabled unless both `[control].enabled` and the target
printer's `control_enabled` are true. An enabled request uses
`POST /v1/printers/{id}/commands/{pause|resume|cancel}`, a canonical UUIDv4
`Idempotency-Key` header, and an exact JSON body containing the current
`state_token`. Klove serializes requests per printer, polls immediately before
dispatch, sends the dedicated Moonraker method once, then polls for the exact
job transition. Any ambiguity after dispatch returns `outcome_unknown` and is
never retried automatically. Changing the idempotency key does not bypass that
uncertainty: the affected printer and state token remain fenced until the caller
obtains a newly observed token.

Moonraker resolves these methods through Klipper's `PAUSE`, `RESUME`, and
`CANCEL_PRINT` commands. Printer owners must review and safely maintain any
macros that replace those commands; Klove does not inspect or approve their
contents.

For containers, copy `compose.example.yml`, replace its image placeholder with
an accepted immutable digest, and mount configuration and secrets read-only.

## Development

Create `.venv`, install `requirements-dev.lock` and `requirements-build.lock`
with `--require-hashes`, and install Klove with `--no-build-isolation --no-deps`.
Then run `scripts/test-fast.ps1` on Windows or `scripts/test-fast.sh` elsewhere.
The gate includes formatting, linting, strict typing, and 100% statement and
branch coverage. Pytest is configured to import Klove from `src`, so the gate
exercises the working tree even when the environment also contains a
non-editable or older package installation.

See the [architecture and delivery plan](docs/architecture-plan.md),
[active implementation plan](docs/implementation-plan.md),
[threat model](docs/threat-model.md), and [testing policy](docs/testing.md).
