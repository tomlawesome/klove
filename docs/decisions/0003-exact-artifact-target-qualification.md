# ADR 0003: require exact artifact target qualification

Status: accepted

Date: 2026-08-14

## Context

Byte-safe G-code is not necessarily safe for a particular printer. A file can
name a plausible model or slicer preset while carrying the wrong nozzle,
envelope, plate, or command dialect. Archive metadata is hostile input, and a
route slug is an operational address rather than a stable printer identity.

Klove needs a machine-verifiable boundary between hostile artifact inspection
and any later transport. That boundary must invalidate prior evidence when the
configured printer profile changes and must not turn a human acknowledgement
into unattended dispatch authority.

## Decision

1. Every configured printer has a canonical UUIDv4 distinct from its route
   slug. UUIDs and route slugs are each globally unique in one Klove
   configuration. Names, models, aliases, and near matches never identify a
   target.
2. A safety profile binds the printer UUID, an exact registered slicer-profile
   identifier, a positive generation, Klipper G-code, nozzle diameter, build
   volume, and build-plate identity. Physical dimensions use bounded integer
   micrometres so equivalent-looking floating-point values cannot alias.
3. Klove computes the profile fingerprint as lowercase SHA-256 over canonical
   sorted compact JSON containing every safety-profile field. Operators must
   advance the generation whenever a profile is replaced, including a change
   that later restores prior field values. A changed field changes the
   fingerprint; a changed generation prevents an earlier approval from being
   revived by reversion.
4. Hostile inspection and target approval remain separate evidence. One target
   approval binds its authority and approval identities to the exact operation,
   idempotency key, source artifact digest and size, selected member path,
   selected G-code digest and size, printer UUID, profile generation,
   fingerprint, and all compatibility fields. Self-asserted archive comments,
   filenames, model names, and the request's own target claim are not approvals.
   A future external boundary may supply this type only after authenticating
   and authorizing the controller or registered slicer-profile authority.
5. Qualification requires exactly one byte inspection, exactly one independent
   target approval, and one current configured profile. Missing, unknown,
   multiple, stale, contradictory, or mismatched evidence is denied. Nozzle,
   volume, plate, dialect, slicer-profile, and printer mismatches have bounded
   non-reflective denial codes.
6. A successful result carries one immutable, non-actuating qualification that
   repeats the exact approved artifact, selected G-code, target, and approval
   authority. Later upload and start slices must compare that qualification
   with current configuration again immediately before their own action.
7. A manual review record is explicit and bound to one operation, idempotency
   key, file, selected G-code, target, actor, timestamp, and closed reason. Its
   `automatic_dispatch_allowed` field is literally `false`; presenting any
   manual override to the automatic policy produces a denial. Grove may retain
   and present the audit record, but it cannot convert the record into Klove
   upload or print-start authority.
8. This decision adds no network endpoint, upload, print start, generic G-code,
   motion, or local user interface.

## Consequences

- Printer configurations must add a stable UUID. Artifact-capable installations
  must explicitly register every accepted current safety profile and manage its
  generation.
- The v3 artifact contract supersedes the previously unexposed v2 contract.
  V3 represents qualification rather than treating inspection as compatibility
  proof.
- Registered slicer/controller integration must preserve the exact approval
  record and authenticate its source; untrusted target fields alone always
  fail closed.
- Upload remains blocked on issue #8. Print start remains blocked on a separate
  accepted decision and issue #9's durable at-most-once design.

## Tracking

- [GitHub slice #5](https://github.com/tomlawesome/klove/issues/5)
- [Dispatch epic #33](https://github.com/tomlawesome/klove/issues/33)
