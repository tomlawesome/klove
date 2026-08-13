# ADR 0002: keep Klove headless and automation-first

Status: accepted

Date: 2026-08-13

## Context

Klove is a security-critical translation layer between Grove Control and
Moonraker/Klipper. Grove already owns the normal operator experience. Adding a
second dashboard or asking operators to copy state between controllers would
increase exposed surface, duplicate workflow, and replace reliable protocol
evidence with error-prone human input.

Some future workflow may contain an irreducible human choice or recovery
decision. If so, a responsive browser can be the least burdensome way to obtain
that input, but convenience alone does not justify a Klove user interface.

## Decision

Klove remains headless and automation-first:

1. Grove owns normal user interaction. Klove sends Grove conservative canonical
   state, capabilities, operation outcomes, and reconciliation status through
   the bounded MQTT/TLS compatibility contract and, later, the native provider
   API. Artifact movement remains prohibited until a separately accepted
   spool/dispatch contract exists.
2. Klove sources evidence directly from configured controllers and exchanges
   structured data between controllers. It does not ask an operator to
   transcribe information that can be discovered or reconciled automatically.
3. Missing, stale, contradictory, or ambiguous evidence is reconciled where a
   bounded proof exists and otherwise fails closed. A prompt is not a substitute
   for evidence or policy.
4. Klove exposes only the machine interfaces needed for health, translation,
   and explicitly accepted operations. It has no dashboard, configuration or
   secret editor, generic command console, or duplicate print workflow.
5. Human input is reserved for a demonstrated choice or recovery action that
   cannot be derived safely. Any Klove-local Web UI is a last resort and
   requires a separate accepted decision defining that exact interaction,
   authentication, session and CSRF controls, browser security, accessibility,
   and removal criteria. It must be the smallest possible same-origin surface
   and must never connect a browser directly to Moonraker.

## Consequences

- When introduced, the Grove bridge must carry enough typed state and outcome
  detail for Grove to present stale evidence, denials, and `outcome_unknown`
  correctly.
- The native provider boundary remains the long-term operator integration; it
  does not create a Klove frontend.
- Configuration and credentials remain operator-managed files and secret
  mounts. Diagnostics remain machine-readable and redacted.
- No frontend framework, browser bundle, or UI service is added to Klove now.
- A future UI proposal must first prove why controller-to-controller exchange,
  automatic reconciliation, Grove, and bounded CLI/configuration paths are
  insufficient.

## Tracking

- [GitHub decision #40](https://github.com/tomlawesome/klove/issues/40)
