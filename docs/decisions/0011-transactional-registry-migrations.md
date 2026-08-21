# ADR 0011: accept transactional registry migrations

Status: accepted

Date: 2026-08-21

## Context

The canonical printer registry is an exact SQLite `STRICT` schema at version 1.
Startup creates a new empty database, but rejects every other schema version and
every structural or data inconsistency. That is the correct fail-closed
foundation, but it cannot evolve the same registry as fleet storage grows.

Issue #34 orders the migration decision and exact migration harness before any
profile or mapping history, cross-service fence references, backup tooling, or
fault-isolation behavior. The migration foundation therefore must prove how an
existing registry changes safely without deciding any later schema.

## Decision

Accept one registry-only, startup-time migration boundary with these rules:

1. `PRAGMA user_version` remains the canonical schema version. Version 0 means
   only a newly created database with an empty SQLite application catalog; it is
   initialization, not a migration. A version-0 database containing any
   application object or content is rejected without modification.
2. Each supported historical version has an immutable exact schema validator.
   Before changing anything, Klove validates the private database file, WAL and
   `synchronous=FULL` connection, source schema, indexes, canonical decoded
   records, denormalized columns, integrity result, and registry HMAC-key
   identity. Unknown, future, too-old, corrupt, partial, or weakened sources are
   denied with fixed redacted diagnostics.
3. Production migrations are an explicit contiguous sequence of one-version
   steps. Klove never skips a version, guesses a source layout, repairs hostile
   state, imports a second registry, or discovers migrations dynamically.
4. Startup runs the complete sequence under one `BEGIN EXCLUSIVE` SQLite
   transaction before registry reconciliation, bootstrap import, runtime
   activation, listeners, or readiness. Each step uses bounded deterministic
   SQL and in-process transformations. The final step sets `user_version`, then
   Klove validates the exact target schema and decoded data before commit. Any
   exception rolls the whole sequence back. A lock timeout or competing writer
   fails startup closed.
5. Reopening either the original source or the completed target after a crash
   is deterministic. Klove resumes only from a fully committed supported
   version; it never uses an out-of-band progress marker or treats a partially
   transformed schema as authoritative.
6. Production downgrade migrations are not supported. An older binary rejects
   a newer schema. Rollback is supported only by restoring the complete
   quiesced pre-upgrade recovery set or while the schema version is unchanged.
   Tests must prove this rejection rather than destructively projecting data
   backwards.
7. The first implementation adds the migration runner and exact harness while
   version 1 remains current and has no production migration step. A later
   accepted schema decision must add both the next exact schema descriptor and
   its single explicit migration. The framework must not create a migration
   history table or otherwise change version 1 merely to test itself.
8. The harness retains a reviewed, immutable, secret-free logical fixture for
   every supported source version, including populated records and interrupted
   registry operations. It constructs private SQLite files and matching
   ephemeral secret stores at test time; no credential or HMAC-key value is
   committed. Fixture digests prevent accidental historical-schema rewrites.
9. Tests cover clean initialization, exact historical validation, every
   transaction phase through deterministic fault injection, rollback and
   reopen, concurrent initializers, lock timeout, corrupt and weakened source
   state, version gaps, future and too-old versions, target-validation failure,
   idempotent reopen, opaque-reference preservation, and absence of secret
   material from databases and diagnostics. The safety-critical boundary keeps
   100% statement and branch coverage.
10. Migration logs expose only a fixed result code and numeric source and target
    versions. They never include record JSON, SQL, credential references,
    secret material, remote content, or exception text.

This decision migrates only the canonical registry database. Separate
print-start, coordinator, and future durable actuator-fence stores remain
independently versioned and fail closed under their accepted contracts. A
recoverable installation still requires the registry snapshot, complete secret
directory and HMAC key, every separate durable fence, and retained dispatch
state from one quiesced point. A database-only snapshot is not a recoverable
backup, and operational backup/restore remains issue #18.

## Consequences

- Registry evolution is automatic only for explicitly supported, exactly
  validated versions and remains unavailable while any ambiguity exists.
- One transaction makes multi-step registry upgrades restart-safe without an
  additional journal, while exclusive startup serialization prevents two
  service instances from racing a migration.
- The immutable harness becomes the compatibility gate for every later schema
  change; changing an old fixture or validator requires separate review and is
  not a substitute for a new migration.
- Profile/mapping history, cross-service fence references, retention, complete
  backup/restore commands, and per-printer fault isolation remain separate
  ordered fleet-hardening decisions and implementations.

## Tracking

- [Registry fleet storage #17](https://github.com/tomlawesome/klove/issues/17)
- [Fleet hardening epic #34](https://github.com/tomlawesome/klove/issues/34)
- [Registry storage contract](../registry-storage.md)

