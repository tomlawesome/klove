#!/usr/bin/env sh
set -eu

if [ ! -d node_modules/@playwright/test ]; then
  echo "Install pinned browser-test dependencies first: npm ci" >&2
  exit 1
fi

# A test whose title ends in @quarantine leaves the gate; the flake-check CI job
# lists those titles from the sources, so the skip and the flake log cannot drift.
exec npx playwright test --config=playwright.config.mjs --grep-invert @quarantine
