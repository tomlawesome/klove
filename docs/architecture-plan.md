# Klove architecture and delivery plan

Status: accepted  
Research date: 2026-08-12  
Grove Control source reviewed at commit `cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4`.

Repository delivery decisions:

- Klove is intentionally single-maintainer. Protected branches require pull
  requests and resolved conversations but no approving review.
- Production self-review and administrator bypass remain enabled.
- GitHub Actions defaults to a read-only repository token. Publication jobs
  elevate only the scopes they require.
- Authentication, authorization, translation, and command-safety code starts
  and remains at 100% statement and branch coverage.
- The safety policy is proof-based: any uncertainty, however small, is a denial.

## Decision summary

Build Klove as one centrally managed service beside Grove Control. Run it as a
container in the same Docker deployment by default. Klove connects outward to
each printer's Moonraker HTTP and WebSocket APIs; it does not scrape Mainsail,
modify Klipper, or require an installation on every printer host.

Klove is headless and automation-first for routine operation. Grove owns the
normal operator experience. Prefer direct, structured controller-to-controller
evidence over human input; reconcile automatically when a bounded proof exists
and otherwise fail closed. ADR 0006 accepts one narrow exception to ADR 0002:
a Klove-owned setup/recovery page embedded by Grove's **Klipper via Klove** Add
Printer path. It is limited to irreducible onboarding and recovery choices and
must not become a dashboard or printer-control surface.

Use three deliberately narrow northbound surfaces:

1. A Bambu-shaped MQTT/TLS and implicit-FTPS facade supplies only conservative
   runtime behavior already accepted by Klove's own safety contracts.
2. A setup/recovery HTTP surface owns authenticated onboarding, direct
   Moonraker probes, the runtime registry, secret handling, and a strict
   completion handoff to Grove.
3. Existing versioned Klove HTTP endpoints remain bounded machine interfaces;
   they are not a commitment to a broad native Grove provider.

Grove receives only an explicit conservative `KLOVE` printer type, the embedded
Add Printer path, completion-message decoding, and feature gates. It does not
receive Moonraker credentials or implement Klipper semantics. The earlier broad
native-provider programme is retired as not planned.

Do not translate `printer.cfg` into arbitrary commands and do not attempt to
convert Bambu-sliced G-code into Klipper G-code. Read Klipper's exposed objects
and selected configuration values to construct a capability and safety profile.
Accept only typed operations, validate them against current state and that
profile, and reject unknown or ambiguous operations.

## Why this deployment

Moonraker is already the supported network API in front of Klipper. It exposes
printer control, object subscriptions, file upload, job history, cameras, and
authentication. Mainsail is another Moonraker client, so putting Klove behind or
inside Mainsail adds no useful control surface. Klipper's own API is a local Unix
socket; a per-host Klove agent would only recreate functionality Moonraker
already provides.

| Option | Advantages | Costs and risks | Decision |
| --- | --- | --- | --- |
| Central Klove container | One install and upgrade; one audit/logging point; manages many printers; uses official Moonraker APIs; natural future-adapter boundary | Requires network reachability and credentials to Moonraker; central failure affects Grove integration | Default |
| systemd service on every Klipper host | Can use loopback or the Klipper Unix socket; each host has a distinct IP for Bambu-compatible ports | Fleet-wide installation, upgrades, secrets, certificates, and support burden; competes for small SBC resources | Optional later for isolated/outbound-only networks |
| Moonraker component/plugin | Close integration and local access | Couples Klove to Moonraker internals and its release cadence; must be installed everywhere | Reject for v1 |
| Implement Klipper directly in Grove | Fewest runtime services | Expands Grove's Bambu-specific core, couples release cycles, and creates a new Klipper maintenance surface | Reject; permit only the minimal `KLOVE` type, embedded Add Printer path, handoff, and conservative feature gates in ADR 0006 |

An optional lightweight agent can be designed later for sites where Moonraker
cannot accept an authenticated connection from the Grove host. It should make an
outbound mTLS connection to central Klove and preserve the same canonical API;
it is not a separate implementation.

## Findings from Grove Control

Grove is currently Bambu-specific below its fleet and UI layers:

