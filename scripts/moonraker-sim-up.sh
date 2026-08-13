#!/usr/bin/env sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: $0 RUN_ID" >&2
    exit 2
fi

run_id=$1
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$script_dir/moonraker-sim-lib.sh"
moonraker_sim_validate_run_id "$run_id"

repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
compose_file="$repo_root/tests/integration/moonraker-sim/compose.yml"
state_dir="$repo_root/.klove-integration/moonraker-sim/$run_id"
project="klove-mr-$run_id"

if [ -e "$state_dir/project" ]; then
    echo "integration run already exists: $run_id" >&2
    exit 1
fi

moonraker_sim_capture_daemon
case "$sim_daemon_mode" in
    rootless) recorded_daemon_mode=rootless ;;
    rootful)
        if [ "${CI:-}" != "true" ] || [ "${KLOVE_SIM_ALLOW_ROOTFUL_CI:-}" != "1" ]; then
            echo "moonraker simulation requires rootless Docker outside explicit CI" >&2
            exit 1
        fi
        recorded_daemon_mode=rootful-ci
        ;;
esac

umask 077
mkdir -p "$state_dir"
printf '%s\n%s\n%s\n%s\n' \
    "$project" "$sim_context" "$sim_daemon_id" "$recorded_daemon_mode" > "$state_dir/project"

cleanup_failed_start() {
    timeout 30 docker compose --project-name "$project" --file "$compose_file" logs \
        --no-color --tail 200 printer-host moonraker-proxy klove >&2 || true
    if timeout 120 docker compose --project-name "$project" --file "$compose_file" \
        --profile contract down --volumes --remove-orphans; then
        rm -f -- "$state_dir/project"
        rmdir -- "$state_dir" 2>/dev/null || true
    else
        echo "integration cleanup failed; retained recovery state for RUN_ID: $run_id" >&2
    fi
}
trap cleanup_failed_start EXIT HUP INT TERM

KLOVE_SIM_REVISION=$(git -C "$repo_root" rev-parse HEAD)
export KLOVE_SIM_REVISION
timeout 900 docker compose --project-name "$project" --file "$compose_file" up \
    --build --detach --wait printer-host moonraker-proxy klove

trap - EXIT HUP INT TERM
echo "moonraker simulation ready: $run_id"
