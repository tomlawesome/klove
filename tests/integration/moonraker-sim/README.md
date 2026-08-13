# Native Klipper/Moonraker integration fixture

This fixture runs Klove against real, revision-pinned Klipper and Moonraker
processes. It is an automated protocol and orchestration test, not a printer
deployment and not RatOS.

## Pinned components

| Component | Immutable identity |
| --- | --- |
| Klipper | `fe4eb8650bd7de4c2100a14eaf09b0965c430e29` |
| Moonraker | `d5ee17128bb88434aacdab90c2e9e990e2b64e4a` |
| Python base | `python:3.12.13-slim-bookworm@sha256:6e13e65c55e33adf203d77ee371cf8bf5d81bd4902ef07565721f46bf44917af` |

The Docker build fetches each exact upstream commit, verifies `HEAD`, installs
only hash-pinned Python dependencies, and verifies the controlled fixture files
against `SHA256SUMS`. See `THIRD_PARTY_NOTICES.md` for source and licence
information. The integration image is local test infrastructure and must not be
published.

Klipper uses its real Linux-process MCU with `kinematics: none`. The fixture has
no heaters, motors, GPIO, USB devices, or generic command interface. Its
controlled virtual-SD file consists only of finite dwell commands.

## Run the contract

The full lifecycle is:

```console
KLOVE_SIM_RUN_ID=local-contract scripts/test-moonraker-sim.sh
```

`KLOVE_SIM_RUN_ID` is optional. The wrapper otherwise generates a unique valid
identifier. A manual lifecycle is also available:

```console
scripts/moonraker-sim-up.sh local-contract
scripts/moonraker-sim-test.sh local-contract
scripts/moonraker-sim-down.sh local-contract
```

Local execution requires rootless Docker and the coreutils `timeout` command.
The scripts record the exact Compose project, Docker context, daemon ID, and
rootless mode in ignored state. Test and teardown refuse a different context or
daemon and remove only that exact project's containers, network, and volumes.
Every Docker operation has a finite outer timeout. Hosted CI may use its
rootful daemon only when both `CI=true` and
`KLOVE_SIM_ALLOW_ROOTFUL_CI=1` are set by the dedicated least-privilege job.

Every service runs non-root with a read-only root filesystem, all capabilities
dropped, `no-new-privileges`, finite CPU/memory/PID limits, and an internal-only
Compose network with no published port, host network, device, or Docker socket.
Runtime credentials are generated inside the stack, mounted only where needed,
and removed with the exact project. Moonraker trusts only its own loopback;
Klove must authenticate with the generated API key.

## Contract boundary

The isolated contract runner starts a benign virtual-SD print, after Moonraker
initialization, to establish a controllable state with a coherent history row.
That private preparation is not a Klove capability: production code still
contains no upload, print-start, or generic G-code path.

The contract verifies:

- invalid Klove bearer and Moonraker API keys are rejected;
- real WebSocket discovery and monitoring observe the exact test job;
- typed pause, resume, and cancel bind Moonraker's unique history job id and
  start time, each dispatch once, and reconcile to later same-job evidence;
- a duplicate idempotency key reuses its result and a stale token never
  dispatches;
- a deliberately dropped post-dispatch response yields `outcome_unknown`,
  remains fenced across keys, and never causes a retry;
- a printer-host restart preserves that uncertainty fence while Klove stays
  running; and
- a Klove restart creates a new boot epoch, so the prior token is denied
  without dispatch.

RatOS host, kernel, systemd, udev, configurator, board, and physical macro
semantics are outside this fixture. Follow `docs/ratos-acceptance.md` for that
separate lane.
