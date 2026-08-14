# Registry storage and recovery contract

Status: registry foundation implemented; runtime and onboarding wiring remain
tracked in issues #63–#65.

## Boundary

Klove has one canonical versioned SQLite/WAL printer registry. It stores exact
printer UUIDs, Moonraker origins, direct identity and capability evidence,
safety-profile bindings, lifecycle state, opt-in flags, revisions, opaque
credential references, and secret-free operation/audit records. It does not
store credential values.

Moonraker credentials and Grove-compatibility access-code copies live as
separate owner-only files under opaque UUIDv4 references. The same private
directory contains a 32-byte request-fingerprint HMAC key. Persisted request
fingerprints are keyed digests; they are not hashes from which submitted secret
material can be tested offline. Active Moonraker credentials must be visible
ASCII tokens of at least 32 characters; compatibility copies must be exactly 20
Base64url characters as frozen in ADR 0006.

The foundation is a persistence interface, not a listener or product setup
path. It adds no route, network probe, printer runtime, UI, upload, print start,
or generic G-code capability. Issue #63 owns the bounded direct Moonraker probe
and lifecycle orchestration, #64 owns dynamic runtime activation, and #65 owns
the protected onboarding API.

## Mutation and recovery protocol

Every create, update, credential rotation, disable, removal, or bootstrap import
uses one canonical idempotency key and keyed request fingerprint:

1. SQLite atomically reserves a secret-free `preparing` operation. At most one
   preparing operation may exist for the exact printer, including an aborted
   key being retried.
2. New credential files are created once with mode `0600` under the reserved
   opaque references. Secret values are bounded visible ASCII tokens and never
   enter SQLite.
3. One SQLite transaction validates the exact current revision and typed
   lifecycle transition, writes the new canonical printer record, and marks the
   operation `committed`.
4. Only after that commit are retired credential files deleted. The operation
   is finalized after the deletions are durable.

Startup reconciliation aborts every interrupted `preparing` operation and
removes only its uncommitted new references. It completes retirement for every
committed operation. Before either cleanup, it proves that the cleanup set is
disjoint from every currently active or disabled printer reference. Missing,
orphaned, short, public, symlinked, unknown, mismatched, or contradictory files
fail closed. Removed printers remain as revisioned tombstones without credential
references.

The schema is exact and `STRICT`. Startup validates the schema version, table
and index definitions, canonical JSON, denormalized lookup columns, SQLite
integrity result, private ownership, and the HMAC-key identity recorded in the
database. A future or weakened schema is rejected; migrations require a later
explicitly tested slice.

## Backup and restore

The recoverable registry unit consists of all of the following from one
quiesced state:

- a consistent SQLite snapshot produced by `PrinterStore.backup`;
- the complete owner-only secret directory, including `.request-hmac-key` and
  every referenced credential file; and
- the existing durable print-start journal and any other unresolved operation
  fence stored elsewhere in Klove state.

The SQLite snapshot deliberately does not copy secret values. Stop Klove or
otherwise quiesce registry mutations before copying the secret directory and
creating the database snapshot. Do not copy a live database file, `-wal`, and
`-shm` independently, and do not combine a database snapshot with a secret
directory from another time or installation.

Restore the complete set before starting Klove. The registry and credential
files must be owner-owned regular files with no group or other permissions; the
secret directory must likewise be owner-only. Initialization then validates the
HMAC-key identity, reconciles interrupted local secret work, validates every
active reference and secret length, and rejects any orphan or omission. It
never invents a credential, drops a tombstone, or clears a control/dispatch
fence to make a restore start.

Operational backup/restore commands and container-volume guidance remain a
later operations slice. Until those commands exist, this document defines the
library and recovery boundary; it is not a claim that normal users should
manipulate these files.
