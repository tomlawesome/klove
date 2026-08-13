# Klove threat model

Status: active through the first typed-control slice

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
- Exposed state tokens are opaque and boot-scoped. They bind a revision to its
  Moonraker event time, canonical phase, filename, and file position so a stale
  caller cannot reuse a revision number after Klove restarts.
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
  capability, filename, state token, event ordering, and non-regressing file
  position. The token is rechecked after the direct poll.
- Once dispatch may have occurred, every lost response, transport failure,
  mismatched job, contradictory observation, timeout, or internal failure is
  retained as `outcome_unknown`. The affected printer and state token remain
  fenced even if a caller changes its idempotency key; only a newly observed
  state token can authorize another attempt.
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

## Required controls before print start and later actuators

Uploaded 3MF/G-code validation, target binding, and durable dispatch
reconciliation must be complete before print start exists. Every later actuator
requires its own typed parameters, positive capability and policy evidence, and
an accepted decision; the absence of any one item is denial.
