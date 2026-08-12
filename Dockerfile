# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY requirements-build.lock requirements.lock pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --require-hashes -r requirements-build.lock \
    && python -m pip install --require-hashes --prefix=/install -r requirements.lock \
    && python -m pip install --no-build-isolation --no-deps --prefix=/install .

FROM python:3.12-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2

ARG VCS_REF=unknown
ARG SOURCE_BRANCH=unknown
LABEL org.opencontainers.image.source="https://github.com/tomlawesome/klove" \
    org.opencontainers.image.revision="${VCS_REF}" \
    io.github.tomlawesome.klove.source-branch="${SOURCE_BRANCH}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN groupadd --system --gid 10001 klove \
    && useradd --system --uid 10001 --gid klove --home-dir /nonexistent --shell /usr/sbin/nologin klove
COPY --from=build /install /usr/local
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["klove"]
CMD ["--config", "/etc/klove/config.toml"]
