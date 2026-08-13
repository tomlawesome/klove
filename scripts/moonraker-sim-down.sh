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
state_file="$state_dir/project"

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

timeout 120 docker compose --project-name "$project" --file "$compose_file" \
    --profile contract down \
    --volumes --remove-orphans
rm -f -- "$state_file"
rmdir -- "$state_dir"
echo "moonraker simulation removed: $run_id"
