# RatOS v2.1.0 acceptance lane

Status: procedure defined; execution requires supported ARM hardware

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

These are board-specific ARM whole-disk images, not OCI images. The current
x86-64 rootless host has no registered ARM binary-format emulator or QEMU
system emulator. Extracting their userspace into a container would omit the
bootloader, board kernel and device tree, systemd service graph, udev, and
hardware integration that make the release RatOS; it does not count as a RatOS
test.

A later full-system emulation experiment is acceptable if it:

- verifies and boots the exact release image without modifying its contents;
- uses a sufficiently faithful emulated target board and needs no privileged
  container, host device, or protection bypass;
- reaches the real RatOS-managed Klipper and Moonraker services and records all
  emulation limitations; and
- remains supplemental evidence rather than closing physical acceptance.

Do not weaken rootless confinement or install an unreviewed emulator merely to
label the native integration fixture “RatOS.”

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
