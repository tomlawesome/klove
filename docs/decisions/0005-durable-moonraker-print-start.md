# ADR 0005: accept durable typed Moonraker print start

Status: accepted

Date: 2026-08-14

## Context

ADR 0004 can place and independently verify one operation-unique G-code file
without starting it. Starting that file is an actuator. Moonraker's stock
`printer.print.start` RPC and its live-state and history reads are not one
atomic transaction: another client, a Moonraker or Klipper restart, or a lost
response can make the outcome unknowable. Repeating the RPC in that state could
start a second job.

Klove therefore needs an at-most-once boundary that survives its own restart.
It must prefer a visible unresolved operation over an automatic retry, even
when a crash occurred after the durable reservation but before the network
request.

## Decision

Accept stock Moonraker's dedicated `printer.print.start` JSON-RPC method for
one exact verified path. No host-installed Klove component is required. The
contract is:

1. Print start is disabled unless installation-wide `dispatch.enabled` and the
   exact printer's `dispatch_enabled` are both true. The service consumes only
   ADR-0004 `VerifiedUpload` evidence; it has no generic G-code interface.
2. Under one per-printer lock, Klove rechecks the exact current printer UUID,
   slicer-profile id, safety-profile generation and fingerprint. It then reads
   the operation path's metadata, streams the remote file through its exact
   size and SHA-256, and requires a second identical metadata read. Every field
   must still equal the verified upload, including the absence of a prior job
   identity or start time.
3. Immediately before reservation, Klove brackets one direct
   `printer.objects.query` sample with equal newest `server.history.list`
   identities. Up to three complete read-only samples may obtain coherence.
   The printer must be idle, and the newest history entry must not already use
   the operation path.
4. The canonical operation id, idempotency key, complete verified-upload
   evidence, exact idle preflight, and target printer UUID are committed as a
   `dispatching` row in a private SQLite `STRICT` database before any start RPC.
   The database uses WAL mode and `synchronous=FULL`; its schema, primary key,
   unique idempotency index, owner and owner-only file/directory permissions
   are validated fail closed. The same SQLite write transaction rejects a new
   reservation while another unresolved row fences that printer, including
   across separate service instances.
5. A committed `dispatching` row means the RPC may have happened. Klove sends
   at most one authenticated JSON-RPC request whose exact method is
   `printer.print.start` and whose only parameter is the verified filename.
   It never sends generic G-code and never retries the start, including after
   cancellation, timeout, disconnect, malformed response, or process crash.
6. A response is not confirmation. Klove polls coherent live state and newest
   immutable history. Confirmation requires the operation path, an event time
   strictly later than the recorded preflight. If preflight recorded a prior
   history job, the confirming job id must differ and its start time must be
   strictly later. Any live
   non-idle filename and phase must agree with that exact job. A fast terminal
   history row may confirm that the start occurred even when the live printer
   has already returned to idle.
7. A contradictory observation is immediately `outcome_unknown`. Missing
   evidence remains pending only until the bounded confirmation deadline.
   Transport failure or timeout is `outcome_unknown`; it never triggers a
   second action.
8. On Klove startup and Moonraker reconnect, the dispatch gate remains closed
   while every `dispatching` or `outcome_unknown` row for that printer is
   reconciled by reads only. Exact later evidence may durably change it to
   `confirmed`. Otherwise the unresolved row fences that printer across every
   operation id and idempotency key. Only exact later confirmation evidence, or
   a separately accepted recovery design, may clear the fence; ordinary
   telemetry cannot.
9. Exact duplicates return the durable result. Reusing either identity with
   different evidence is denied. A confirmed row is terminal and cannot be
   replaced. Storage failure closes the gate and dispatches nothing.

## Consequences

- At-most-once safety deliberately sacrifices automatic liveness. A crash
  between committing `dispatching` and calling Moonraker leaves an unresolved
  row even if no request was sent. Klove will not guess or retry it.
- Stock Moonraker is sufficient for this accepted boundary. A host component
  remains unjustified unless a later requirement needs a genuinely atomic
  file-and-start primitive and receives its own decision.
- Another authorized Moonraker client can still replace or start the file in
  the narrow interval after Klove's final file/state checks. Printer owners are
  responsible for coordinating such clients. Klove detects later
  contradiction, reports ambiguity, and never retries, but it cannot remove
  this stock-API race.
- SQLite durability protects Klove restarts, not loss or rollback of the state
  volume. Backup, restore, retention, migrations, and operator recovery remain
  fleet-hardening work and must preserve unresolved fences.
- This slice supplies a domain service and typed Moonraker adapter. It adds no
  user interface or northbound dispatch route. The end-to-end intake-through-
  completion workflow remains issue #12.

## Primary references

- [Moonraker JSON-RPC API](https://moonraker.readthedocs.io/en/latest/external_api/jsonrpc/)
- [Moonraker print management API](https://moonraker.readthedocs.io/en/latest/external_api/printer/)
- [Moonraker job history API](https://moonraker.readthedocs.io/en/latest/external_api/history/)
- [SQLite WAL](https://sqlite.org/wal.html)
- [SQLite `PRAGMA synchronous`](https://sqlite.org/pragma.html#pragma_synchronous)

## Tracking

- [GitHub slice #9](https://github.com/tomlawesome/klove/issues/9)
- [Dispatch epic #33](https://github.com/tomlawesome/klove/issues/33)
