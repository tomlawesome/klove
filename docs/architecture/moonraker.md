# Moonraker and Klipper integration

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
6. [ADR 0004](../decisions/0004-bounded-moonraker-upload.md) accepts HTTP
   `/server/files/upload` only for bounded, operation-unique, checksum-verified
   placement with `print=false`, remote digest verification, and no northbound
   route.
7. [ADR 0005](../decisions/0005-durable-moonraker-print-start.md) accepts one
   exact `printer.print.start` JSON-RPC only after current target/file checks,
   coherent idle preflight and durable SQLite/WAL reservation. Startup/reconnect
   reconciliation is read-only and unresolved rows fence their printer without
   retry.
8. [ADR 0007](../decisions/0007-authenticated-dispatch-ingress.md) accepts one
   internal coordinator that composes exact qualification, upload, start, and
   terminal history under a shared durable operation identity. No northbound
   adapter exists yet.
9. Use Moonraker metadata and history to estimate remaining time and reconcile
   jobs after either side restarts when the relevant slice is authorized.

Minimum required Klipper objects for farm dispatch are `virtual_sdcard`,
`print_stats`, and `pause_resume`. A printer missing one should remain visible
for monitoring but be marked dispatch-ineligible with a concrete diagnostic.

The onboarding probe is a separate one-shot HTTP boundary, not the long-lived
runtime client. It accepts only a canonical origin and an exact deployment CIDR
allowlist, rejects the whole resolution if any DNS answer is disallowed, pins
one deterministic peer address, and requires two equal bounded
`server.info`/`printer.info`/`printer.objects.list` snapshots. Its result is
committed only through the revisioned lifecycle service in
[onboarding core](../onboarding-core.md). Discovery names and advertisements
never select or authorize that endpoint.

## Subscriptions

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

## Capability discovery

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
