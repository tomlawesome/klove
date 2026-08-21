# ADR 0012: accept immutable registry profile and capability history

Status: accepted

Date: 2026-08-21

## Context

The version-1 canonical registry retains one current `RegisteredPrinter` JSON
document. An update can replace direct Moonraker/Klipper identity and capability
evidence or its safety-profile set, so the prior trusted mapping is no longer
available for fleet audit and recovery analysis. ADR 0011 now supplies the
exact transactional migration boundary required before extending that schema.

Issue #34 orders immutable profile and mapping history before durable
cross-service fence references, backup tooling, isolation, observability, and
soak evidence. This decision must add history to the existing registry without
making history a second source of current authority.

## Decision

Accept registry schema version 2 with two append-only history tables:

1. `registry_mapping_history` stores one complete, versioned, canonical JSON
   snapshot of directly probed printer identity and capability evidence when
   its confirmed mapping changes. Denormalized columns bind the row to the
   exact printer UUID, registry revision, observation time, and a SHA-256
   mapping fingerprint.
2. The mapping fingerprint covers every identity and capability field except
   `observed_at_unix_ms`. A fresher equal mapping updates current evidence but
   appends no duplicate history. Any hostname, software-version, object-set, or
   derived-capability change requires a strictly later observation and appends
   exactly one new mapping row in the same transaction and revision as the
   current printer update. Names, models, or near matches never create a row.
3. `registry_profile_history` stores exact full `SafetyProfile` JSON for
   `baseline`, `bound`, and `retired` events. Denormalized columns bind each row
   to printer UUID, registry revision, slicer-profile id, generation, profile
   fingerprint, and event. A profile fingerprint is the existing canonical
   ADR-0003 fingerprint over all safety-relevant profile fields.
4. New profiles append one `bound` event. Removal appends one `retired` event
   containing the exact former profile. Replacing the same slicer-profile id
   appends the old `retired` and new `bound` events at one revision and requires
   the new generation to be strictly greater than every retained generation for
   that printer and slicer-profile id. An exact unchanged profile appends
   nothing. Rebinding a retired id obeys the same monotonic-generation rule.
5. Current authorization continues to consume only the exact current
   `registry_printers` record. History is audit evidence and cannot restore,
   enable, qualify, or authorize a printer, profile, route, credential, upload,
   control action, or print start.
6. Every create or update writes its current record, operation result, and all
   required history rows in the existing single optimistic registry
   transaction. A stale revision, collision, missing row, inconsistent
   generation, encoding failure, or database error rolls back all of them.
   Disable and removal append nothing unless their canonical identity or
   profile set also changes.
7. Exact database triggers reject every `UPDATE` or `DELETE` against either
   history table. Only a later accepted schema migration may temporarily
   replace those exact triggers inside ADR 0011's exclusive migration
   transaction. Production store methods expose append and read operations
   only.
8. The version-1-to-version-2 migration validates the complete v1 source, then
   deterministically inserts one mapping snapshot and one `baseline` row for
   every retained profile in every current or removed printer record. A
   baseline records the last v1 snapshot at that record's revision; it does not
   claim when the mapping or profile was first bound. No credential value is
   read or copied. Target schema, triggers, denormalized fields, decoded JSON,
   and row coverage are validated before commit.
9. Mapping history is ordered by `(printer_uuid, registry_revision)`. Profile
   history is ordered by `(printer_uuid, registry_revision,
   slicer_profile_id, event)`. Internal queries require one exact printer UUID,
   an exclusive composite cursor, and a limit from 1 through 1,000. They return
   stable ascending pages, never scan or return another printer, and add no
   northbound route in this slice.
10. Version 2 validation rejects missing or extra tables, indexes, or triggers;
    malformed or noncanonical JSON; unknown events; denormalized disagreement;
    incomplete v1 backfill; duplicate primary identities; non-monotonic profile
    generations; invalid fingerprints; and history that references an absent
    canonical printer. Failures use existing bounded `RegistryStoreError`
    handling without record, SQL, path, reference, or exception-text logging.

The version-2 logical fixture is immutable, reviewed, digest-bound, and
secret-free. Tests construct the matching v1 source, secret directory, and HMAC
key ephemerally, run the production migration, and prove exact reopen and
rollback. Production downgrade remains unsupported; version-1 binaries reject
version 2.

## Consequences

- Fleet audit can prove which direct capability mapping and exact safety
  profile changes were committed without treating historical state as current
  authority.
- History grows only on meaningful mapping or profile changes. Retention and
  deletion are intentionally absent because either would require a separate
  accepted evidence-lifecycle decision.
- Cross-service actuator-fence references, operational backup/restore,
  per-printer isolation, observability, and soak testing remain later ordered
  fleet-hardening slices.

## Tracking

- [Immutable registry history #110](https://github.com/tomlawesome/klove/issues/110)
- [Registry fleet storage #17](https://github.com/tomlawesome/klove/issues/17)
- [Fleet hardening epic #34](https://github.com/tomlawesome/klove/issues/34)
- [Transactional registry migrations](0011-transactional-registry-migrations.md)

