# Klove threat model

Status: active through runtime bootstrap, owner-session security, and the
accepted authenticated dispatch-ingress contract

## Protected assets

- human safety and the physical printer;
- printer firmware, configuration, files, and job state;
- Grove queue integrity and printer identity;
- Moonraker and Klove credentials;
- onboarding owner credentials, setup sessions, compatibility access codes, and
  runtime registry integrity;
- uploaded manufacturing artifacts and operational metadata.

## Trust boundaries

Grove payloads, MQTT delivery, uploaded archives, Moonraker responses,
WebSocket notifications, printer configuration, filenames, metadata, browser
input, frame/parent messages, origins, setup sessions, logs, and network peers
are untrusted. Configuration proves only operator intent; it does not prove that
live state is current. A Grove login and a private-network address are not
Klove owner authorization.

## Monitoring and identity controls

- Native status endpoints require a constant-time bearer credential loaded from
  a mounted file. Health endpoints disclose no printer data.
- Moonraker API keys are loaded from files and sent only to the configured
  endpoint through the documented WebSocket identification request or HTTP
  `X-Api-Key` header. They are never placed in URLs or logs.
- Configuration rejects unknown fields, embedded URL credentials, ambiguous
  printer identifiers, and unacknowledged cleartext remote transport.
- State is derived only after Moonraker and Klippy independently report ready,
  capabilities are rediscovered, and an initial subscription snapshot is
  validated.
- Unknown objects, states, stale event times, malformed messages, and
  contradictory evidence invalidate the affected printer state.
- Disconnect discards volatile capabilities and status. Reconnect never assumes
  continuity.
- Exposed state tokens are opaque and boot-scoped. They bind a dedicated
  control-evidence revision to connection state, capability fingerprint,
  canonical phase, filename, and Moonraker history job id and start time so a
  stale caller cannot reuse generic monitor progress or a revision number after
  Klove restarts. Event time and forward file position remain separately
  checked ordering evidence rather than token inputs.
- Duplicate authentication header instances are denied rather than combined.

## Required embedded-onboarding controls

ADR 0006 accepts the boundary below. Issue #62 — registry storage and issue
#63 — onboarding core implement private lifecycle persistence and direct
evidence. Issue #68 — runtime supervisor, issue #69 — shared runtime gate, and
issue #70 — runtime bootstrap implement canonical runtime wiring. Issue #71 —
owner session implements the independent credential, exact-origin/CSRF request
evidence, and bounded restart-invalidated session substrate. Issue #72 —
lifecycle routes, issue #73 — runtime handoff, and #59 — embedded setup/recovery
remain required before Klove claims product onboarding.

- Klove independently authenticates an owner over HTTPS before issuing a
  server-side setup session. The owner credential is never sent to Grove,
  `postMessage`, a URL, browser storage, telemetry, or logs. Loopback HTTP is an
  explicit development-only exception.
- Each setup session has one exact configured Grove origin, one unguessable flow
  nonce, one operation class, an inactivity limit of 15 minutes and an absolute
  limit of 30 minutes. Completion, cancellation, replay, concurrent use, origin
  change, or privilege mismatch invalidates or denies it. It cannot authorize a
  runtime printer action.
- The supported frame uses a distinct same-site Klove origin, a `Secure`,
  `HttpOnly`, path-scoped, `SameSite=Strict` cookie, exact-Origin and CSRF checks,
  and a route-specific CSP whose `frame-ancestors` contains only configured
  exact Grove origins. It does not rely on third-party cookies or wildcard
  origins.
- The frame sandbox grants only its required scripts, forms, and distinct
  origin. Klove self-hosts its bounded assets and denies popups, downloads, top
  navigation, parent DOM access, external scripts, inline script, analytics,
  telemetry, and service workers. Responses are no-store, no-referrer,
  no-sniff, and contain redacted bounded errors.
- Parent and frame verify exact origins and window references. Their
  ready/nonce/completion state machine uses exact `targetOrigin` values and
  strict versioned schemas; unknown, missing, duplicate, malformed, oversized,
  replayed, or out-of-order messages are denied.
- The completion bundle contains only the flow nonce, a bounded display name,
  stable `KLOVE-<UUID>` serial, Klove compatibility host/IP, and one freshly
  generated 20-character Base64url access code. Grove fixes the model to
  `KLOVE`. Moonraker endpoints, credentials, probes, profiles, and Klipper data
  never cross this boundary.
