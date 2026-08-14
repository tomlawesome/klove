# ADR 0004: accept bounded non-actuating Moonraker upload

Status: accepted

Date: 2026-08-14

## Context

ADR 0003 produces an exact target-bound qualification but deliberately writes
nothing. The next boundary must place only its selected G-code bytes on the
qualified Moonraker host, prove that the host retained the same bytes and
metadata, and grant no print-start authority when the upload result is lost or
ambiguous.

Moonraker's documented HTTP upload can verify a caller-supplied SHA-256 and can
upload without starting a print. Its response and later metadata reads are not
atomic with the remote filesystem, metadata processing, another client, or a
connection loss. A successful response alone therefore does not prove that the
intended path still contains the qualified file.

## Decision

Accept stock Moonraker's file API for one non-actuating upload slice. A
host-installed Klove component is not required for this boundary. The
client-side contract is:

1. An upload consumes only ADR-0003 `ArtifactQualification` evidence and its
   retained immutable archive. Under a per-printer lock Klove compares the
   exact current printer UUID, slicer-profile id, safety-profile generation and
   fingerprint, then re-inspects the same archive immediately before opening
   the exact selected member. Missing, changed, stale, or contradictory
   evidence denies before any file request.
2. Each operation has one canonical UUIDv4 operation id and idempotency key.
   It writes only `gcodes/klove/<operation-id>.gcode`. Colliding operation ids
   or keys are denied. Exact concurrent duplicates share one task and terminal
   result. The bounded process journal never evicts an accepted operation;
   exhaustion denies new work.
3. Klove sends one `POST /server/files/upload` multipart request with root
   `gcodes`, path `klove`, the exact selected G-code SHA-256 in `checksum`, and
   literal `print=false`. It accepts only HTTP 201, the exact documented
   `Location`, `create_file` response, read-write item, returned path, byte
   count and timestamp, with both `print_started` and `print_queued` false.
4. From the moment the upload call begins, any lost response, disconnect,
   timeout, malformed response, internal failure, or cancellation is
   `outcome_unknown`. Klove never repeats the upload with the same or a new key
   and does not delete an uncertain remote path.
5. After a successful response Klove polls the exact metadata path immediately
   and then at a bounded interval until the configured deadline. It requires
   exact path, size, upload timestamp, canonical metadata UUID, bounded command
   offsets, no prior job identity/start time, the configured nozzle diameter,
   and an empty file-processor list. Missing metadata at the deadline or any
   mismatch is `outcome_unknown`.
6. Klove streams the exact remote file back through SHA-256 with an exact byte
   limit. Two metadata reads bracketing that download must be identical. The
   downloaded size and digest must equal the qualified selected member. Only
   then does Klove return immutable `VerifiedUpload` evidence.
7. `VerifiedUpload` is non-actuating evidence, not permission to start by
   itself. It is not exposed by a user-facing workflow in this slice. Print
   start remains absent until ADR 0005 and issue #9 define durable at-most-once
   dispatch and restart reconciliation. No generic G-code method is added.

## Consequences

- Stock Moonraker remains sufficient for bounded file placement; a host agent
  is deferred unless a later requirement demonstrates that the residual
  remote-filesystem race cannot be tolerated.
- Another authorized Moonraker client can still replace a file outside Klove's
  lock. The receipt/metadata/download/metadata bracket detects changes during
  verification, while the later start slice must revalidate current remote
  evidence immediately before its own action.
- A process restart does not reconstruct upload authority from a path that
  happens to exist. The later durable journal may reconcile it read-only, but
  may never infer success or blindly re-upload after an ambiguous outcome.
- File processors are rejected because modifying selected G-code breaks the
  byte-exact ADR-0003 identity. Supporting a processor requires its own trusted
  transformation contract and decision.

## Primary references

- [Moonraker file management API](https://moonraker.readthedocs.io/en/latest/external_api/file_manager/)
- [Moonraker file-manager implementation](https://github.com/Arksine/moonraker/blob/master/moonraker/components/file_manager/file_manager.py)

## Tracking

- [GitHub slice #8](https://github.com/tomlawesome/klove/issues/8)
- [Dispatch epic #33](https://github.com/tomlawesome/klove/issues/33)
