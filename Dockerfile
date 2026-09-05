# syntax=docker/dockerfile:1.7
FROM python:3.12-alpine3.23@sha256:31a768b01976652c222e318fe5bd6e7c252f056cbf489c88fa256f1bf0af58e3 AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY requirements-build.lock requirements.lock pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --require-hashes -r requirements-build.lock \
    && python -m pip install --require-hashes --prefix=/install -r requirements.lock \
    && python -m pip install --no-build-isolation --no-deps --prefix=/install .

FROM python:3.12-alpine3.23@sha256:31a768b01976652c222e318fe5bd6e7c252f056cbf489c88fa256f1bf0af58e3

ARG VCS_REF=unknown
ARG SOURCE_BRANCH=unknown
ARG SOURCE_DIGEST=unknown
LABEL org.opencontainers.image.source="https://github.com/tomlawesome/klove" \
    org.opencontainers.image.revision="${VCS_REF}" \
    io.github.tomlawesome.klove.source-branch="${SOURCE_BRANCH}" \
    io.github.tomlawesome.klove.source-digest="${SOURCE_DIGEST}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN apk add --no-cache --upgrade sqlite-libs=3.53.4-r0 \
    && addgroup --system --gid 10001 klove \
    && adduser --system --disabled-password --no-create-home --uid 10001 \
        --ingroup klove --shell /sbin/nologin klove \
    && install -d -o 10001 -g 10001 -m 0700 /var/lib/klove \
    && install -d -o 10001 -g 10001 -m 0700 /var/lib/klove/registry-secrets \
    && install -d -o 10001 -g 10001 -m 0700 /var/lib/klove/ftps-staging \
    && install -d -o 10001 -g 10001 -m 0700 /run/klove-secrets
COPY --from=build /install /usr/local
USER 10001:10001
EXPOSE 8080/tcp 990/tcp 50000-50009/tcp
ENTRYPOINT ["klove"]
CMD ["--config", "/etc/klove/config.toml"]
