# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
FROM python:3.12.13-slim-bookworm@sha256:6e13e65c55e33adf203d77ee371cf8bf5d81bd4902ef07565721f46bf44917af

ARG KLOVE_SOURCE_REVISION=unknown
ARG KLOVE_SOURCE_DIGEST=unknown

COPY tests/integration/ratos-emulation/debian.sources /etc/apt/sources.list.d/debian.sources

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        mtools=4.0.33-1+really4.0.32-1 \
        qemu-system-arm=1:7.2+dfsg-7+deb12u18+b3 \
        qemu-utils=1:7.2+dfsg-7+deb12u18+b3 \
        xz-utils=5.4.1-1+deb12u1 \
    && rm -rf /var/lib/apt/lists/*

COPY --chmod=0555 tests/integration/ratos-emulation/tool.py /opt/klove-ratos/tool.py
COPY --chmod=0555 tests/integration/moonraker-sim/fixture/proxy.py \
    /opt/klove-ratos/contract/proxy.py
COPY --chmod=0555 tests/integration/moonraker-sim/fixture/exercise_contract.py \
    /opt/klove-ratos/contract/exercise_contract.py
COPY --chmod=0555 tests/integration/ratos-emulation/contract/ratos_exercise_contract.py \
    /opt/klove-ratos/contract/ratos_exercise_contract.py
COPY --chmod=0444 tests/integration/moonraker-sim/fixture/contract.gcode \
    /opt/klove-ratos/contract/contract.gcode
COPY --chmod=0444 tests/integration/ratos-emulation/contract/printer.cfg \
    /opt/klove-ratos/contract/printer.cfg
COPY --chmod=0444 tests/integration/ratos-emulation/contract/klove.toml \
    /opt/klove-ratos/contract/klove.toml
COPY --chmod=0444 tests/integration/ratos-emulation/SHA256SUMS \
    /opt/klove-ratos/contract/SHA256SUMS

RUN install -d -m 0555 /inputs /run-state \
    && install -d -o 10001 -g 10001 -m 0700 /run/klove-secrets \
    && install -m 0444 /dev/null \
        /inputs/2026-03-04-RatOS-2.1.0-raspberry-rpi32.img \
    && install -m 0444 /dev/null /inputs/kernel8.Image \
    && install -m 0444 /dev/null /inputs/bcm2710-rpi-3-b.dtb \
    && install -m 0444 /dev/null /run-state/ratos-rpi32-run.qcow2 \
    && /usr/bin/qemu-system-aarch64 -machine help \
        | grep -Eq '(^|[[:space:]])raspi3b([[:space:]]|$)' \
    && /usr/bin/qemu-system-aarch64 --version | grep -F 'QEMU emulator version 7.2.22' \
    && cd / \
    && sha256sum --check /opt/klove-ratos/contract/SHA256SUMS \
    && /usr/local/bin/python -m py_compile /opt/klove-ratos/tool.py

LABEL io.klove.test-purpose="supplemental-ratos-v2.1.0-emulation" \
      io.klove.source-revision="$KLOVE_SOURCE_REVISION" \
      io.klove.source-digest="$KLOVE_SOURCE_DIGEST"

USER 10001:10001
WORKDIR /work
