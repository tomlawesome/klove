# ADR 0001: accept bounded stock Moonraker job control

Status: accepted

Date: 2026-08-13

## Context

Klove's read-only foundation deliberately contained no actuator transport. The
first control slice requires pause, resume, and cancel without introducing a
generic G-code interface or a host-installed Klove component. Moonraker exposes
dedicated `printer.print.pause`, `printer.print.resume`, and
`printer.print.cancel` methods for these operations.

## Findings

The methods have important limitations:

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
4. Independent WebSocket monitoring and HTTP control connections can observe
   different moments in printer state and rely on the configured endpoint's
   transport identity.

## Decision

Accept the stock Moonraker methods for this deliberately narrow slice. Printer
owners accept responsibility for the correctness and safety of their Klipper
`PAUSE`, `RESUME`, and `CANCEL_PRINT` commands, including any operator-defined
macros that replace them. Klove does not infer macro semantics or claim that
Moonraker makes query-and-control atomic.

The client-side contract is fail-closed:

1. Control requires explicit installation-wide and per-printer opt-in plus an
   authenticated principal with the exact control scope.
2. The route binds one configured printer id to one endpoint and credential.
   The caller supplies a boot-scoped state token binding the exact dedicated
   control-evidence revision, phase, capability fingerprint, filename, and
   Moonraker history job id and start time. Generic snapshot revision,
   Moonraker event time, and ordinary forward file progress are deliberately
   not token inputs: those values remain separately checked ordering evidence,
   while unrelated telemetry cannot make every real request stale.
3. A per-printer lock serializes preflight, dispatch, and reconciliation. Under
   that lock Klove validates capability, phase, cached token, and job identity;
   reads the newest Moonraker history entry, polls printer objects immediately
   before the action, and reads history again; requires one unchanged unique
   history job id, start time, and filename with matching live phase and
   non-regressing event time and file position; then rechecks monitor evidence.
   The caller's exact token must match both at admission and after that direct
   poll. A generic monitor revision arriving during the poll is accepted only
   when the token remains exactly equal and its ordering evidence is fully
   bounded by the direct poll. Any control-token, job, later, regressing, or
   contradictory evidence denies dispatch.
4. A canonical idempotency key creates one process-epoch operation. Duplicate
   requests await or return the original result. Entries for operations that
   dispatched or may have dispatched are never evicted; journal exhaustion
   denies new work. An uncertain result fences every later request against the
   same printer and state token, even when it uses a different idempotency key;
   ordinary telemetry or forward progress does not bypass this fence; changed
   control evidence must produce a new state token. A restart changes every
   state token, preventing replay of a prior-epoch request.
5. Klove dispatches exactly one parameter-free dedicated method. It never
   retries a dispatch, and it exposes no generic G-code or print-start method.
6. After dispatch, Klove brackets every direct object read with history. A poll
   may take at most three read-only samples to obtain equal before/after history
   evidence; this is observation resampling, never action retry. A coherent
   sample must retain the preflight job id, start time, and filename and show a
   later event in the operation's exact target phase. Pause and resume also
   require file position not to regress; cancel permits Klipper's documented
   reset of virtual SD position. A lost response, unavailable or persistently
   changing history, transport error, job mismatch, contradictory evidence,
   timeout, or internal ambiguity becomes `outcome_unknown`.

The non-atomic interval between the final poll and Moonraker applying the
method remains a residual race. The accepted behavior is to make that interval
small, serialize Klove-originated actions, never claim success without a bound
postcondition, and never turn uncertainty into a blind retry. Deployments that
need a stronger multi-client atomic guarantee require a future host-side
protocol and a separate decision; it is not a prerequisite for this slice.

## Consequences

- Pause, resume, and cancel are available only through the typed native route
  when both configuration gates are enabled.
- Operators must coordinate other Moonraker clients and own their macro safety;
  Klove cannot prevent an external client changing a job during the residual
  non-atomic interval.
- `outcome_unknown` means an action may have happened. Callers must refresh
  state. Another action is possible only if changed control evidence yields a
  new state token; ordinary progress leaves the uncertainty fence intact.
- Moonraker's core history component supplies the unique entry id and print
  start time. Malformed, absent, or persistently changing history evidence
  denies before dispatch or becomes `outcome_unknown` afterward.
- Print start, arbitrary G-code, and all other actuators remain prohibited.
- The pinned native Klipper/Moonraker integration fixture verifies this client
  contract against real upstream processes. It does not validate RatOS host
  behavior or operator-defined macro safety, which remain separate attended
  acceptance responsibilities.

## Primary references

- [Moonraker print job management](https://moonraker.readthedocs.io/en/latest/external_api/printer/#print-job-management)
- [Moonraker job history](https://moonraker.readthedocs.io/en/latest/external_api/history/)
- [Moonraker history notifications](https://moonraker.readthedocs.io/en/latest/external_api/jsonrpc_notifications/#history-changed)
- [Moonraker control implementation](https://github.com/Arksine/moonraker/blob/master/moonraker/components/klippy_apis.py)
- [Klipper pause/resume implementation](https://github.com/Klipper3d/klipper/blob/master/klippy/extras/pause_resume.py)
- [Moonraker status notifications](https://moonraker.readthedocs.io/en/latest/external_api/jsonrpc_notifications/#subscription-updates)
