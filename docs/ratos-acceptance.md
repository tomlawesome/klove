# RatOS v2.1.0 acceptance lane

Status: supplemental exact-release emulation executed; conclusive acceptance
still requires supported ARM hardware under the attended procedure below.
Implementation and evidence are tracked in
[issue #50](https://github.com/tomlawesome/klove/issues/50).

RatOS acceptance is deliberately separate from Klove's native
Klipper/Moonraker container test. The automated fixture proves the current
HTTP/WebSocket authentication, observation, typed control, ambiguity, and
reconnect contracts against real upstream processes. It does not boot or claim
to emulate RatOS.

## Immutable release inputs

Use only the official [RatOS v2.1.0 release](https://github.com/Rat-OS/RatOS/releases/tag/v2.1.0).
The following compressed release asset names, byte sizes, and SHA-256 digests
were recorded from the official GitHub release metadata:

| Target | Asset | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| BIGTREETECH CB1 | `2026-03-04-RatOS-2.1.0-armbian-CB1.img.xz` | 1,907,469,676 | `f481fbfecc22fa9b31865a24564fcea45ac36cdab0141822e825efbeb1bcb8b8` |
| Raspberry Pi (32-bit) | `2026-03-04-RatOS-2.1.0-raspberry-rpi32.img.xz` | 2,125,243,800 | `513465cf6b233d73e5c9ed048568493894d77c7c67e93586609f9b7fbc182153` |

Reject a size or digest mismatch. Never silently substitute a later tag,
rolling image, rebuilt archive, or another board image.

## Virtualisation feasibility boundary

These are board-specific ARM whole-disk images, not OCI images. Extracting
their userspace into a container would omit the board kernel and device tree,
systemd service graph, udev, and hardware integration that make the release
RatOS; it does not count as a RatOS test.

The rootless full-system experiment is tracked in
[issue #50](https://github.com/tomlawesome/klove/issues/50) and is acceptable
only if it:

- verifies and boots the exact release image without modifying its contents;
- uses a sufficiently faithful emulated target board and needs no privileged
  container, host device, or protection bypass;
- reaches the real RatOS-managed Klipper and Moonraker services and records all
  emulation limitations; and
- remains supplemental evidence rather than closing physical acceptance.

Do not weaken rootless confinement or install an unreviewed emulator merely to
label the native integration fixture “RatOS.”

## Executed Raspberry Pi 3B emulation

On 2026-08-14, the x86-64 rootless host executed the opt-in lane under
`tests/integration/ratos-emulation/`. It used the exact verified Raspberry Pi
asset above as an immutable raw backing disk and QEMU's `raspi3b` model under
TCG. The matching release `kernel8` payload and Pi 3B device tree were extracted
read-only, hashed, and direct-loaded; all guest writes went to a fresh,
derived qcow2 overlay.

The proof reached and queried the running RatOS-managed services through
loopback forwards inside a networkless outer container:

| Evidence | Observed identity/state |
| --- | --- |
| RatOS | `2.1.0`, Raspbian GNU/Linux 11 Bullseye |
| Emulated board/kernel | Raspberry Pi 3 Model B, `6.1.21-v8+` |
| Moonraker | `v0.9.1-0-g63578ae`, API `1.4.0` |
| SSH | OpenSSH `8.4p1 Raspbian-5+deb11u5` |
| Managed services | Klipper, Moonraker, and RatOS Configurator active/running |
| Klippy | not ready; exploratory runs observed `startup` or `disconnected`, and `/printer/info` remained unavailable without a usable printer configuration/MCU |

The guest also exhibited expected emulation-only limitations around Raspberry
Pi EEPROM, GPIO/firmware interfaces, and camera hardware. A raw firmware-style
boot produced no usable console or write evidence; QEMU's Pi model required
direct-loading the exact kernel and DTB. That bypasses the physical Raspberry
Pi firmware boot chain, so this is exact release-kernel and immutable-base-image
service evidence—not a complete whole-disk boot claim. No CB1 experiment is
planned because upstream QEMU has no matching Allwinner H616/CB1 machine.

Run `scripts/test-ratos-emulation.sh` with the official asset path to reproduce
the bounded lifecycle. It verifies all release identities, records the exact
local tool-image and rootless daemon, permits only one evidenced first-boot
restart, probes non-secret system/service identities, removes the exact
container, and checks and archives the fresh run's derived COW disk with its
digest. Each run also archives its prepared-input identities, tool-image/daemon
origin, Git revision, and deterministic lane-source digest. Probe success uses
a separate atomic marker rather than being inferred from evidence-file
presence. Each run starts from a new overlay; no prior mutable guest state is
reused. The tool image uses a fixed Debian package snapshot. Raw guest serial
output is not retained. The lane is deliberately excluded from routine CI
because the download and TCG boot are large and slow.

This result does not exercise Klove's job-control contract. A faithful virtual
printer still needs an explicitly controlled Klipper configuration and virtual
MCU without modifying or misrepresenting the golden release. That work is
tracked in
[spike #51](https://github.com/tomlawesome/klove/issues/51). Physical RatOS,
configured macro semantics, board peripherals, timing, USB MCU behavior, and
safe printer action remain subject to the attended procedure. QEMU's TCG
[security policy](https://www.qemu.org/docs/master/system/security.html) also
provides no supported guest-isolation guarantee; the rootless, capability-free
outer container remains the host confinement boundary. The relevant upstream
board limitations are recorded in QEMU's
[Raspberry Pi machine documentation](https://www.qemu.org/docs/master/system/arm/raspi.html).

## Attended hardware procedure

1. Select the exact release asset for a supported dedicated ARM board. Record
   board model/revision and storage identity.
2. Download from the official release, verify compressed byte size and SHA-256,
   then image dedicated removable media. Preserve the downloaded asset or its
   immutable retrieval evidence until acceptance is recorded.
3. Boot the board on an isolated test network. Record the RatOS, kernel,
   Klipper, and Moonraker versions/revisions reported by the running system.
4. Install a controlled `printer.cfg`. Record its SHA-256 and separately review
   the exact effective `PAUSE`, `RESUME`, and `CANCEL_PRINT` definitions.
   Printer owners remain responsible for those macro semantics.
5. Give Klove a dedicated least-privilege Moonraker credential through its
   secret file. Do not trust an entire Grove subnet and do not record the
   credential in evidence.
6. Use an attended, known low-risk virtual-SD job prepared outside Klove. Klove
   must not upload, start, or send generic G-code in this acceptance slice.
7. Verify authenticated observation, exact job identity, pause, resume, and
   cancel. For each operation record one dispatch and later same-job state
   evidence.
8. Verify invalid credentials, stale tokens, duplicate idempotency keys, and a
   controlled lost-response case fail closed. After any request may have
   dispatched, require `outcome_unknown` and never issue a blind retry.
9. Restart Moonraker/Klipper and Klove separately. Verify re-discovery,
   capability probing, a changed boot-scoped token after Klove restart, and no
   dispatch from an old token.
10. Stop immediately on unexpected motion, heating, macro behavior, job
    substitution, credential exposure, or ambiguous evidence. Correct the
    printer configuration and repeat the entire acceptance; do not waive the
    failure.

## Evidence record

Record the following in the relevant GitHub issue without secrets or raw
private configuration:

```text
Date/operator:
RatOS tag, asset name, byte size, SHA-256:
Board model/revision and storage:
Kernel, RatOS, Klipper, Moonraker identities:
Klove commit and immutable container digest:
printer.cfg SHA-256:
PAUSE/RESUME/CANCEL_PRINT review result:
Network/authentication boundary:
Positive control results and dispatch counts:
Negative, ambiguity, and restart results:
Unexpected behavior:
Final pass/fail and follow-up issues:
```

Only a complete pass on the exact release, board, configuration, Klove commit,
and container digest is evidence for release-candidate acceptance. A change to
any of those identities requires proportional re-acceptance.