- `PrinterManager.connect_printer()` constructs `BambuMQTTClient` directly;
  there is no backend/provider selection.
- Each client subscribes to `device/{serial}/report` and publishes to
  `device/{serial}/request` over TLS MQTT on port 8883.
- Queue dispatch first uploads a `.3mf` using implicit FTPS on port 990, then
  publishes a `print.project_file` command referencing a plate G-code inside the
  uploaded archive.
- Pause, resume, cancel, temperature, fan, light, homing, jogging, and extrusion
  eventually become Bambu MQTT messages or `print.gcode_line` payloads.
- The printer database and UI identify Bambu model families rather than a
  provider plus capabilities.
- Grove's existing virtual-printer implementation already contains an MQTT
  server and FTPS server that speak the Bambu-side protocol. This is a valuable
  executable protocol reference, but it is not a clean printer-provider API.

Relevant source:

- [hard-coded Bambu connection](https://github.com/EdwardChamberlain/grove-control/blob/cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4/backend/app/services/printer_manager.py#L470-L535)
- [MQTT report/request topics](https://github.com/EdwardChamberlain/grove-control/blob/cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4/backend/app/services/bambu_mqtt.py#L621-L626)
- [FTPS upload followed by MQTT dispatch](https://github.com/EdwardChamberlain/grove-control/blob/cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4/backend/app/services/print_scheduler.py#L2260-L2438)
- [Bambu command methods](https://github.com/EdwardChamberlain/grove-control/blob/cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4/backend/app/services/bambu_mqtt.py#L4558-L4905)
- [virtual-printer MQTT status facade](https://github.com/EdwardChamberlain/grove-control/blob/cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4/backend/app/services/virtual_printer/mqtt_server.py#L853-L1003)

This makes pure Bambu impersonation the quickest experiment, but a poor domain
model. It would force Klipper printers to pretend to have a Bambu model, storage,
AMS, HMS errors, camera protocol, and feature set. It also makes silent
unsupported-command behaviour too easy. The reviewed create schema limits a
serial to 50 uppercased characters and an access code to 20 characters, while
model values drive scheduling and hardware features. ADR 0006 therefore uses a
stable `KLOVE-<UUID>` proxy serial, a 20-character generated access code, and an
explicit `KLOVE` model with conservative gates.

## Target architecture

```text
Grove Control
  |  setup: exact-origin iframe + strict completion message
  |  runtime: MQTT/TLS + implicit FTPS
  v
Klove northbound boundaries
  -> owner-authenticated setup/recovery and runtime registry
  -> compatibility authentication and exact per-printer routing
  -> command decoder / conservative state encoder
  v
Canonical printer domain
  -> typed state and capabilities
  -> command policy and state machine
  -> idempotency, operation journal, reconciliation
  -> artifact validation and dispatch coordinator
  v
Moonraker adapter (one session per printer)
  -> WebSocket JSON-RPC subscriptions and commands
  -> HTTP file upload, metadata, history, typed start, and webcams
  v
Moonraker -> Klipper -> MCU

Mainsail remains a parallel Moonraker client.
```

Suggested package boundaries:

```text
src/klove/
  domain/          state, capabilities, typed commands, policy
  orchestration/   registry, dispatch, reconciliation, idempotency
  adapters/
    moonraker/     HTTP/WS client, discovery, state and file mapping
  northbound/
    bambu_compat/  MQTT, FTPS, Bambu payload mapping
    onboarding/    bounded setup/recovery HTTP and browser contract
    api/           bounded versioned machine API
  persistence/     configuration, operation journal, migrations
```

Python with `asyncio` is the path of least resistance: Grove and Moonraker are
Python systems, the workload is I/O-heavy, and one event loop can supervise many
Moonraker WebSockets. Use a shared HTTP/WebSocket client session per endpoint,
Pydantic models at protocol boundaries, SQLite/WAL for small durable state, and
an OCI image for amd64 and arm64. Keep MQTT and FTPS implementations behind
interfaces so the exact server libraries can be changed after protocol contract
tests.

## Moonraker integration

For every configured printer Klove should:

1. Authenticate with an API key or JWT supplied through a mounted secret file.
   Do not require the operator to add the whole Grove subnet to Moonraker's
   `trusted_clients`.
2. Open `/websocket`, identify as a Klove `agent`, and query `server.info` and
   `printer.info`.
3. Wait for Klippy to be `ready`, call `printer.objects.list`, then subscribe to
   only the objects that exist. Merge the initial subscription snapshot and all
   later `notify_status_update` diffs into local state.
4. Watch `notify_klippy_ready`, `notify_klippy_shutdown`, and
   `notify_klippy_disconnected`; re-probe capabilities after restart.
5. For the accepted first control slice, use only JSON-RPC
   `printer.print.pause`, `.resume`, and `.cancel`.
6. ADR 0004 accepts HTTP `/server/files/upload` only for bounded,
   operation-unique, checksum-verified placement with `print=false`, remote
   digest verification, and no northbound route.
7. ADR 0005 accepts one exact `printer.print.start` JSON-RPC only after current
   target/file checks, coherent idle preflight and durable SQLite/WAL
   reservation. Startup/reconnect reconciliation is read-only and unresolved
   rows fence their printer without retry.
8. Use Moonraker metadata and history to estimate remaining time and reconcile
   jobs after either side restarts when the relevant slice is authorized.

Minimum required Klipper objects for farm dispatch are `virtual_sdcard`,
`print_stats`, and `pause_resume`. A printer missing one should remain visible
for monitoring but be marked dispatch-ineligible with a concrete diagnostic.

Subscribe as available to:

- `webhooks`: Klippy health and failure message
- `print_stats`: filename, state, durations, message, filament use, layers
- `virtual_sdcard`: active flag and byte progress
- `display_status`: slicer progress/message when present
- `extruder*`, `heater_bed`, and configured heater objects: temperature, target,
  power, and `can_extrude`
- `heaters`: available heaters and sensors
- `fan` and mapped fan objects
- `toolhead`: homed axes, limits, position, active extruder, velocity limits
- `gcode_move`: speed and extrusion factors
- `pause_resume`, `exclude_object`, and filament sensors when present

Moonraker's webcam registry can populate suggested Grove external-camera URLs;
Klove should not implement a fake Bambu RTSP camera in the MVP.

## Capability discovery: read, infer, confirm

`printer.cfg` is data, not an instruction source. Klove may read the Klipper
`configfile.settings` status object but must extract only known fields and never
execute macro bodies or copy arbitrary values into commands.

Automatically infer:

- Klipper/Moonraker identity and versions
- kinematics and cartesian bounds
- hotend count, active hotend, nozzle diameter when reliably available
- configured heater limits and the presence of a heated bed/chamber
- fans, filament sensors, object exclusion, virtual SD, and pause support
- camera entries and build-volume information

Require operator confirmation for ambiguous semantics:

- which named fan is part, auxiliary, chamber, or filter
- which macro or output controls a chamber light
- tool changer/MMU load and unload operations
- printer-specific preparation, clear-plate, or recovery macros
- accepted slicer profile identifiers and nozzle sizes

Persist a safety-profile fingerprint over relevant settings. On Klipper config
change, reductions in limits may apply automatically; increased temperature,
motion, or build limits require confirmation. Until then, fail closed only for
the affected operation rather than taking the whole printer offline.

## Canonical state mapping

Klove owns a provider-neutral state model. The Bambu facade derives a small
synthetic report from it; Moonraker field names must not leak into Grove-facing
business logic.

| Canonical state | Moonraker evidence | Current Grove facade |
| --- | --- | --- |
| offline | WebSocket/HTTP unavailable or Klippy disconnected | refuse/close that printer's authenticated MQTT session |
| not_ready | Klippy startup, shutdown, or error | connected but non-dispatchable `UNKNOWN`/`FAILED`; never `IDLE` |
| idle | Klippy ready and `print_stats.state=standby` | `IDLE` |
| preparing | artifact accepted/start requested, before printing is observed | `PREPARE` |
| printing | `print_stats.state=printing` | `RUNNING` |
| paused | `print_stats.state=paused` | `PAUSE` |
| completed | `print_stats.state=complete` | `FINISH` |
| cancelled/failed | `cancelled` or `error`, preserving the reason | `FAILED` with a Klove diagnostic, not a fabricated Bambu HMS code |

Map progress from `virtual_sdcard.progress` or `display_status.progress`, layers
from `print_stats.info`, temperatures from heater objects, and remaining time
from Moonraker file metadata plus elapsed/progress data. Omit unsupported fields
instead of inventing sensors or AMS state.

## Typed command translation and safety policy

The accepted canonical operations are currently limited to:

- pause, resume, cancel

The roadmap proposes the following later operations, but each remains
prohibited until its own ADR accepts the complete typed contract and safety
evidence:

- dispatch artifact
- set hotend or bed target
- set speed multiplier
- set a mapped fan or light
- home
- bounded jog
- bounded extrude/retract
- exclude objects
- invoke an explicitly configured macro with a declared parameter schema

The following are design constraints for any future accepted operation, not
authorization to implement it:

- Reject unknown MQTT commands and arbitrary `gcode_line` by default.
- Pause/resume/cancel use Moonraker's typed print endpoints, not G-code strings.
- Any future heater-target design must use the lower of the operator policy and
  discovered Klipper limit, with a configured safety margin.
- A future jogging design would require idle state, homed axes, current
  `toolhead.axis_minimum/axis_maximum`, and below configured distance/speed
  limits. Whether Klove may generate any bounded sequence requires its own ADR.
- A future extrusion design would require idle state, a selected hotend
  reporting `can_extrude`, and configured length/rate limits.
- Future fan and light commands require explicit object or reviewed macro
  mappings. Never guess by substring alone.
- Future printer, heater, fan, and macro targets must be selected from
  discovered allowlists; they must never be interpolated from a Grove payload.
- Any future live control while printing must be separately accepted as safe
  for that exact state.

ADR 0001 accepts stock Moonraker pause, resume, and cancel for the first control
slice. The printer owner is responsible for the correctness and safety of any
Klipper macros replacing `PAUSE`, `RESUME`, or `CANCEL_PRINT`. Klove mitigates,
but cannot remove, Moonraker's non-atomic query/control interval: it requires an
exact state token and job match, serializes by printer, polls immediately before
one dispatch, and binds confirmation to the same Moonraker history job id and
start time. The exact token is checked again after the direct poll; unrelated
telemetry cannot rotate it, while any control-state or job-identity change does.
Any
ambiguity after dispatch is `outcome_unknown` and never triggers a blind retry.
No generic G-code or print-start transport is part of this decision.

The automated integration lane runs the production Klove image against pinned
real Klipper and Moonraker processes with Klipper's Linux-process MCU. It tests
authentication, status/control semantics, ambiguity, and restarts without
hardware, host ports, devices, or privilege. It is not RatOS. The exact RatOS
v2.1.0 ARM disk release, host services, configured macros, and physical effects
remain a separate attended acceptance lane; full-system emulation may add
evidence only if it faithfully boots the immutable release and does not weaken
confinement.

Grove sends MQTT with QoS 1, so Klove must assume duplicate delivery. Deduplicate
commands by printer, command kind, sequence/task ID, and payload hash. A repeated
`project_file` must return/re-emit the existing operation result; it must never
start a second print.

## Artifact and dispatch safety

The largest semantic hazard is the print file, not the control API. A G-code
file sliced for a Bambu machine is not made safe for a Klipper printer by
renaming it or extracting it from a 3MF. Klove must not rewrite a foreign start
G-code dialect or silently ignore commands.

Artifact policy is staged. Its validation, qualification, upload and durable
start components are implemented behind separate evidence boundaries, but no
northbound intake-to-completion workflow is yet authorized:

1. Accept Grove's `.gcode.3mf` container only when its selected plate contains
   G-code sliced for the target Klipper profile. Never support unsliced geometry
   in the first dispatch path.
2. Require a target identity in a manifest/comment or an exact registered
   slicer-profile identifier. Bind it to a Klove printer UUID and safety-profile
   fingerprint. Legacy files without proof are denied; the automatic path does
   not replace missing controller evidence with a human override.
3. Reject known Bambu-only G-code signatures and mismatched printer/nozzle/build
   metadata.
4. Treat the 3MF as a hostile ZIP: cap upload/compressed/uncompressed sizes and
   entry count; reject traversal, links, encrypted entries, duplicate paths, and
   compression bombs; stream only the chosen plate G-code.
5. Under ADR 0004, upload once to a unique `klove/<operation-id>.gcode` path
   through Moonraker with the selected digest and `print=false`; wait for
   metadata processing, bracket a bounded remote-file digest with identical
   metadata reads, and retain every post-request ambiguity without retry.
6. Under ADR 0005, revalidate that exact target, metadata and remote digest,
   require a coherent idle preflight, durably reserve the complete operation,
   and issue at most one typed start request. A response never authorizes a
   retry or proves success.
7. Observe the expected Moonraker filename/state transition before reporting a
   successful start to Grove. If the response is lost, reconcile current state
   and history instead of retrying blindly.

The implemented artifact prerequisite remains deliberately non-actuating. Its strict v3 contract
keeps hostile byte inspection separate from independently trusted target
approval. The validator accepts one immutable byte snapshot, bounds and
validates hostile ZIP/ZIP64 metadata before parsing, and streams only the exact
selected G-code through integrity and conservative syntax/vendor checks.
Qualification then requires the same exact bytes in one controller/slicer
approval and one current configured safety profile. Canonical printer UUID,
registered slicer profile, generation, fingerprint, Klipper dialect, nozzle,
build volume, and plate must all agree. Names and near matches are not proof;
manual overrides remain audit-only and are denied by automation. Qualification
itself writes nothing. ADR 0004 may consume only that exact qualification,
recheck the current profile and immutable source, upload the selected G-code
once with Moonraker checksum verification and `print=false`, and emit verified
remote-file evidence only after bounded metadata and byte-digest reconciliation.
ADR 0005 may consume only that `VerifiedUpload`, repeat current target/file/live
checks, commit a durable pre-dispatch reservation, send one typed start, and
confirm only from later exact history and monotonic state evidence. These
components expose no northbound dispatch workflow; issue #12 owns that
integration.

Longer term, Grove's slicer sidecar can produce target-specific G-code using a
registered Klipper profile. That is re-slicing, not protocol translation, and
must remain a separate optional service.

## Current-Grove compatibility facade

The runtime facade remains deliberately small:

- TLS MQTT on 8883 with stable `KLOVE-<UUID>` per-printer serial topics and
  unique 20-character high-entropy access codes;
- implicit FTPS on 990, routing a login to the exact printer by its unique
  access code;
- specific-printer queueing only, with Klove's exact target approval rather
  than Grove model matching as dispatch authority;
- external camera URLs configured in Grove only after their separate boundary
  is supported; and
- no AMS/MMU, calibration, firmware, maintenance, drying, Bambu HMS, camera
  protocol, or unsupported hardware-control emulation.

Klove must publish enough conservative state and operation lifecycle detail for
Grove to remain the sole normal user interface, including stale/unavailable
reasons, structured denials, and `outcome_unknown`. It must not move a workflow
into human input merely because a compatibility mapping is inconvenient.

A single Klove endpoint can serve many printers: MQTT routing includes the
stable proxy serial, and FTPS routing uses the unique generated access code. The
access code is secret because FTPS itself does not carry the printer serial.

Run Grove and Klove on a private Docker network for the simplest runtime setup.
Grove's host-network mode and Grove's own virtual-printer feature can contend
for 8883, 990, and passive FTP ports; document bridge mode for the MVP and add
configurable compatibility ports before claiming host-network support.

The fastest lawful reuse path is to make Klove AGPL-3.0-compatible and adapt
Grove's tested virtual-printer MQTT/FTPS components with attribution. If a
different Klove licence is desired, obtain permission or implement the facade
without copying Grove code before development begins.

## Embedded onboarding and minimal Grove boundary

ADR 0006 replaces manual product registration and the former broad native
provider proposal. The supported workflow is:

1. An authorized Grove user selects **Klipper via Klove**. Grove opens Klove's
   setup route in a sandboxed frame at one configured exact origin.
2. Klove independently authenticates an owner and creates a short-lived,
   single-flow setup session. Grove authentication and private-network location
   are not sufficient authority.
3. Klove discovers candidate Moonraker endpoints or accepts one bounded manual
   host entry. The user supplies the Moonraker credential directly to Klove.
4. Klove performs direct identity and capability probes, presents only the
   irreducible name, exact safety-profile confirmation and opt-in choices, and
   persists the canonical UUID, endpoint, evidence, profile binding and opaque
   secret references in its one runtime registry.
5. Klove generates the stable proxy serial and compatibility access code. An
   exact-origin, nonce-bound, versioned completion message returns only the
   display name, serial, Klove host/IP and access code.
6. Grove sets the model to `KLOVE` and uses its existing authorized
   printer-create path. Cancellation or failure creates nothing. The access code
   is cleared from transient browser state after submission.

The same Klove route exposes credential rotation, disable/removal, and bounded
recovery after independent owner authentication. It contains no status
dashboard or controls. Normal onboarding does not edit per-printer TOML; file
configuration remains deployment/bootstrap input.

`KLOVE` is an explicit conservative type, not a fake Bambu model. Grove must
suppress model-derived file matching and scheduling, firmware, maintenance,
AMS, HMS, drying, calibration and unsupported hardware controls. Initial queue
routing is exact-printer only. Klove independently rejects anything outside its
accepted runtime contracts.

The frame and parent use exact origins and window references, an explicit
ready/nonce handshake, strict versioned schemas, and no wildcard
`postMessage`. Moonraker and Klove-owner credentials never cross the browser
handoff or enter Grove. The Klove route is protected by owner authentication,
short-lived server-side sessions, CSRF and exact-Origin checks, route-specific
CSP `frame-ancestors`, a minimal iframe sandbox, no-store/no-referrer responses,
and self-hosted assets. The complete browser and completion policy is frozen in
ADR 0006.

Compatibility is claimed only for an exact tested Grove revision. The currently
reviewed baseline is `cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4`. Klove has
read-only access upstream, so the small Grove contribution is built in a fork
and proposed normally. Upstream rejection leaves the compatibility facade and
standalone recovery available for development, but does not justify a permanent
private fork or revive the native-provider programme.

## Persistence, reconciliation, and operations

Persist only what Klove owns: one canonical runtime printer registry, opaque
secret references, confirmed capability mappings, safety-profile history,
artifact/operation identifiers, and bounded idempotency journals. Secret values
remain in owner-only storage outside the database. Grove remains the queue and
production system of record; Moonraker/Klipper remains the execution state of
record. Per-printer TOML is a temporary bootstrap path, not a parallel product
registry.

On startup or reconnect:

1. query Klippy and current `print_stats`/`virtual_sdcard` state;
2. load only existing exact durable operation rows; never infer authority from
   an operation-looking filename;
3. reconcile each unresolved start through coherent live/history reads only;
4. retain every unproven row as a per-printer fence and never retry its action;
5. publish the reconciled snapshot before accepting new dispatches.

Use bounded exponential backoff with jitter, periodic full-state resync, atomic
artifact writes, graceful shutdown that never cancels an active printer job,
redacted structured logs, health/readiness endpoints, and per-printer circuit
breakers. A Klove outage must not stop an already running Klipper print.

Container hardening should include a non-root user, read-only root filesystem,
no Docker socket, narrowly mounted data and secret volumes, dropped Linux
capabilities except `NET_BIND_SERVICE` only if ports 990/8883 require it, private
network exposure by default, and an egress allowlist to configured Moonraker
hosts. Do not log access codes, Moonraker keys, JWTs, URLs containing secrets, or
uploaded G-code bodies.

## Delivery sequence

### Phase 0: contract and fixtures

- Decide the Klove licence and whether Grove virtual-printer code may be reused.
- Capture sanitized Bambu request/report fixtures from Grove and Moonraker
  snapshots/notifications from representative Klipper configurations.
- Freeze canonical state, capability, command, operation, and error schemas.
- Write the threat model and artifact acceptance policy before enabling motion or
  heating.

Exit: mappings and rejected behaviours are executable tests, not only prose.

### Phase 1: read-only Moonraker core

- Bootstrap configuration/secrets, Moonraker authentication, discovery
  primitives, WebSocket subscriptions, state reducer, capability probe,
  reconnect, and health API. Product onboarding is completed by ADR 0006's
  later runtime-registry and setup/recovery slices.
- Support one and then multiple fake/real Moonraker endpoints.

Exit: stable monitoring through Moonraker restarts and network partitions; no
printer-changing command exists.

### Phase 2: typed Moonraker job control

- Deliver the ADR-0001 pause, resume, and cancel contract through the typed
  native route.
- Keep print start, generic G-code, temperature, speed, fan, light, motion, and
  extrusion absent.

Exit: one exact current job-control request is dispatched at most once, and
every post-dispatch ambiguity is retained as `outcome_unknown` without retry.

### Phase 3: runtime registry and safe file dispatch

- Implement ADR 0006's one canonical runtime printer registry and external
  owner-only secret store under issue #58. The registry supplies an exact
  onboarded UUID, endpoint and safety-profile binding without adding a UI or
  actuator.
- ADRs 0003–0005 accept exact qualification, non-actuating Moonraker upload and
  durable at-most-once typed print start as separate internal components.
- Implement hostile-3MF validation, target manifest/profile checks, Moonraker
  upload/metadata/start, dedupe, acknowledgement, and restart reconciliation in
  separately gated slices.
- Complete their authenticated intake-through-completion integration under
  issue #12 before claiming this phase's exit criterion.
- Start with single-plate, single-extruder, no-MMU G-code.

Exit: one canonically registered target-tagged job can be queued, started,
paused, resumed, cancelled, completed, and reconciled without duplicate starts.

### Phase 4: current-Grove compatibility bridge

- Accept ADR 0006's Klove-owned runtime registry, embedded setup/recovery
  surface, strict completion message, and minimal Grove `KLOVE` type boundary.
- Decide compatibility-facade and Grove-theme licence and provenance.
- Use the Phase 3 runtime registry for the product onboarding path; do not add a
  second UI registry or return to per-printer TOML.
- Add the minimal conservative MQTT/TLS state/control facade and bounded FTPS
  spool, using only operations already accepted by their own ADRs.
- Build the Grove-themed embedded setup/recovery flow and propose the tiny
  `KLOVE` Add Printer contribution upstream through a fork.

Exit: an authorized Grove user can onboard and monitor one exact Klove printer
without entering Moonraker credentials into Grove or editing per-printer TOML,
and can safely dispatch only through accepted Klove contracts. Unsupported
commands are hidden, fail visibly if sent, and never reach a generic G-code
path.

### Phase 5: bounded live controls

- Separately decide and test temperature/speed and explicitly mapped fan/light
  operations. Keep jog and extrusion disabled until separately proven.

Exit: every enabled control has a current, exact capability and state proof,
bounded typed parameters, idempotency, reconciliation, and complete negative
tests.

### Phase 6: fleet hardening

- Multi-printer routing, per-printer credentials/policies, job journal, cameras
  via external URLs, metrics, backups, migration tests, and upgrade/rollback.
- Docker Compose examples for bridge and host-network constraints.

Exit: fault-injection and soak tests cover simultaneous printers, Klove/Grove/
Moonraker restarts, lost acknowledgements, corrupt uploads, stale config, and
credential rejection.

### Retired option: broad native Grove provider

The provider/backend abstraction formerly planned as Phase 7 is not planned.
It would create an unacceptable Klipper maintenance requirement in Grove. ADR
0006 instead gives Klipper printers an explicit `KLOVE` type, conservative
feature gates, embedded Klove-owned onboarding, and a Bambu-shaped compatibility
facade whose semantics remain wholly owned and enforced by Klove. Reopening the
broad provider option requires a new accepted decision and upstream agreement.

### Later adapters

- Exclude-object support, richer camera integration, explicitly supported
  MMU/toolchanger adapters, optional outbound host agent, and other printer
  stacks behind new southbound adapters.

## Validation strategy

- Unit and property tests for state reduction, command policy, limit changes,
  duplicate delivery, and every state-machine transition.
- Fuzz MQTT/JSON, URL, filename, ZIP/3MF, metadata, and G-code-header parsers.
- Contract tests that run Grove's real MQTT client against Klove and Klove
  against a deterministic fake Moonraker WebSocket/HTTP server.
- A native real-process Klipper/Moonraker integration test for authentication,
  typed controls, lost responses, exact single dispatch, and process restarts.
- Issue #12 integration tests for authorized intake/upload/start, metadata
  delays, lost acknowledgements, history reconciliation, cancellation,
  completion, substitution, restart and concurrent printers; component ADRs do
  not authorize a public workflow by themselves.
- Scripted browser tests for the real Grove parent/Klove frame handshake,
  independent Klove owner authentication, CSRF and exact-origin rejection,
  strict completion decoding, cancellation, responsive/accessibility behavior,
  privacy boundaries, `KLOVE` feature suppression, exact-printer queueing, and
  regression of existing Bambu types.
- Hardware-in-the-loop release-candidate tests on a dedicated printer with a
  known safe low-risk file. Heating, motion, and cancellation tests require an
  attended checklist and must never be part of routine CI.

Release only after negative tests demonstrate that mismatched G-code, unknown
macros, over-temperature requests, unhomed/out-of-bounds jogs, duplicate starts,
unauthorized Moonraker access, corrupt archives, and stale safety profiles all
fail closed.

## Immediate next slice

The artifact contract, hostile validator, exact target qualification, bounded
upload, and durable print-start components are complete under issues #5–#9.
ADR 0006 now freezes the missing product-onboarding boundary. The immediate
implementation prerequisite is the secure runtime printer registry and secret
store in [issue #58](https://github.com/tomlawesome/klove/issues/58). It unblocks
the target-bound intake-through-completion proof in
[issue #12](https://github.com/tomlawesome/klove/issues/12) without making Grove
or the browser part of that internal safety proof.

The compatibility provenance decision in
[issue #11](https://github.com/tomlawesome/klove/issues/11) may proceed in
parallel. Registry work then feeds the embedded setup/recovery UI in
[issue #59](https://github.com/tomlawesome/klove/issues/59), the conservative
MQTT/FTPS facade, and the minimal upstream Grove contribution in
[issue #60](https://github.com/tomlawesome/klove/issues/60). Track the complete
order under [Grove epic #32](https://github.com/tomlawesome/klove/issues/32) and
[programme roadmap #38](https://github.com/tomlawesome/klove/issues/38).

## Primary references

- [Moonraker architecture and API overview](https://moonraker.readthedocs.io/en/latest/)
- [Moonraker external API introduction](https://moonraker.readthedocs.io/en/latest/external_api/introduction/)
- [Moonraker printer administration](https://moonraker.readthedocs.io/en/latest/external_api/printer/)
- [Moonraker file management](https://moonraker.readthedocs.io/en/latest/external_api/file_manager/)
- [Moonraker authentication](https://moonraker.readthedocs.io/en/latest/external_api/authorization/)
- [Moonraker notifications](https://moonraker.readthedocs.io/en/latest/external_api/jsonrpc_notifications/)
- [Moonraker webcam management](https://moonraker.readthedocs.io/en/latest/external_api/webcams/)
- [Klipper API server](https://www.klipper3d.org/API_Server.html)
- [Klipper status reference](https://www.klipper3d.org/Status_Reference.html)
- [Mainsail overview](https://docs.mainsail.xyz/)
- [Grove Control repository](https://github.com/EdwardChamberlain/grove-control)
- [3MF Core Specification 1.3.0](https://3mf.io/wp-content/uploads/sites/106/2025/02/3MF_Core_Specification_v1.3.0.pdf)
- [Python `zipfile` documentation](https://docs.python.org/3/library/zipfile.html)
- [Bambu Studio 3MF implementation](https://github.com/bambulab/BambuStudio/blob/master/src/libslic3r/Format/bbs_3mf.cpp)