- The access code is necessarily exposed once to the authorized Grove parent
  because its existing MQTT/FTPS client requires that secret. It is bound to one
  exact registry printer, cleared from transient UI state immediately after the
  authorized create request, never returned to read-only Grove callers, and
  rotated or disabled only through authenticated Klove recovery.
- Registry changes are typed, bounded, transactional, auditable, idempotent,
  and fail closed across restart. SQLite stores opaque secret references rather
  than values. Secret creation, registry commit, rotation, disable/removal,
  backup, and restore cannot discard unresolved control or dispatch fences.
  Every existing-printer mutation requires a current composite fence proof;
  issue #64 must serialize that proof and commit with all new actuator admission
  through one shared per-printer runtime gate.
  Interrupted cleanup proves its target references are disjoint from every
  active or disabled printer before deleting anything. The database snapshot,
  complete secret directory and HMAC key, and separate durable actuator journals
  are restored only as one quiesced recovery set.
- Discovery grants no authority. A direct bounded Moonraker probe, exact
  identity, fresh capability evidence, exact profile binding, and operator
  intent must all agree. The probe accepts one canonical origin and an exact
  CIDR allowlist, validates every DNS answer, pins one address, disables ambient
  proxies and redirects, sends the key only in `X-Api-Key`, bounds time and
  bytes, and requires two equal strict identity/capability snapshots. Names,
  model strings, discovery advertisements, near matches, stale or changing
  probes, mixed DNS answers, collisions, and ambiguous endpoints cannot
  authorize onboarding or an actuator.
- Grove's explicit `KLOVE` type suppresses model-derived scheduling and every
  unsupported Bambu feature. Exact-printer routing is required, and Klove's
  server-side policy still rejects commands that a stale or modified Grove UI
  exposes.

## Typed job-control controls

- Global and per-printer configuration must both opt in. Authentication must
  yield the exact `printers:control` scope.
- The API accepts only pause, resume, and cancel with one canonical idempotency
  key and one exact boot-scoped state token. Duplicate or malformed headers,
  JSON members, operations, tokens, and keys are denied before orchestration.
- A per-printer lock serializes cached validation, immediate direct preflight,
  one dispatch, and post-action polling.
- Cached and live evidence must agree on the configured target, allowed phase,
  capability, filename, unique history job id and start time, event ordering,
  and non-regressing file position. Every accepted direct object sample is
  bracketed by two equal newest-history reads. Klove may resample that read-only
  bracket at most three times; it never retries an action. The caller's state
  token must match exactly both at admission and at the final cached recheck. A
  generic monitor revision arriving during the direct poll is accepted only if
  the exact token remains unchanged and its evidence is fully bounded by the
  direct preflight; changed, later, regressing, or contradictory evidence denies
  dispatch.
- Once dispatch may have occurred, every lost response, transport failure,
  mismatched job, contradictory observation, timeout, or internal failure is
  retained as `outcome_unknown`. The affected printer and state token remain
  fenced even if a caller changes its idempotency key. Ordinary telemetry and
  forward progress do not rotate the token; only changed control evidence can
  authorize another action.
- Dispatched and uncertain idempotency entries are never evicted. Capacity
  exhaustion denies new keys, and process restart invalidates all old state
  tokens.
- Repository policy confines the three dedicated Moonraker job-control methods
  to one adapter and the one accepted typed print-start method to another.
  Generic G-code and every other actuator remain absent.

## Accepted residual risk

Stock Moonraker query and control are not atomic. Another client can change the
job between Klove's final poll and the action. Klipper resolves the methods
through `PAUSE`, `RESUME`, and `CANCEL_PRINT`, which operators may replace with
macros. Printer owners accept responsibility for those macros and for
coordinating other clients. Klove limits the interval, binds any claimed success
to later evidence for the same job, and reports ambiguity without retrying. See
`docs/decisions/0001-typed-job-control.md`.

## Artifact inspection and target-qualification controls

