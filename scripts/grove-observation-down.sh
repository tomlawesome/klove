#!/usr/bin/env sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: $0 RUN_ID" >&2
    exit 2
fi

run_id=$1
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$script_dir/grove-observation-lib.sh"
grove_observation_validate_run_id "$run_id"

repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
state_dir="$repo_root/.klove-integration/grove-observation/$run_id"
origin_file="$state_dir/origin"
if [ ! -f "$origin_file" ]; then
    echo "unknown Grove observation run: $run_id" >&2
    exit 1
fi

grove_observation_require_origin "$origin_file"
grove_observation_require_name "$run_id"
grove_observation_remove_recorded "$run_id"
rm -f -- "$origin_file"
rmdir -- "$state_dir"
echo "Grove observation removed: $run_id"
