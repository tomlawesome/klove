# Safety and command policy

Klove's safety policy is proof-based: any uncertainty, however small, is a
denial. The accepted canonical operations are currently limited to:

- pause, resume, cancel

ADR 0007 accepts one authenticated, registry-bound artifact-dispatch
composition. Its internal coordinator and native lifecycle proof are complete,
but every northbound compatibility adapter remains absent.
The roadmap proposes the following later operations, but each remains prohibited
until its own ADR accepts the complete typed contract and safety evidence:

- set hotend or bed target
- set speed multiplier
- set a mapped fan or light
- home
- bounded jog
- bounded extrude/retract
- exclude objects
- invoke an explicitly configured macro with a declared parameter schema

The following are design constraints for any future accepted operation, not
authorization to implement it:

- Reject unknown MQTT commands and arbitrary `gcode_line` by default.
- Pause/resume/cancel use Moonraker's typed print endpoints, not G-code strings.
- Any future heater-target design must use the lower of the operator policy and
  discovered Klipper limit, with a configured safety margin.
- A future jogging design would require idle state, homed axes, current
  `toolhead.axis_minimum/axis_maximum`, and below configured distance/speed
  limits. Whether Klove may generate any bounded sequence requires its own ADR.
- A future extrusion design would require idle state, a selected hotend
  reporting `can_extrude`, and configured length/rate limits.
- Future fan and light commands require explicit object or reviewed macro
  mappings. Never guess by substring alone.
- Future printer, heater, fan, and macro targets must be selected from
  discovered allowlists; they must never be interpolated from a Grove payload.
- Any future live control while printing must be separately accepted as safe
  for that exact state.

[ADR 0001](../decisions/0001-typed-job-control.md) is the canonical contract
for stock Moonraker pause, resume, and cancel. The printer owner is responsible
for the correctness and safety of any Klipper macros replacing `PAUSE`,
`RESUME`, or `CANCEL_PRINT`. Klove mitigates, but cannot remove, Moonraker's
non-atomic query/control interval: it requires an exact state token and job
match, serializes by printer, polls immediately before one dispatch, and binds
confirmation to the same Moonraker history job id and start time. The exact
token is checked again after the direct poll; unrelated telemetry cannot rotate
it, while any control-state or job-identity change does. Any ambiguity after
dispatch is `outcome_unknown` and never triggers a blind retry. No generic
G-code or print-start transport is part of this decision.

Grove sends MQTT with QoS 1, so Klove must assume duplicate delivery. Deduplicate
commands by printer, command kind, sequence/task ID, and payload hash. A repeated
`project_file` must return/re-emit the existing operation result; it must never
start a second print.

## Validation expectation

Authentication, authorization, translation, and command-safety code starts and
remains at 100% statement and branch coverage. The full validation strategy,
including negative tests and hardware boundaries, is in
[deployment, delivery, and validation](deployment.md). The artifact, upload,
print-start, and ingress contracts are canonical in
[ADRs 0003–0005](../decisions/0003-exact-artifact-target-qualification.md) and
[ADR 0007](../decisions/0007-authenticated-dispatch-ingress.md).