- The v3 artifact contract accepts only one known archive format, canonical
  UUIDv4 operation/artifact/idempotency identities, lowercase SHA-256 digests,
  one selected plate with its exact canonical archive path, and one exact
  printer UUID, slicer-profile id, safety-profile generation, and fingerprint.
  V3 supersedes the unexposed v2 request because hostile inspection and trusted
  target approval must remain separate. Unknown contract members and
  non-canonical aliases are rejected.
- Archive compressed and expanded bytes, ZIP entry count, compression ratio,
  central-directory bytes, selected G-code bytes, header bytes, line bytes, and
  upload requests, metadata waits and polls, and upload idempotency capacity
  have finite operator limits whose own configuration is
  bounded. Ratio evidence carries exact compressed and expanded byte counts for
  the highest-ratio entry; aggregate consistency and the policy threshold are
  checked by integer cross-multiplication.
- Validation accepts only an immutable `bytes` snapshot, verifies its exact
  physical size and SHA-256, and retains that same object in the candidate. It
  rejects traversal and platform aliases, duplicate and case-colliding paths,
  links and special files, encryption, multi-disk archives, unsupported ZIP
  versions/compression, comments, malformed local or central records, excessive
  counts/bytes/ratios, and selected-member CRC failure. The central directory is
  capped from EOCD/ZIP64 records before Python's ZIP parser reads it.
- Only the exact selected regular member is decompressed. Its body is scanned in
  bounded chunks without retaining the expanded body, with exact digest/length,
  canonical ASCII line endings and controls, finite header/line limits, a known
  supported slicer marker, actual motion, and a conservative set of known
  Bambu-only generator and executable signatures.
- Qualification requires exactly one inspection and one independently trusted
  approval binding the same operation, idempotency key, archive, selected path,
  selected G-code digest and size, current printer UUID, registered slicer
  profile, generation, canonical fingerprint, Klipper dialect, nozzle, build
  volume, and plate. Unknown, missing, multiple, stale, contradictory, or
  mismatched evidence produces a bounded denial without echoing source text.
- Route slugs, filenames, model names, near matches, and self-asserted archive
  metadata are not target proof. The future external approval boundary must
  authenticate and authorize the controller or slicer registry before it may
  construct trusted approval evidence.
- A manual review record binds one exact file and records its actor, time, and
  reason, but its schema fixes automatic authority to false and the automatic
  policy always denies it.
- Qualification grants no transport or actuation authority. Only the separate
  ADR-0004 service may consume it after repeating current target and source
  checks; no northbound route constructs that service in this slice.

## Non-actuating upload controls

- A per-printer lock contains the final profile/source recheck, the one upload
  request, metadata polling, remote-file digest, and second metadata read. The
  exact retained archive is re-inspected and its selected member reopened
  immediately before the request. A stale printer UUID, profile id, generation,
  fingerprint, artifact, selected path, digest, or size denies before transport.
- The only destination is the canonical operation-derived
  `gcodes/klove/<operation-id>.gcode` path. The multipart request supplies the
  selected SHA-256 to Moonraker's checksum verifier and literal `print=false`.
  The response must be an exact non-starting `create_file` result with matching
  location, root, path, size, timestamp and permissions.
- Exact duplicates share one task and terminal result. An operation-id or
  idempotency-key collision denies. Accepted entries are retained in a bounded
  process journal; capacity exhaustion denies new work. A restart never infers
  upload authority from an existing remote filename.
- Once the request may have begun, a lost response, disconnect, timeout,
  malformed reply, cancellation, metadata delay, mismatch, remote digest
  mismatch, substitution, or changing metadata becomes `outcome_unknown`.
  Klove never blindly retries or deletes the uncertain path.
- Metadata must identify the exact path, byte size, upload timestamp and a
  canonical UUID, have bounded increasing command offsets, retain no job/start
  identity or file processors, and match the configured nozzle. Klove streams
  the remote file within the exact selected size and digest while equal
  metadata reads bracket that download.
- `VerifiedUpload` carries only remote-file evidence. It grants no authority by
  itself, and generic G-code remains absent. Only ADR 0005 may consume it after
  rechecking current evidence immediately before its separate one-time action.

## Durable print-start controls

- Installation-wide and per-printer dispatch gates must both opt in. The
  internal service consumes one exact `VerifiedUpload`; this slice adds no
  northbound route or user interface.
