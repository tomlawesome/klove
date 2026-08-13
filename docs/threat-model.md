# Klove threat model

Status: active through the hostile-artifact validation slice

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

## Artifact validation controls

- The v2 artifact contract accepts only one known archive format, canonical
  UUIDv4 operation/artifact/idempotency identities, lowercase SHA-256 digests,
  one selected plate with its exact canonical archive path, one exact printer
  id, one slicer-profile id, and one safety-profile fingerprint. V2 supersedes
  the unexposed v1 request because exact selection cannot be inferred safely
  from a plate id. Unknown contract members and non-canonical aliases are
  rejected.
- Archive compressed and expanded bytes, ZIP entry count, compression ratio,
  central-directory bytes, selected G-code bytes, header bytes, line bytes, and
  future metadata waits have finite operator limits whose own configuration is
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
- Validation requires exactly one complete candidate matching the intent and
  the currently configured target. Unknown, missing, multiple, stale, or
  contradictory evidence produces a bounded denial without echoing source
  text.
- Validation success is not proof of target/profile compatibility and grants no
  transport or actuation authority. The current slice creates no file, contacts
  no printer, and introduces no upload or print-start transport.

## Required controls before print start and later actuators

Uploaded 3MF/G-code validation, target binding, and durable dispatch
reconciliation must be complete before print start exists. Every later actuator
requires its own typed parameters, positive capability and policy evidence, and
an accepted decision; the absence of any one item is denial.
