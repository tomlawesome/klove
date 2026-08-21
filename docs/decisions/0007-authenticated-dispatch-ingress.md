# ADR 0007: accept one authenticated dispatch ingress

Status: accepted

Date: 2026-08-21

## Context

ADRs 0003–0005 separately prove hostile artifact qualification, one bounded
non-starting upload, and one durable at-most-once print start. None authorizes a
northbound caller to compose those services. A caller-controlled target,
profile, remote path, retry, or cancellation could bypass their individual
guarantees.

Klove needs one asynchronous intake-to-completion contract that can later sit
behind a narrowly approved machine or compatibility adapter. It must survive
client disconnects and Klove restarts without re-uploading or restarting an
ambiguous operation.

## Decision

Accept one transport-neutral dispatch ingress and coordinator with the
following contract.

### Authority and request

1. An ingress adapter must authenticate one exact principal with the
   `printers:dispatch` scope and an explicit grant for one canonical active
   registry printer UUID. The adapter passes that trusted context separately
   from hostile request fields. A Grove login, browser/setup session, private
   network location, model, name, route slug, filename, or discovery result
   grants nothing. Later HTTP, MQTT, or FTPS exposure requires its own approved
   adapter boundary.
2. Submission version 1 contains exactly one canonical UUIDv4 operation id and
   idempotency key, the exact granted printer UUID, one registered slicer-profile
   id, current safety-profile generation and fingerprint, one plate id and
   canonical archive member path, the declared archive byte count and SHA-256,
   and one streamed `.gcode.3mf` body. It accepts no URL, controller endpoint,
   credential, arbitrary local or remote path, generic G-code, command, macro,
   or start option.
3. The coordinator resolves only the canonical active registry record and the
   exact requested current profile. It computes the received byte count and
   digest, validates the archive, and constructs trusted target-approval
   evidence from the authenticated exact-printer grant plus that registry
   profile. Caller-supplied identity or archive metadata never becomes
   `ArtifactTargetApproval`. Disabled, missing, stale, changing, multiple, or
   mismatched records, profiles, gates, capabilities, or evidence deny before
   transport.
4. The operation id and idempotency key form one durable identity across
   intake, qualification, upload, start, and completion. An exact duplicate
   returns or waits for the existing operation. Reuse of either identity with
   any different authenticated principal, target, profile, plate, digest,
   length, or request fingerprint is denied. Each downstream service receives
   those same identities; no stage creates a retry identity.

### Input ownership and bounds

5. Klove reserves the operation before accepting bytes, streams them once into
   an owner-only operation-derived spool file, hashes while writing, and makes
   the complete file immutable to the workflow before durably recording
   `accepted`. Partial, oversized, slow, disconnected, or mismatched input is
   denied and its incomplete local file is removed. Client disconnect after
   `accepted` does not cancel the operation.
6. The configured archive limit is enforced while streaming and remains capped
   by ADR 0003 at 4 GiB; the default remains 512 MiB. Metadata is strict UTF-8
   with no unknown or duplicate fields and is capped at 8 KiB. Intake duration
   is configurable from 1 to 3,600 seconds with a 300-second default. Concurrent
   receives are configurable from 1 to 64 with a default of 4, and durable
   operation capacity is configurable from 1 to 100,000 with a default of
   1,024. Capacity exhaustion denies new work rather than evicting accepted or
   unresolved operations. Existing stricter archive, selected-G-code, upload,
   confirmation, and polling limits still apply.
7. Klove retains the exact source while a safe continuation or unresolved
   upload may still require it. It deletes a local source only after a durable
   pre-start terminal result or after exact start confirmation makes the source
   unnecessary. It retains operation identities, request fingerprints, exact
   target/digest evidence, outcomes, and every unresolved fence. It never logs
   source bytes and never automatically deletes a remote path; remote cleanup
   requires a separately accepted typed design. The spool, coordinator journal,
   registry, secret store, and start journal are one quiesced backup/restore
   set.

### Composition, restart, and completion

8. The coordinator calls only the existing validator, qualification policy,
   ADR-0004 upload service, and ADR-0005 start service, under the shared
   per-printer runtime gate. It derives the Moonraker destination solely as
   `gcodes/klove/<operation-id>.gcode`. Installation-wide and exact-printer
   dispatch opt-ins remain mandatory.
9. Before the upload call the coordinator durably records `uploading`, which
   means the request may have begun. A crash or cancellation in that state can
   never cause another upload. Verified upload evidence is committed before
   start admission; a crash before that commit becomes `outcome_unknown` and
   never starts. The durable start journal remains authoritative once start is
   reserved. A missing start row proves only that ADR 0005 could not have sent
   its RPC; an existing row is reconciled read-only and never replaced.
10. After exact start confirmation, the coordinator durably binds the operation
    to that immutable Moonraker history job id and start time. It reports
    `printing` only while coherent current/history evidence names that job, and
    reports `completed`, `cancelled`, or `failed` only from its exact terminal
    history status. Missing, substituted, contradictory, or bounded-timeout
    evidence is `outcome_unknown`. Later exact read-only evidence may resolve a
    post-start unknown result; it never permits another upload or start. An
    ambiguous upload never becomes qualification or start authority.
11. Startup and reconnect close ingress and dispatch for an affected printer
    until the registry, coordinator journal, source spool, upload reservations,
    and start journal agree. `accepted` work may resume. `uploading` work is
    never retried. Verified pre-start work may proceed only after all current
    evidence is repeated. Reserved or started work performs read-only
    reconciliation. Corrupt, missing, rolled-back, public, or inconsistent
    durable state fails readiness closed.

### Cancellation and lookup

12. Cancellation is a separate authenticated, target-bound, idempotent request
    for the same operation. Under the shared printer gate, a cancellation
    committed before `uploading` prevents upload; one committed after verified
    upload but before the durable start reservation prevents start. Once upload
    is ambiguous or start is reserved, cancellation cannot erase evidence,
    delete a path, infer failure, or issue a compensating action. Cancelling a
    printing job uses ADR 0001's separately authorized current-token cancel
    operation; dispatch cancellation never becomes a second cancel path.
13. The same principal and exact-printer grant may look up an operation by its
    canonical id. Cross-principal, cross-printer, missing, and expired
    pre-acceptance identities share one non-enumerating not-found response.
    Results expose only the contract version, operation and printer UUIDs,
    bounded lifecycle state, exact non-secret target/digest identities, and a
    closed failure code where applicable. They never contain source text,
    remote responses, endpoints, credentials, secret references, or logs.
    Submission and cancellation return only after their durable record exists;
    long-running progress is obtained by bounded polling of this result.

## Consequences

- The accepted workflow is asynchronous and deliberately sacrifices liveness
  after ambiguity. A crash before a network call may conservatively fence an
  operation that never left Klove.
- The coordinator needs a durable journal and private spool in addition to the
  existing start journal. Their schemas, permissions, reconciliation, capacity,
  and recovery set are safety-critical.
- Grove remains the user interface and queue owner, but no Grove-controlled
  field is dispatch authority. A future compatibility adapter may authenticate
  and route one exact printer only after separately satisfying this contract.
- This decision authorizes the internal contract and implementation under
  #75 — dispatch coordinator. It does not expose an HTTP, MQTT, FTPS, browser,
  generic G-code, arbitrary-path, upload-only, or print-start-only route.

## Tracking

- [Dispatch-ingress decision #74](https://github.com/tomlawesome/klove/issues/74)
- [End-to-end dispatch #12](https://github.com/tomlawesome/klove/issues/12)
- [Safe dispatch epic #33](https://github.com/tomlawesome/klove/issues/33)
