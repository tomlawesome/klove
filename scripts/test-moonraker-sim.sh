#!/usr/bin/env sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
run_id=${KLOVE_SIM_RUN_ID:-local-$(date -u +%Y%m%d%H%M%S)-$$}

cleanup() {
    "$repo_root/scripts/moonraker-sim-down.sh" "$run_id" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

"$repo_root/scripts/moonraker-sim-up.sh" "$run_id"
"$repo_root/scripts/moonraker-sim-test.sh" "$run_id"
"$repo_root/scripts/moonraker-sim-down.sh" "$run_id"
trap - EXIT HUP INT TERM
