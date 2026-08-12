# ADR 0001: stock Moonraker job control is not a safety boundary

Status: deferred

Date: 2026-08-12

## Context

Klove's read-only foundation deliberately contains no actuator transport. We
evaluated Moonraker's dedicated `printer.print.pause`,
`printer.print.resume`, and `printer.print.cancel` methods for the first control
slice.

## Findings

The method names are typed at Moonraker's external API, but they do not provide
the proof required by Klove's fail-closed policy:

1. A state query and a control request are separate operations. Another client
   or printer event can change the active job between them. Stock Moonraker
   offers no compare-and-act primitive binding a request to the queried job.
2. Moonraker forwards these controls to Klipper's pause/resume webhooks. Klipper
   then runs the `PAUSE`, `RESUME`, or `CANCEL_PRINT` G-code command. Operators
   can replace those commands with arbitrary macros, so endpoint selection does
   not prove actuator semantics.
3. A successful JSON-RPC response acknowledges request processing, not the
   resulting printer state. Lost responses and delayed notifications create
   outcomes that cannot be safely retried.
4. Independent WebSocket and HTTP connections do not prove that both resolved
   to the same remote instance when transport identity is weak.

## Decision

Do not add these methods to Klove. No control credential, mutating route, or
Moonraker actuator transport will ship in this slice. Command decoding remains
classification-only and ends in `actuation_disabled`.

Klove will first add the prerequisites that are safe without actuation:

- a boot-scoped opaque state token that binds revision, phase, filename, file
  position, and Moonraker event time;
- monotonic local evidence-receipt timestamps;
- race-safe waiting for newer observations;
- strict duplicate-header handling;
- executable repository rules proving that actuator methods and mutating routes
  remain absent.

The future control boundary must atomically compare target identity, boot
epoch, job identity, state, and a reviewed control-profile fingerprint before
executing one typed transition. This likely requires a small host-local
Moonraker component or Klipper extension. Choosing and deploying that component
is a separate architecture decision because it changes the earlier
central-sidecar-only deployment model.

## Consequences

- Klove remains monitoring-only after this slice.
- Pause, resume, and cancel wait until an atomic host-side gate exists.
- Common operator-defined pause macros can eventually be supported only after
  their live configuration matches an explicitly approved fingerprint.
- The path is less convenient, but it is consistent with the product invariant:
  uncertainty, however small, is denial.

## Primary references

- [Moonraker print job management](https://moonraker.readthedocs.io/en/latest/external_api/printer/#print-job-management)
- [Moonraker control implementation](https://github.com/Arksine/moonraker/blob/master/moonraker/components/klippy_apis.py)
- [Klipper pause/resume implementation](https://github.com/Klipper3d/klipper/blob/master/klippy/extras/pause_resume.py)
- [Moonraker status notifications](https://moonraker.readthedocs.io/en/latest/external_api/jsonrpc_notifications/#subscription-updates)
