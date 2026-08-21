# Deployment, delivery, and validation

## Delivery sequence

### Phase 0: contract and fixtures

- Apply ADR 0008's clean-room boundary: Grove virtual-printer code and assets
  are not reused in Klove.
- Capture only minimal sanitized black-box Grove request/report observations
  with exact-revision fixture manifests, plus Moonraker snapshots/notifications
  from representative Klipper configurations.
- Freeze canonical state, capability, command, operation, and error schemas.
- Write the threat model and artifact acceptance policy before enabling motion or
  heating.

Exit: mappings and rejected behaviours are executable tests, not only prose.

### Phase 1: read-only Moonraker core

- Bootstrap configuration/secrets, Moonraker authentication, discovery
  primitives, WebSocket subscriptions, state reducer, capability probe,
  reconnect, and health API. Product onboarding is completed by ADR 0006's
  later runtime-registry and setup/recovery slices.
- Support one and then multiple fake/real Moonraker endpoints.

Exit: stable monitoring through Moonraker restarts and network partitions; no
printer-changing command exists.

### Phase 2: typed Moonraker job control

- Deliver the [ADR-0001](../decisions/0001-typed-job-control.md) pause, resume,
  and cancel contract through the typed native route.
- Keep print start, generic G-code, temperature, speed, fan, light, motion, and
  extrusion absent.

Exit: one exact current job-control request is dispatched at most once, and every
post-dispatch ambiguity is retained as `outcome_unknown` without retry.

### Phase 3: runtime registry and safe file dispatch — complete

- ADR 0006's one canonical runtime printer registry chain is complete under
  issue #58 — registry epic. Issue #62 — registry storage implements its
  exact-schema SQLite/WAL foundation, external owner-only secret store, typed
  lifecycle journal, crash reconciliation, and backup boundary. Issue #63 —
  onboarding core implements direct probing and lifecycle orchestration. Issue
  #64 — runtime fleet and issue #65 — protected onboarding API add dynamic
  runtime activation, shared lifecycle/actuator admission, and protected routes
  without adding a dashboard.
- ADRs 0003–0005 accept exact qualification, non-actuating Moonraker upload and
  durable at-most-once typed print start as separate internal components.
- ADR 0007 accepts their one authenticated exact-printer ingress contract.
  Issue #75 — dispatch coordinator implements its durable composition and
  issue #76 — native dispatch proof proves the lifecycle against pinned real
  Moonraker.
