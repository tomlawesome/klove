#!/usr/bin/env sh
set -eu

if [ "$#" -ne 0 ]; then
    echo "usage: $0" >&2
    exit 2
fi

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
. "$script_dir/ratos-emulation-lib.sh"
repo_root=$(CDPATH='' cd -- "$script_dir/.." && pwd)
ratos_integration_root="$repo_root/.klove-integration"
ratos_state_dir="$ratos_integration_root/ratos-v2.1.0"
ratos_runtime_dir="$ratos_state_dir/runtime"
ratos_evidence_dir="$ratos_state_dir/evidence"
ratos_origin="$ratos_state_dir/origin"
ratos_active="$ratos_runtime_dir/active"
ratos_probe="$ratos_runtime_dir/probe.json"
ratos_probe_succeeded="$ratos_runtime_dir/probe-succeeded"
ratos_require_origin "$ratos_origin"
ratos_require_prepared_inputs
ratos_require_active "$ratos_active"
ratos_require_private_file "$ratos_runtime_dir/$ratos_overlay_name" 666

status=$(timeout 15 docker inspect --format '{{.State.Status}}' "$ratos_container")
case "$status" in
    running)
        timeout 40 docker stop --timeout 20 "$ratos_container" >/dev/null
        stopped_status=$(timeout 15 docker inspect \
            --format '{{.State.Status}}' "$ratos_container")
        if [ "$stopped_status" != exited ]; then
            echo "RatOS emulator did not reach a safe stopped state; retained runtime" >&2
            exit 1
        fi
        ;;
    exited) ;;
    *)
        echo "RatOS emulator is not safely checkable in state: $status" >&2
        exit 1
        ;;
esac

ratos_run_overlay_tool check-overlay
if [ -e "$ratos_probe_succeeded" ] || [ -L "$ratos_probe_succeeded" ]; then
    ratos_require_private_file "$ratos_probe_succeeded" 600
    ratos_require_private_file "$ratos_probe" 600
    run_outcome=probe-passed
else
    if [ -e "$ratos_probe" ] || [ -L "$ratos_probe" ]; then
        ratos_require_private_file "$ratos_probe" 600
    fi
    run_outcome=probe-incomplete
fi
timeout 20 docker rm "$ratos_container" >/dev/null
ratos_archive_runtime "$run_outcome"
echo "RatOS emulator removed; fresh-run COW and bounded evidence were retained"
