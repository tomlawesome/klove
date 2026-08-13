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
state_file="$repo_root/.klove-integration/moonraker-sim/$run_id/project"

if [ ! -f "$state_file" ]; then
    echo "unknown integration run: $run_id" >&2
    exit 1
fi
moonraker_sim_require_origin "$state_file"
project=$stored_project
if [ "$project" != "klove-mr-$run_id" ]; then
    echo "integration state does not match RUN_ID" >&2
    exit 1
fi

show_logs() {
    timeout 30 docker compose --project-name "$project" --file "$compose_file" logs \
        --no-color --tail 200 printer-host moonraker-proxy klove >&2
}

if ! timeout 180 docker compose --project-name "$project" --file "$compose_file" \
    --profile contract run --rm --no-deps contract; then
    show_logs
    exit 1
fi

if ! timeout 60 docker compose --project-name "$project" --file "$compose_file" \
    restart --timeout 10 printer-host; then
    show_logs
    exit 1
fi
if ! timeout 120 docker compose --project-name "$project" --file "$compose_file" \
    up --detach --wait --no-build --no-recreate printer-host moonraker-proxy klove; then
    show_logs
    exit 1
fi
if ! timeout 180 docker compose --project-name "$project" --file "$compose_file" \
    --profile contract run --rm --no-deps \
    --env KLOVE_SIM_EXPECTED_CODE=outcome_unknown reconnect-contract; then
    show_logs
    exit 1
fi

if ! timeout 60 docker compose --project-name "$project" --file "$compose_file" \
    restart --timeout 10 klove; then
    show_logs
    exit 1
fi
if ! timeout 120 docker compose --project-name "$project" --file "$compose_file" \
    up --detach --wait --no-build --no-recreate klove; then
    show_logs
    exit 1
fi
if ! timeout 180 docker compose --project-name "$project" --file "$compose_file" \
    --profile contract run --rm --no-deps \
    --env KLOVE_SIM_EXPECTED_CODE=state_token_mismatch reconnect-contract; then
    show_logs
    exit 1
fi