- Under one per-printer lock, Klove rechecks the exact current printer UUID,
  slicer-profile id, safety-profile generation and fingerprint. Exact metadata
  reads bracket a bounded remote-file size/SHA-256 stream and must still equal
  the non-started verified upload.
- An immediate direct preflight brackets the printer object sample with equal
  newest-history reads. The printer must be idle, and its newest history row
  must not already name the operation-unique path. Missing, changing,
  malformed, active or contradictory evidence denies before reservation.
- Before network actuation, Klove commits the complete operation, key, target,
  verified file and idle preflight as `dispatching` in an owner-only SQLite
  `STRICT` database. The exact schema, operation primary key, idempotency unique
  index, WAL mode and `synchronous=FULL` are verified fail closed. The container
  stores it on a dedicated persistent state volume. The reservation transaction
  atomically rejects another unresolved operation for that printer, including
  from another service instance.
- A `dispatching` row always means the one start RPC may have occurred. Klove
  never retries it after a timeout, lost or malformed response, cancellation,
  internal failure, Klove restart, or Moonraker reconnect. A crash before the
  network call can therefore leave a conservative unresolved fence.
- Confirmation ignores the RPC response and requires a strictly later
  Moonraker event time and an immutable history identity for the exact operation
  path. When preflight had a prior history job, the confirming id must differ
  and its start time must be later. A non-idle live filename and phase must
  agree with that history; an exact fast terminal history row may prove the
  start after the printer has returned to idle.
- Startup and reconnect close the dispatch gate while unresolved rows are
  reconciled through reads only. Contradiction, transport failure or timeout is
  durably `outcome_unknown`. An unresolved row fences only its exact printer
  across all new keys; ordinary telemetry cannot clear it.
- The journal contains operational evidence, not credentials, but its integrity
  is safety-critical. Missing, public, symlinked, wrong-schema, corrupt or
  unavailable storage prevents dispatch. Rollback, backup, recovery and
  migration procedures remain required fleet-hardening work.

## Accepted print-start residual risk

Stock Moonraker cannot atomically combine Klove's final file/state reads with
`printer.print.start`. Another authorized client can replace or start the path
inside that interval. Printer owners accept responsibility for coordinating
other clients. Klove binds any claimed outcome to later exact evidence and
never retries an ambiguity. A host component is not justified for the accepted
boundary; any future atomic host primitive requires its own decision and tests.
See `docs/decisions/0005-durable-moonraker-print-start.md`.

## Authenticated dispatch-ingress controls

ADR 0007 accepts one internal asynchronous intake-to-completion contract. The
coordinator is not yet implemented and no northbound route is exposed.

- Only an independently authenticated `printers:dispatch` principal with an
  exact canonical printer grant may submit, cancel, or read an operation. A
  Grove login, setup session, browser, private network, route slug, model, name,
  filename, or request target claim grants nothing.
- Klove streams one bounded `.gcode.3mf` into an owner-only operation-derived
  spool, computes its size and digest, and constructs approval only from the
  authenticated printer grant plus one exact current canonical registry safety
  profile. URLs, arbitrary paths, generic G-code, commands, and caller-created
  approval evidence are rejected.
- One durable operation/idempotency identity spans validation, qualification,
  upload, start, and terminal history. A committed `uploading` state means the
  request may have begun and is never retried. The ADR-0005 start journal
  remains authoritative once start is reserved.
- Cancellation can prevent a not-yet-begun upload or start, but never erases an
  ambiguity, deletes a remote path, retries a request, or cancels a live print.
  Live job cancellation remains only ADR 0001's exact current-token operation.
- Exact immutable history identity proves printing and terminal completion.
  Restart and reconnect reconcile read-only; missing, substituted,
  contradictory, rolled-back, corrupt, or unavailable evidence fails closed.
- The private spool, coordinator journal, canonical registry, secret store, and
  durable start journal form one quiesced backup/restore set. Accepted and
  unresolved operations are never evicted to admit new work.

Issue #75 — dispatch coordinator must implement this composition and issue #76
— native dispatch proof must cover authentication, duplicate delivery,
substitution, cancellation, restart, completion, and cross-printer isolation
before target-bound dispatch is complete. Every later actuator requires its own
typed parameters, positive capability and policy evidence, and accepted
decision; the absence of any one item is denial.
