# Klove threat model

Status: initial, enforced by the read-only foundation

## Protected assets

- human safety and the physical printer;
- printer firmware, configuration, files, and job state;
- Grove queue integrity and printer identity;
- Moonraker and Klove credentials;
- uploaded manufacturing artifacts and operational metadata.

## Trust boundaries

Grove payloads, MQTT delivery, uploaded archives, Moonraker responses,
WebSocket notifications, printer configuration, filenames, metadata, logs, and
network peers are untrusted. Configuration proves only operator intent; it does
not prove that live state is current.

## Initial controls

- The first slice has no printer-changing Moonraker method and no arbitrary
  G-code transport.
- Native status endpoints require a constant-time bearer credential loaded from
  a mounted file. Health endpoints disclose no printer data.
- Moonraker API keys are loaded from files and sent only through the documented
  connection-identification request. They are never placed in URLs or logs.
- Configuration rejects unknown fields, embedded URL credentials, ambiguous
  printer identifiers, and unacknowledged cleartext remote transport.
- State is derived only after Moonraker and Klippy independently report ready,
  capabilities are rediscovered, and an initial subscription snapshot is
  validated.
- Unknown objects, states, stale event times, malformed messages, and
  contradictory evidence invalidate the affected printer state.
- Disconnect discards volatile capabilities and status. Reconnect never assumes
  continuity.
- Exposed state tokens are opaque and boot-scoped. They bind a revision to its
  Moonraker event time, canonical phase, filename, and file position so a stale
  caller cannot reuse a revision number after Klove restarts.
- Duplicate authentication header instances are denied rather than combined.

## Deferred stock Moonraker control

Moonraker's dedicated pause, resume, and cancel requests are not accepted as a
sufficient actuator boundary. Query and control are not atomic, another client
can change the job between them, and Klipper may resolve the request through an
operator-defined G-code macro. Klove therefore contains no such transport or
mutating route. See `docs/decisions/0001-typed-job-control.md`.

## Required future controls before actuation

Every operation requires an authenticated Grove identity, exact printer route,
current state revision, explicit capability, configured policy, bounded typed
parameters, idempotency key, and observable postcondition. The absence of any
one item is denial. Uploaded 3MF/G-code validation and target binding must be
complete before print dispatch exists.
