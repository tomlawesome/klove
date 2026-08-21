# Klove architecture reference

Status: accepted  
Research date: 2026-08-12  
Grove Control source reviewed at commit `cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4`.

This index replaces the former combined architecture and delivery plan. It
keeps the architecture narrative in focused references; accepted ADRs and the
implementation roadmap remain the canonical detailed contracts and status.

## Architecture references

- [Product boundary and architecture](architecture/product-boundary.md) — the
  deployment choice, northbound boundaries, and package shape.
- [Safety and command policy](architecture/safety.md) — proof-based admission,
  typed control, and validation expectations.
- [Grove compatibility and onboarding](architecture/grove.md) — the conservative
  `KLOVE` type, facade, and embedded setup/recovery boundary.
- [Moonraker and Klipper integration](architecture/moonraker.md) — runtime
  sessions, capability discovery, and canonical state mapping.
- [Artifact and dispatch safety](architecture/artifacts.md) — the hostile-artifact
  boundary and its separately accepted upload/start components.
- [Runtime, persistence, and operations](architecture/runtime.md) — the
  canonical registry, reconciliation, and operational constraints.
- [Deployment, delivery, and validation](architecture/deployment.md) — delivery
  phases, release/testing strategy, current dependency order, and references.

## Canonical decisions and implementation status

- [ADR 0001: typed job control](decisions/0001-typed-job-control.md)
- [ADR 0002: headless automation boundary](decisions/0002-headless-automation-boundary.md)
- [ADR 0003: exact artifact target qualification](decisions/0003-exact-artifact-target-qualification.md)
- [ADR 0004: bounded Moonraker upload](decisions/0004-bounded-moonraker-upload.md)
- [ADR 0005: durable Moonraker print start](decisions/0005-durable-moonraker-print-start.md)
- [ADR 0006: embedded Grove onboarding](decisions/0006-embedded-grove-onboarding.md)
- [ADR 0007: authenticated dispatch ingress](decisions/0007-authenticated-dispatch-ingress.md)
- [ADR 0008: clean-room Grove provenance](decisions/0008-grove-provenance.md)
- [Proposed ADR 0009: Grove MQTT compatibility boundary](decisions/0009-grove-mqtt-compatibility.md)
- [ADR 0010: Grove FTPS artifact staging](decisions/0010-grove-ftps-staging.md)
- [Implementation roadmap](implementation-plan.md)
- [Onboarding core](onboarding-core.md)
- [Registry storage](registry-storage.md)
- [Testing strategy](testing.md)
- [Release process](releasing.md)

Repository delivery decisions:

- Klove is intentionally single-maintainer. Protected branches require pull
  requests and resolved conversations but no approving review.
- Production self-review and administrator bypass remain enabled.
- GitHub Actions defaults to a read-only repository token. Publication jobs
  elevate only the scopes they require.
- Authentication, authorization, translation, and command-safety code starts
  and remains at 100% statement and branch coverage.
- The safety policy is proof-based: any uncertainty, however small, is a denial.
