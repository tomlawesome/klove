# ADR 0010: define bounded Grove FTPS artifact staging

Status: proposed; implementation prohibited until the observation gates below
are complete and this decision is accepted

Date: 2026-08-21

## Context

Grove's current queue path uploads a `.3mf` through implicit FTPS before it
publishes a separate MQTT `print.project_file` request. ADR 0006 intends a
compatibility facade, while ADR 0007 requires a separately approved adapter
before any northbound file source may reach its dispatch coordinator.

The retained normal-path client capture establishes two implicit-FTPS TLS 1.3
sessions, the generated access-code mapping, ordered cleanup and protected
passive-upload commands, and exact transfer integrity for one generated test
archive. It does not establish reply/error semantics, PASV deployment and NAT
behavior, production filename grammar, concurrency, retry/disconnect behavior,
or the later MQTT correlation. Guessing any of those behaviors would create an
unauthenticated path, overwrite, traversal, cross-printer disclosure,
resource-exhaustion, or unintended dispatch risk.

## Proposed decision

Accept one file-only FTPS staging boundary after its exact wire profile is
completed from approved ADR-0008 black-box evidence. A successful transfer
creates only private, immutable, bounded staged bytes and a non-secret internal
receipt. It never validates, uploads to Moonraker, starts a print, issues a
control, or grants dispatch authority.

### Clean-room protocol gate

1. Before this decision can become accepted, approved fixtures for the exact
   supported Grove revision must record the minimum required implicit-FTPS
   handshake, TLS behavior, login field mapping, FTP commands and arguments,
   working-directory and filename behavior, passive-mode behavior, data-channel
   protection, response codes, upload completion, disconnect, and retry flow.
   Every fixture follows ADR 0008's provenance, sanitization, separation, and
   revision rules.
2. Klove's versioned FTPS profile enumerates every accepted command, state,
   argument grammar, reply, ordering rule, and bound. Unknown commands,
   extensions, encodings, active mode, plaintext FTP, explicit TLS upgrade, and
   unobserved behavior fail closed. A Grove revision change blocks the
   compatibility claim until the clean-room lane passes again.

### TLS, login, and exact-printer binding

3. The control connection uses implicit TLS from its first byte. Certificate
   and private-key files are named through deployment configuration; their
   values never enter tracked configuration, logs, metrics, replies, fixtures,
   or process arguments. The observed compatible TLS versions, certificate
   validation, session-resumption behavior, and cipher policy must be recorded
   before acceptance. Plaintext fallback is prohibited.
4. A login authenticates exactly one active canonical registry printer through
   the owner-only compatibility-secret boundary. The observed username field
   carries no independent authority. The access code is compared without
   revealing whether another printer, username, or secret exists; successful
   server-side resolution creates one compatibility principal limited to file
   staging for that exact printer.
5. Rotation, disable, removal, or record revision change invalidates new logins
   immediately and closes affected control and data sessions within a bounded
   interval. Authentication and transfer admission share the per-printer gate
   with lifecycle operations. An authenticated session cannot select another
   printer through a path, filename, FTP account field, network address, or
   later command.

### Narrow FTP and passive-data surface

6. Klove supports only commands proven necessary by the observation gate. It
   always denies active data mode, arbitrary directory navigation, download,
   append, restart/resume, rename, delete, chmod, timestamp mutation, checksum
   claims, server filesystem discovery, and SITE commands unless a specific
   read-only compatibility command is both observed and accepted here. No FTP
   command exposes staged bytes or another client's names.
7. Passive listeners use an explicit bounded deployment port range. Each data
   socket is single-use, short-lived, cryptographically protected, bound to one
   authenticated control-session transfer reservation, and accepted from only
   the permitted control peer under the documented container/network policy.
   A connection cannot steal, reuse, race, or attach to another session's
   passive reservation. Advertised addresses and host-network/NAT behavior must
   be explicit; unsupported topology fails readiness.
8. Configuration places conservative hard maxima on global and per-printer
   sessions, login attempts, command line and argument bytes, commands per
   session, passive listeners, concurrent transfers, archive bytes, idle time,
   transfer time, minimum progress, shutdown time, and retained staged files.
   Capacity exhaustion rejects new work and never evicts a complete unconsumed
   stage, unresolved transfer record, or security fence.

### Path and byte ownership

