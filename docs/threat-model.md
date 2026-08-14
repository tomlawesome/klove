# Klove threat model

Status: active through exact artifact target qualification

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
- Repository policy permits only the three dedicated Moonraker job-control
  methods in one adapter. Generic G-code, print start, and other actuators remain
  absent.

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
- `VerifiedUpload` carries only remote-file evidence. It cannot start a print,
  and generic G-code remains absent. A later durable start slice must recheck
  current evidence immediately before its separate one-time action.

## Required controls before print start and later actuators

Uploaded 3MF/G-code validation, target binding, and durable dispatch
reconciliation must be complete before print start exists. Every later actuator
requires its own typed parameters, positive capability and policy evidence, and
an accepted decision; the absence of any one item is denial.
