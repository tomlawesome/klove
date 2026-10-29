#!/usr/bin/env sh
set -eu

python -m ruff format --check .
python -m ruff check .
python -m mypy src tests
# Quarantined tests (scripts/quarantine.txt) leave the gate here; the flake-check
# CI job reads the same file, so the skip and the flake log cannot drift.
set --
while IFS= read -r node; do
    case $node in '' | '#'*) continue ;; esac
    set -- "$@" --deselect "$node"
done < "$(dirname -- "$0")/quarantine.txt"

python -m pytest --cov=klove --cov-branch --cov-report=term-missing --cov-report=json -q "$@"
