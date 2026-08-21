# Runtime, persistence, and operations

Persist only what Klove owns: one canonical runtime printer registry, opaque
secret references, confirmed capability mappings, safety-profile history,
artifact/operation identifiers, and bounded idempotency journals. Secret values
remain in owner-only storage outside the database. Grove remains the queue and
production system of record; Moonraker/Klipper remains the execution state of
record. Per-printer TOML is a one-time idempotent bootstrap input, not a
parallel product registry: exact file entries are directly probed and imported
into the canonical registry, and every live route is then derived from that
durable record by canonical printer UUID. File/database drift fails startup
closed.

The implemented persistence foundation, two-phase credential protocol, and
inseparable backup/restore set are specified in
[registry storage](../registry-storage.md). The implemented address-pinned
direct probe, revisioned lifecycle transitions, composite actuator-fence
requirement, and failure/restart semantics are specified in
[onboarding core](../onboarding-core.md).

On startup or reconnect:

1. query Klippy and current `print_stats`/`virtual_sdcard` state;
2. load only existing exact durable operation rows; never infer authority from
   an operation-looking filename;
3. reconcile each unresolved start through coherent live/history reads only;
4. retain every unproven row as a per-printer fence and never retry its action;
5. publish the reconciled snapshot before accepting new dispatches.

Use bounded exponential backoff with jitter, periodic full-state resync, atomic
artifact writes, graceful shutdown that never cancels an active printer job,
redacted structured logs, health/readiness endpoints, and per-printer circuit
breakers. A Klove outage must not stop an already running Klipper print.

Container hardening should include a non-root user, read-only root filesystem,
no Docker socket, narrowly mounted data and secret volumes, dropped Linux
capabilities except `NET_BIND_SERVICE` only if ports 990/8883 require it,
private network exposure by default, and an egress allowlist to configured
Moonraker hosts. Do not log access codes, Moonraker keys, JWTs, URLs containing
secrets, or uploaded G-code bodies.
