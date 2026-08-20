# Grove compatibility and onboarding

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
model values drive scheduling and hardware features. [ADR 0006](../decisions/0006-embedded-grove-onboarding.md)
therefore uses a stable `KLOVE-<UUID>` proxy serial, a 20-character generated
access code, and an explicit `KLOVE` model with conservative gates.

## Compatibility facade

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
recovery after independent owner authentication. It contains no status dashboard
or controls. Normal onboarding does not edit per-printer TOML; file
configuration remains deployment/bootstrap input.

`KLOVE` is an explicit conservative type, not a fake Bambu model. Grove must
suppress model-derived file matching and scheduling, firmware, maintenance,
AMS, HMS, drying, calibration and unsupported hardware controls. Initial queue
routing is exact-printer only. Klove independently rejects anything outside its
accepted runtime contracts.

The frame and parent use exact origins and window references, an explicit
ready/nonce handshake, strict versioned schemas, and no wildcard `postMessage`.
Moonraker and Klove-owner credentials never cross the browser handoff or enter
Grove. The Klove route is protected by owner authentication, short-lived
server-side sessions, CSRF and exact-Origin checks, route-specific CSP
`frame-ancestors`, a minimal iframe sandbox, no-store/no-referrer responses, and
self-hosted assets. The complete browser and completion policy is frozen in ADR
0006.

Compatibility is claimed only for an exact tested Grove revision. The currently
reviewed baseline is `cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4`. Klove has
read-only access upstream, so the small Grove contribution is built in a fork
and proposed normally. Upstream rejection leaves the compatibility facade and
standalone recovery available for development, but does not justify a permanent
private fork or revive the native-provider programme.

## Retired option: broad native Grove provider

The provider/backend abstraction formerly planned as Phase 7 is not planned.
It would create an unacceptable Klipper maintenance requirement in Grove. ADR
0006 instead gives Klipper printers an explicit `KLOVE` type, conservative
feature gates, embedded Klove-owned onboarding, and a Bambu-shaped compatibility
facade whose semantics remain wholly owned and enforced by Klove. Reopening the
broad provider option requires a new accepted decision and upstream agreement.
