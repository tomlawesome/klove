# Artifact and dispatch safety

The largest semantic hazard is the print file, not the control API. A G-code
file sliced for a Bambu machine is not made safe for a Klipper printer by
renaming it or extracting it from a 3MF. Klove must not rewrite a foreign start
G-code dialect or silently ignore commands.

Artifact policy is staged. Its validation, qualification, upload and durable
start components are implemented behind separate evidence boundaries. ADR 0007
also has its internal authenticated intake-to-completion coordinator. It
reserves an operation before consuming a bounded streamed archive into its
owner-only spool, binds every later stage to the canonical registry profile and
exact upload/start evidence, and keeps ambiguous work fenced for read-only
reconciliation. Every northbound adapter remains unimplemented:

1. Accept Grove's `.gcode.3mf` container only when its selected plate contains
   G-code sliced for the target Klipper profile. Never support unsliced geometry
   in the first dispatch path.
2. Require a target identity in a manifest/comment or an exact registered
   slicer-profile identifier. Bind it to a Klove printer UUID and safety-profile
   fingerprint. Legacy files without proof are denied; the automatic path does
   not replace missing controller evidence with a human override.
3. Reject known Bambu-only G-code signatures and mismatched printer/nozzle/build
   metadata.
4. Treat the 3MF as a hostile ZIP: cap upload/compressed/uncompressed sizes and
   entry count; reject traversal, links, encrypted entries, duplicate paths, and
   compression bombs; stream only the chosen plate G-code.
5. Under [ADR 0004](../decisions/0004-bounded-moonraker-upload.md), upload once
   to a unique `klove/<operation-id>.gcode` path through Moonraker with the
   selected digest and `print=false`; wait for metadata processing, bracket a
   bounded remote-file digest with identical metadata reads, and retain every
   post-request ambiguity without retry.
6. Under [ADR 0005](../decisions/0005-durable-moonraker-print-start.md),
   revalidate that exact target, metadata and remote digest, require a coherent
   idle preflight, durably reserve the complete operation, and issue at most one
   typed start request. A response never authorizes a retry or proves success.
7. Observe the expected Moonraker filename/state transition before reporting a
   successful start to Grove. If the response is lost, reconcile current state
   and history instead of retrying blindly.

The implemented artifact prerequisite remains deliberately non-actuating. Its
strict v3 contract keeps hostile byte inspection separate from independently
trusted target approval. The validator accepts one immutable byte snapshot,
bounds and validates hostile ZIP/ZIP64 metadata before parsing, and streams only
the exact selected G-code through integrity and conservative syntax/vendor
checks. Qualification then requires the same exact bytes in one
controller/slicer approval and one current configured safety profile. Canonical
printer UUID, registered slicer profile, generation, fingerprint, Klipper
dialect, nozzle, build volume, and plate must all agree. Names and near matches
are not proof; manual overrides remain audit-only and are denied by automation.
Qualification itself writes nothing.

[ADR 0003](../decisions/0003-exact-artifact-target-qualification.md) is the
canonical qualification contract. ADR 0004 may consume only that exact
qualification, recheck the current profile and immutable source, upload the
selected G-code once with Moonraker checksum verification and `print=false`, and
emit verified remote-file evidence only after bounded metadata and byte-digest
reconciliation. ADR 0005 may consume only that `VerifiedUpload`, repeat current
target/file/live checks, commit a durable pre-dispatch reservation, send one
typed start, and confirm only from later exact history and monotonic state
evidence. [ADR 0007](../decisions/0007-authenticated-dispatch-ingress.md)
accepts one asynchronous coordinator that binds their shared identities to an
authenticated exact-printer grant, canonical registry target, private spool,
durable lifecycle result, restart reconciliation, and safe cancellation.
[Issue #75 — dispatch coordinator](https://github.com/tomlawesome/klove/issues/75)
implements that internal integration. No northbound route exists yet.

Longer term, Grove's slicer sidecar can produce target-specific G-code using a
registered Klipper profile. That is re-slicing, not protocol translation, and
must remain a separate optional service.
