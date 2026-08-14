# Supplemental RatOS v2.1.0 full-system emulation

This opt-in Linux lane boots the exact verified RatOS v2.1.0 Raspberry Pi
release kernel, device tree, and raw base image under QEMU's `raspi3b` machine.
The running filesystem is that immutable base plus one fresh, derived COW disk.
It is real RatOS userspace and service evidence, but it is not a
physical-printer acceptance test and does not emulate a printer MCU.

The lane is deliberately absent from routine CI. The official archive is about
2.1 GB, its raw disk is about 8.4 GB, and ARM execution on the x86-64 development
host uses slow TCG emulation. The scripts publish no image or port.

## Exact inputs

| Input | Immutable identity |
| --- | --- |
| RatOS release | `v2.1.0` |
| Asset | `2026-03-04-RatOS-2.1.0-raspberry-rpi32.img.xz` |
| Asset bytes | `2125243800` |
| Asset SHA-256 | `513465cf6b233d73e5c9ed048568493894d77c7c67e93586609f9b7fbc182153` |
| Expanded raw SHA-256 | `63e95ea4f7a362a1fa9adf210d8f069c671341fb8531a4595f35e2ff6e4a1f51` |
| Kernel payload SHA-256 | `6cc7aad62b32a1efa774955edc10ea302ae015a58abbea683c23287839b097f8` |
| Pi 3B DTB SHA-256 | `83eefb232fe362a75a8cced8d3f5f17af4398e1d121d486e45eff8937a86b0a1` |
| Tool base | `python:3.12.13-slim-bookworm@sha256:6e13e65c55e33adf203d77ee371cf8bf5d81bd4902ef07565721f46bf44917af` |
| Debian package snapshot | `20260803T000000Z` |
| QEMU packages | Debian `1:7.2+dfsg-7+deb12u18+b3` |

`ratos-emulation-prepare.sh` rejects every size or digest mismatch, tests the
XZ stream, preserves the expanded raw disk read-only, extracts and verifies the
matching release kernel/DTB, and records the exact local tool-image ID and
rootless Docker daemon identity. It also records both Git `HEAD` and a
deterministic digest of every executable lane input, so an uncommitted build is
identified without being misrepresented as the clean revision. Package
installation uses the fixed official Debian snapshot recorded above. Each
evidence run creates a new private 16 GiB sparse qcow2 overlay backed by the raw
disk; it never reuses a prior guest's mutable state.

The host must be x86-64 Linux with rootless Docker, GNU coreutils, and the
commands preflighted by `ratos-emulation-lib.sh`. A fresh preparation requires
at least 16 GB free for the staged archive, expanded raw disk, and derived run
state. TCG emulation is intentionally slow.

## Run

Download only the named asset from the official release, then pass its path to
the lifecycle wrapper:

```console
gh release download v2.1.0 --repo Rat-OS/RatOS \
  --pattern 2026-03-04-RatOS-2.1.0-raspberry-rpi32.img.xz \
  --dir /private/staging
scripts/test-ratos-emulation.sh \
  /private/staging/2026-03-04-RatOS-2.1.0-raspberry-rpi32.img.xz
```

The manual lifecycle is:

```console
scripts/ratos-emulation-prepare.sh /private/staging/2026-03-04-RatOS-2.1.0-raspberry-rpi32.img.xz
scripts/ratos-emulation-up.sh
scripts/ratos-emulation-probe.sh
scripts/ratos-emulation-down.sh
```

The first fresh RatOS overlay expands its root partition and requests a reboot.
Because QEMU runs with `-no-reboot`, the probe accepts and performs exactly one
restart only after a clean container exit and both expected console markers.
Any second exit, non-zero exit, missing marker, timeout, or malformed response
fails. Every later evidence run repeats first boot on a new overlay.
First boot has a fixed ten-minute deadline; post-reboot service readiness has a
separate fixed fifteen-minute deadline for slow TCG hosts.

The probe succeeds only after an OpenSSH banner and RatOS nginx's proxied
`/server/info` and `/machine/system_info` endpoints return coherent identities.
It writes a bounded, non-secret `probe.json`, then atomically records success
only after emitting that evidence successfully. Teardown is bound to the
recorded daemon, image ID, state path, container name, and labels; it removes
only that container, rechecks the overlay, and retains the derived COW digest,
per-run origin and exact-input identities, and bounded lifecycle/probe evidence
under `.klove-integration/ratos-v2.1.0/evidence/`. Raw serial output is neither
copied nor retained because the guest controls its content and volume.

## Confinement and limits

The outer container requires rootless Docker and uses no host or container
network, published port, device, Docker socket, privilege, or Linux capability.
Its root filesystem is read-only, `no-new-privileges` is set, and CPU, memory,
PID, and temporary-filesystem limits are finite. QEMU user networking is
`restrict=on`; its three forwards bind only to loopback *inside* that otherwise
networkless container. The guest receives no Internet route.

The exact raw disk, kernel, and DTB are separate read-only mounts. The
unprivileged QEMU process receives only the fresh COW file writable. The local
preparation helper has write access to the private state tree solely while it
expands and verifies the user-supplied release; every state directory and
lifecycle marker is rejected unless it is a real, current-user-owned path with
the required private mode.

QEMU's Pi model does not implement the physical Raspberry Pi firmware path well
enough to boot this disk unaided. The lane therefore direct-loads the exact
release `kernel8` payload and Pi 3B DTB, bypassing the physical firmware boot
chain. It passes only console/root, standard in-image USB-network module, and
watchdog-device arguments; it neither edits the immutable image nor injects a
printer configuration.

This proves that the exact release kernel and base image can reach the expected
RatOS, Moonraker, Klipper-process, and configurator identities in the emulated
environment. It does not prove GPIO, EEPROM, camera, USB MCU, timing, boot
firmware, macro semantics, job control, or physical safety. QEMU's
[security policy](https://www.qemu.org/docs/master/system/security.html) does
not treat TCG guests as a supported isolation boundary; the rootless outer
container is the relevant host confinement boundary. QEMU documents the
limited Raspberry Pi models in its
[Arm board guide](https://www.qemu.org/docs/master/system/arm/raspi.html).
Follow `docs/ratos-acceptance.md` for attended hardware acceptance and
[issue #51](https://github.com/tomlawesome/klove/issues/51) for the controlled
virtual-MCU/Klove-contract follow-up.