9. Klove accepts one observed canonical `.3mf` upload name grammar. It rejects
   empty, absolute, nested, dot-segment, repeated-separator, alternate-encoding,
   Unicode-confusable, control-character, trailing-dot/space, reserved, and
   near-match names before filesystem use. Client names are stored only as
   bounded correlation data; server paths are owner-only and derived from a
   generated canonical staging identifier.
10. The server durably reserves a staging identity before receiving bytes,
    streams once into a new owner-only temporary file while computing byte
    count and SHA-256, enforces ADR 0003's archive hard limit, flushes the
    complete file, and atomically commits an immutable workflow stage before a
    success reply. It rejects links, special files, replacement, overwrite,
    truncation, sparse expansion, and path races.
11. Partial, slow, oversized, disconnected, failed-TLS, shutdown-interrupted,
    or otherwise ambiguous inbound transfers never become complete stages and
    their temporary bytes are removed through exact owner-checked cleanup. A
    lost success reply may cause Grove to upload again, but it cannot cause
    downstream action. An exact same-name/same-byte repeat may resolve to the
    original complete receipt after hashing; a same-name conflict is denied and
    preserves the first stage.
12. A complete receipt contains only its contract version, staging identifier,
    exact printer UUID, canonical client-name identity, byte count, SHA-256,
    creation/expiry evidence, and consumed state. It contains no bytes, local
    path, endpoint, credential, secret reference, raw FTP text, or Moonraker
    identity. Cross-principal and cross-printer lookup is non-enumerating.

### Separation from dispatch

13. FTPS completion supplies bytes, not authority. It cannot construct an
    `ArtifactTargetApproval`, choose a registered slicer profile, profile
    generation or fingerprint, choose an archive member or plate, create a
    dispatch operation/idempotency identity, call the ADR-0007 coordinator, or
    make a Moonraker request.
14. A later separately accepted compatibility-dispatch adapter must bind one
    exact authenticated MQTT request to one unexpired, unconsumed stage for the
    same canonical printer; supply or safely derive every ADR-0007 request
    field; and consume the stage at most once. Missing, multiple, expired,
    changing, conflicting, or already consumed correlation denies. Proposed
    ADR 0009 deliberately keeps `print.project_file` unsupported until that
    joint contract exists.
15. The staging journal and byte store are owner-only, crash-safe,
    capacity-bounded, reconciled before readiness, and included with the
    registry, secrets, dispatch spool/journal, start journal, and actuator
    fences in one quiesced backup/restore set. Cleanup may remove only an exact
    expired unconsumed stage proven disjoint from every active correlation and
    operation; ambiguity retains bytes and denies capacity.

## Required validation before acceptance

- ADR-0008-compliant black-box fixtures and manifests define every retained
  FTPS protocol token and observed behavior at the pinned Grove revision.
- TLS, login, authorization, parser, state machine, passive binding, path,
  staging, replay, cleanup, lifecycle-race, shutdown, and policy code has 100%
  statement and branch coverage with negative, property, fuzz, restart, and
  resource-exhaustion tests.
- Contract tests prove plaintext/explicit-TLS rejection, exact-printer login,
  credential rotation/disable, control/data peer binding, passive-port bounds,
  traversal and overwrite denial, partial cleanup, lost-reply repeat handling,
  cross-printer non-enumeration, restart reconciliation, and secret absence.
- The dependency review selects maintained TLS/FTP components with acceptable
  provenance and a restricted surface, or separately justifies and tests the
  bounded protocol implementation. Grove source and dependencies never become
  Klove implementation inputs.
- The exact supported Grove client can stage one harmless bounded archive in an
  isolated private network while no production validator, upload, start, or
  control transport is reachable from the FTPS service.

## Consequences

- Issue #14 — FTPS ingress may perform clean-room observation and protocol
  design, but runtime implementation remains blocked while this ADR is proposed
  or its exact wire profile is incomplete.
- Issue #79 — completion handoff supplies the shared compatibility secret; that
  one-time disclosure grants no artifact or actuation authority by itself.
- Issue #12 — end-to-end dispatch and issue #76 — native dispatch proof are
  completed prerequisites for a later FTPS/MQTT-to-dispatch correlation
  boundary.
- Issue #32 — Grove bridge gains no generic file server, arbitrary path, remote
  cleanup, upload-only API, or second print-start path.

## Tracking

- [FTPS ingress #14](https://github.com/tomlawesome/klove/issues/14)
- [End-to-end dispatch #12](https://github.com/tomlawesome/klove/issues/12)
- [Grove bridge #32](https://github.com/tomlawesome/klove/issues/32)
