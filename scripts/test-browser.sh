#!/usr/bin/env sh
set -eu

if [ ! -d node_modules/@playwright/test ]; then
  echo "Install pinned browser-test dependencies first: npm ci" >&2
  exit 1
fi

exec npx playwright test --config=playwright.config.mjs