- [Issue #12 — end-to-end dispatch](https://github.com/tomlawesome/klove/issues/12)
  completes that integration and satisfies this phase's exit criterion.
- Start with single-plate, single-extruder, no-MMU G-code.

Exit: one canonically registered target-tagged job can be queued, started,
paused, resumed, cancelled, completed, and reconciled without duplicate starts.

### Phase 4: current-Grove compatibility bridge

- Accept ADR 0006's Klove-owned runtime registry, embedded setup/recovery
  surface, strict completion message, and minimal Grove `KLOVE` type boundary.
- Apply ADR 0008's accepted clean-room compatibility and independent visual
  provenance boundary.
- Use the Phase 3 runtime registry for the product onboarding path; do not add a
  second UI registry or return to per-printer TOML.
- Add the minimal conservative MQTT/TLS state/control facade and bounded FTPS
  spool, using only operations already accepted by their own ADRs.
- Build the independently styled embedded setup/recovery flow and propose the
  tiny `KLOVE` Add Printer contribution upstream through a short-lived fork.

Exit: an authorized Grove user can onboard and monitor one exact Klove printer
without entering Moonraker credentials into Grove or editing per-printer TOML,
and can safely dispatch only through accepted Klove contracts. Unsupported
commands are hidden, fail visibly if sent, and never reach a generic G-code
path.

### Phase 5: bounded live controls

- Separately decide and test temperature/speed and explicitly mapped fan/light
  operations. Keep jog and extrusion disabled until separately proven.

Exit: every enabled control has a current, exact capability and state proof,
bounded typed parameters, idempotency, reconciliation, and complete negative
tests.

### Phase 6: fleet hardening

- ADR 0011 freezes the registry-only transactional migration contract and exact
  historical-schema harness before any fleet schema extension. Profile and
  mapping history, cross-service fence references, and operational
  backup/restore remain separate ordered slices.
- ADR 0012 accepts schema version 2's append-only, directly evidenced
  capability-mapping and safety-profile history. History remains audit evidence
  and never becomes current authorization.
- Multi-printer routing, per-printer credentials/policies, job journal, cameras
  via external URLs, metrics, backups, migration tests, and upgrade/rollback.
- Docker Compose examples for bridge and host-network constraints.

Exit: fault-injection and soak tests cover simultaneous printers, Klove/Grove/
Moonraker restarts, lost acknowledgements, corrupt uploads, stale config, and
credential rejection.

### Later adapters

- Exclude-object support, richer camera integration, explicitly supported
  MMU/toolchanger adapters, optional outbound host agent, and other printer
  stacks behind new southbound adapters.

## Validation strategy

The automated integration lane runs the production Klove image against pinned
real Klipper and Moonraker processes with Klipper's Linux-process MCU. It tests
authentication, status/control semantics, ambiguity, and restarts without
hardware, host ports, devices, or privilege. It is not RatOS. The exact RatOS
v2.1.0 ARM disk release, host services, configured macros, and physical effects
remain a separate attended acceptance lane; full-system emulation may add
evidence only if it faithfully boots the immutable release and does not weaken
confinement.

- Unit and property tests for state reduction, command policy, limit changes,
  duplicate delivery, and every state-machine transition.
- Fuzz MQTT/JSON, URL, filename, ZIP/3MF, metadata, and G-code-header parsers.
- Contract tests that run Grove's real MQTT client against Klove and Klove
  against a deterministic fake Moonraker WebSocket/HTTP server.
- A native real-process Klipper/Moonraker integration test for authentication,
  typed controls, lost responses, exact single dispatch, and process restarts.
- Issue #12 and issue #76 integration tests cover authorized
  intake/upload/start, metadata delays, lost acknowledgements, history
  reconciliation, cancellation, completion, substitution, restart and
  concurrent printers; component ADRs do not authorize a public workflow by
  themselves.
- Scripted browser tests for the real Grove parent/Klove frame handshake,
  independent Klove owner authentication, CSRF and exact-origin rejection,
  strict completion decoding, cancellation, responsive/accessibility behavior,
  privacy boundaries, `KLOVE` feature suppression, exact-printer queueing, and
  regression of existing Bambu types.
- Hardware-in-the-loop release-candidate tests on a dedicated printer with a
  known safe low-risk file. Heating, motion, and cancellation tests require an
  attended checklist and must never be part of routine CI.

Release only after negative tests demonstrate that mismatched G-code, unknown
macros, over-temperature requests, unhomed/out-of-bounds jogs, duplicate starts,
unauthorized Moonraker access, corrupt archives, and stale safety profiles all
fail closed.

## Immediate next slices

The canonical registry, protected onboarding API, embedded setup/recovery
surface, dispatch coordinator, and native intake-through-completion proof are
complete. The remaining current-Grove bridge order is:

1. obtain exact ADR-0008-compliant Grove wire evidence and accept the bounded
   MQTT/TLS and FTPS contracts under issue #10 — MQTT facade and issue #14 —
   FTPS ingress;
2. implement those adapters without adding another upload, start, or control
   path;
3. prepare issue #60 — minimal Grove contribution against the pinned supported
   Grove revision; and
4. publish issue #13 — deployment and operations guidance.

Issue #51 — RatOS virtual-MCU proof remains a separate supplemental acceptance
lane and does not weaken or replace supported-hardware release acceptance.
Track the full order under
[#32 — Grove bridge](https://github.com/tomlawesome/klove/issues/32) and
[programme roadmap #38](https://github.com/tomlawesome/klove/issues/38).

## Primary references

- [Moonraker architecture and API overview](https://moonraker.readthedocs.io/en/latest/)
- [Moonraker external API introduction](https://moonraker.readthedocs.io/en/latest/external_api/introduction/)
- [Moonraker printer administration](https://moonraker.readthedocs.io/en/latest/external_api/printer/)
- [Moonraker file management](https://moonraker.readthedocs.io/en/latest/external_api/file_manager/)
- [Moonraker authentication](https://moonraker.readthedocs.io/en/latest/external_api/authorization/)
- [Moonraker notifications](https://moonraker.readthedocs.io/en/latest/external_api/jsonrpc_notifications/)
- [Moonraker webcam management](https://moonraker.readthedocs.io/en/latest/external_api/webcams/)
- [Klipper API server](https://www.klipper3d.org/API_Server.html)
- [Klipper status reference](https://www.klipper3d.org/Status_Reference.html)
- [Mainsail overview](https://docs.mainsail.xyz/)
- [Grove Control repository](https://github.com/EdwardChamberlain/grove-control)
- [3MF Core Specification 1.3.0](https://3mf.io/wp-content/uploads/sites/106/2025/02/3MF_Core_Specification_v1.3.0.pdf)
- [Python `zipfile` documentation](https://docs.python.org/3/library/zipfile.html)
- [Bambu Studio 3MF implementation](https://github.com/bambulab/BambuStudio/blob/master/src/libslic3r/Format/bbs_3mf.cpp)
