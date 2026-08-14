# Klove

Klove is a fail-closed interface between Grove Control and Klipper printers
managed through Moonraker.

Klove is intentionally headless: Grove owns normal user interaction, while
Klove exchanges structured evidence between controllers and exposes the
smallest practical machine-facing surface. Human input—and therefore any local
Web UI—is a last resort for a separately justified, irreducible interaction.

Klove discovers Moonraker capabilities, maintains a canonical printer state,
authenticates native API clients, and classifies Grove commands. Its first
opt-in control slice exposes only typed pause, resume, and cancel operations.

This is pre-release software. It cannot start a print, execute arbitrary G-code,
or provide any other motion, heating, fan, light, or macro control.

Klove defines a strict v2 contract and non-actuating validator for
`.gcode.3mf` intake, one exact selected plate path, printer/profile binding,
validation evidence, and structured denials. The `[artifacts]` configuration
bounds the central directory, entry and byte counts, compression ratio, selected
G-code, header, line length, and future metadata waits. Validation operates on
one immutable byte snapshot, rejects unsafe ZIP structure and known Bambu-only
G-code signatures, and streams only the selected member through integrity and
syntax checks. It does not establish target compatibility, upload a file, or
authorize print start.

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

The token binds a dedicated control-evidence revision, capability fingerprint,
phase, filename, and Moonraker history job identity. Ordinary telemetry and
forward progress advance the public snapshot revision without changing that
token. Reconnect, rediscovery, job replacement, phase change, invalid or
regressing job evidence, and history identity change rotate it.

Job control is disabled unless both `[control].enabled` and the target
printer's `control_enabled` are true. An enabled request uses
`POST /v1/printers/{id}/commands/{pause|resume|cancel}`, a canonical UUIDv4
`Idempotency-Key` header, and an exact JSON body containing the current
`state_token`. Klove serializes requests per printer, brackets an immediate
Moonraker object poll with the exact current history job identity, rechecks the
same token, sends the dedicated Moonraker method once, then polls for the exact
job transition. Each poll may take a bounded number of read-only samples until
its two history reads agree; it never repeats the action. Any unresolved
ambiguity after dispatch returns `outcome_unknown`. Changing the idempotency key
or observing ordinary forward progress does not bypass that uncertainty: the
affected printer and state token remain fenced until control evidence produces
a new token.

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

Changes to Moonraker protocol/control, reconciliation, or its container fixture
must also run `scripts/test-moonraker-sim.sh`. On Linux this builds a confined,
rootless, revision-pinned native Klipper/Moonraker stack with Klipper's
Linux-process MCU and exercises real authentication, monitoring, typed
pause/resume/cancel, lost-response fencing, and restarts. The private fixture
starts only a finite dwell job to establish test state; Klove itself still has
no upload, print-start, or generic G-code capability. See the
[integration fixture](tests/integration/moonraker-sim/README.md).

That automated amd64 stack is not RatOS. RatOS host and physical-printer
acceptance uses an exact verified RatOS v2.1.0 ARM disk image on supported
hardware under the separate [RatOS acceptance procedure](docs/ratos-acceptance.md).
An opt-in, rootless full-system lane also boots that exact Raspberry Pi release
under QEMU and exercises production Klove against its controlled Linux-process
host-MCU contract; it remains supplemental and is not physical acceptance.

See the [architecture and delivery plan](docs/architecture-plan.md),
[active implementation plan](docs/implementation-plan.md),
[threat model](docs/threat-model.md), [testing policy](docs/testing.md), and
[RatOS acceptance lane](docs/ratos-acceptance.md).
