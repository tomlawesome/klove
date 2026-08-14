# Klove

Klove is a fail-closed interface between Grove Control and Klipper printers
managed through Moonraker.

Klove is intentionally headless for routine operation: Grove owns normal user
interaction, while Klove exchanges structured evidence between controllers and
exposes the smallest practical machine-facing surface. ADR 0006 accepts one
narrow exception: a Klove-owned setup and recovery page embedded in Grove's
custom **Klipper via Klove** Add Printer path. It is not a dashboard or control
surface.

Klove discovers Moonraker capabilities, maintains a canonical printer state,
authenticates native API clients, and classifies Grove commands. Its first
opt-in northbound control slice exposes only typed pause, resume, and cancel
operations.

This is pre-release software. Its native API cannot submit an artifact, upload
or start a print, execute arbitrary G-code, or provide any other motion,
heating, fan, light, or macro control. The separately accepted upload and
durable print-start domain services are not exposed through a northbound route.

The product onboarding path is accepted but not complete. Issue #62 implements
its private versioned registry, external credential store, exact lifecycle
journal, crash reconciliation, and database snapshot foundation. Issues #63–#65
still have to add the direct Moonraker probe, lifecycle orchestration, dynamic
runtime activation, owner-authenticated API, and issue #59's embedded page.
Once those and the runtime facade in issues #10/#14 ship, an authorized Grove
user will select **Klipper via Klove**, complete Moonraker setup inside the
embedded Klove page, review direct identity and safety-profile evidence, and
return only Klove's proxy host, stable serial, display name, and generated
compatibility access code to Grove's existing printer-create flow. Grove will
never receive the Moonraker credential. Until then, the file-based steps below
are a developer/bootstrap limitation, not the finished user experience.

Klove defines a strict v3 contract and non-actuating validator for
`.gcode.3mf` intake, one exact selected plate path, target approval,
qualification evidence, and structured denials. The `[artifacts]` configuration
bounds the central directory, entry and byte counts, compression ratio, selected
G-code, header, line length, and future metadata waits. Validation operates on
one immutable byte snapshot, rejects unsafe ZIP structure and known Bambu-only
G-code signatures, and streams only the selected member through integrity and
syntax checks.

Qualification then requires a separate trusted approval for those exact bytes
and an exact current configured safety profile. Printer UUID, registered slicer
profile, generation, canonical fingerprint, Klipper dialect, nozzle, build
volume, and plate must all match. Model names and near matches are ignored.
Manual review records are per-file audit evidence and are always denied by the
automatic path. Qualification itself grants no transport authority. The
separate ADR-0004 upload service rechecks current target and source evidence,
writes only `klove/<operation-id>.gcode` once with Moonraker checksum validation
and `print=false`, polls bounded metadata, downloads and hashes the remote file
between identical metadata reads, and emits non-actuating `VerifiedUpload`
evidence. Any ambiguity after the request begins is retained as
`outcome_unknown` without retry.

ADR 0005 accepts one further internal boundary: after both dispatch gates opt
in, the print-start service rechecks the exact current target and remote
metadata/SHA-256, requires a coherent idle preflight, commits a `dispatching`
reservation to an owner-only SQLite/WAL journal, and sends the exact
`printer.print.start(filename)` RPC no more than once. A response never proves
success. Only a new immutable Moonraker history identity for that exact path,
strictly later monotonic evidence, and a compatible live phase can confirm it.
Every unresolved outcome survives restart and fences that printer without a
blind retry. No generic G-code path exists. See ADRs 0003–0005 and the
safety-profile example in `config.example.toml`.

## Run the current bootstrap service

1. Copy `config.example.toml` to `config.toml`, replace every example printer
   UUID with a generated stable UUIDv4, and use an explicit Moonraker URL for
   each printer. Advance a safety profile's generation on every change,
   including a later reversion.
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
an accepted immutable digest, mount configuration and secrets read-only, and
retain the `klove-state` volume. Losing or rolling back that volume can remove
an unresolved print-start fence.

## Development

Create `.venv`, install `requirements-dev.lock` and `requirements-build.lock`
with `--require-hashes`, and install Klove with `--no-build-isolation --no-deps`.
Then run `scripts/test-fast.ps1` on Windows or `scripts/test-fast.sh` elsewhere.
The gate includes formatting, linting, strict typing, and 100% statement and
branch coverage. Pytest is configured to import Klove from `src`, so the gate
exercises the working tree even when the environment also contains a
non-editable or older package installation.

Changes to Moonraker protocol/control/start, reconciliation, or its container
fixture must also run `scripts/test-moonraker-sim.sh`. On Linux this builds a
confined, rootless, revision-pinned native Klipper/Moonraker stack with
Klipper's Linux-process MCU and exercises real authentication, monitoring,
typed pause/resume/cancel, lost-response fencing, and restarts. The private fixture
starts only a finite dwell job to establish control-test state; that fixture
preparation is not the production upload or durable start service. End-to-end
production upload/start evidence is issue #12; no generic G-code capability is
exposed. See the
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
[RatOS acceptance lane](docs/ratos-acceptance.md). The accepted onboarding
boundary is [ADR 0006](docs/decisions/0006-embedded-grove-onboarding.md), and
the registry's persistence and recovery rules are documented in the
[registry storage contract](docs/registry-storage.md).
