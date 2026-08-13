# syntax=docker/dockerfile:1.7
FROM --platform=linux/amd64 python:3.12.13-slim-bookworm@sha256:6e13e65c55e33adf203d77ee371cf8bf5d81bd4902ef07565721f46bf44917af AS build

ARG KLIPPER_COMMIT=fe4eb8650bd7de4c2100a14eaf09b0965c430e29
ARG MOONRAKER_COMMIT=d5ee17128bb88434aacdab90c2e9e990e2b64e4a
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates=20250419~deb12u1 \
        gcc=4:12.2.0-3 \
        git=1:2.39.5-0+deb12u3 \
        libc6-dev=2.36-9+deb12u14 \
        libffi-dev=3.4.4-1 \
        libsodium-dev=1.0.18-1+deb12u1 \
        make=4.3-4.1 \
    && rm -rf /var/lib/apt/lists/*

RUN git init /opt/klipper \
    && git -C /opt/klipper remote add origin https://github.com/Klipper3d/klipper.git \
    && git -C /opt/klipper fetch --depth 1 origin "${KLIPPER_COMMIT}" \
    && git -C /opt/klipper checkout --detach FETCH_HEAD \
    && test "$(git -C /opt/klipper rev-parse HEAD)" = "${KLIPPER_COMMIT}" \
    && git init /opt/moonraker \
    && git -C /opt/moonraker remote add origin https://github.com/Arksine/moonraker.git \
    && git -C /opt/moonraker fetch --depth 1 origin "${MOONRAKER_COMMIT}" \
    && git -C /opt/moonraker checkout --detach FETCH_HEAD \
    && test "$(git -C /opt/moonraker rev-parse HEAD)" = "${MOONRAKER_COMMIT}"

COPY requirements-python-build.lock requirements-klipper.lock requirements-moonraker.lock /locks/
RUN python -m venv /opt/klipper-env \
    && /opt/klipper-env/bin/pip install --require-hashes -r /locks/requirements-python-build.lock \
    && /opt/klipper-env/bin/pip install --require-hashes --no-build-isolation \
        -r /locks/requirements-klipper.lock \
    && python -m venv /opt/moonraker-env \
    && /opt/moonraker-env/bin/pip install --require-hashes -r /locks/requirements-python-build.lock \
    && /opt/moonraker-env/bin/pip install --require-hashes --no-build-isolation \
        -r /locks/requirements-moonraker.lock

COPY klipper.config /fixture/klipper.config
RUN cp /fixture/klipper.config /opt/klipper/.config \
    && make -C /opt/klipper olddefconfig \
    && make -C /opt/klipper -j2 \
    && PYTHONPATH=/opt/klipper/klippy /opt/klipper-env/bin/python -c \
        'import chelper; chelper.get_ffi()' \
    && printf '%s\n' "${KLIPPER_COMMIT}" > /opt/klipper/klippy/.version \
    && printf '%s\n' "${MOONRAKER_COMMIT}" > /opt/moonraker/moonraker/.version \
    && rm -rf /opt/klipper/.git /opt/moonraker/.git

COPY fixture /fixture
COPY klove/config.toml /fixture/klove-config.toml
COPY SHA256SUMS /fixture/SHA256SUMS
RUN cd / && sha256sum --check /fixture/SHA256SUMS

FROM --platform=linux/amd64 python:3.12.13-slim-bookworm@sha256:6e13e65c55e33adf203d77ee371cf8bf5d81bd4902ef07565721f46bf44917af

ARG KLIPPER_COMMIT=fe4eb8650bd7de4c2100a14eaf09b0965c430e29
ARG MOONRAKER_COMMIT=d5ee17128bb88434aacdab90c2e9e990e2b64e4a
LABEL org.opencontainers.image.source="https://github.com/tomlawesome/klove" \
    io.github.tomlawesome.klove.integration.klipper-revision="${KLIPPER_COMMIT}" \
    io.github.tomlawesome.klove.integration.moonraker-revision="${MOONRAKER_COMMIT}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        libjpeg62-turbo=1:2.1.5-2 \
        libopenjp2-7=2.5.0-2+deb12u3 \
        libsodium23=1.0.18-1+deb12u1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 klove-sim \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin klove-sim \
    && install -d -o 10001 -g 10001 -m 0700 \
        /run/klove-secrets /run/printer-state /run/test-state

COPY --from=build --chown=10001:10001 /opt/klipper /opt/klipper
COPY --from=build --chown=10001:10001 /opt/klipper-env /opt/klipper-env
COPY --from=build --chown=10001:10001 /opt/moonraker /opt/moonraker
COPY --from=build --chown=10001:10001 /opt/moonraker-env /opt/moonraker-env
COPY --from=build --chown=10001:10001 /fixture /fixture

USER 10001:10001
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "/fixture/host.py"]
