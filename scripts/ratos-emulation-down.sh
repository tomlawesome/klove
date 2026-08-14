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
ratos_contract_prepared="$ratos_runtime_dir/contract-prepared.json"
ratos_contract_evidence="$ratos_runtime_dir/contract.json"
ratos_contract_succeeded="$ratos_runtime_dir/contract-succeeded"
ratos_require_recovery_origin "$ratos_origin"
ratos_require_prepared_inputs
ratos_require_active_identity "$ratos_active"
ratos_require_private_file "$ratos_runtime_dir/$ratos_overlay_name" 666

if timeout 15 docker container inspect "$ratos_proxy_container" >/dev/null 2>&1; then
    ratos_require_contract_container_identity \
        "$ratos_proxy_container" "$ratos_tool_image_id" moonraker-proxy
fi
if timeout 15 docker container inspect "$ratos_klove_container" >/dev/null 2>&1; then
    ratos_require_contract_container_identity \
        "$ratos_klove_container" "$ratos_klove_image_id" production-klove
fi
if timeout 15 docker container inspect "$ratos_contract_container" >/dev/null 2>&1; then
    ratos_require_contract_container_identity \
        "$ratos_contract_container" "$ratos_tool_image_id" contract-runner
fi

for ratos_stop_container in \
    "$ratos_contract_container" "$ratos_klove_container" \
    "$ratos_proxy_container" "$ratos_container"
do
    if timeout 15 docker container inspect "$ratos_stop_container" >/dev/null 2>&1; then
        status=$(timeout 15 docker inspect --format '{{.State.Status}}' "$ratos_stop_container")
        case "$status" in
            running)
                timeout 40 docker stop --timeout 20 "$ratos_stop_container" >/dev/null
                ;;
            created | exited) ;;
            *)
                echo "RatOS container is not safely checkable in state: $status" >&2
                exit 1
                ;;
        esac
        stopped_status=$(timeout 15 docker inspect \
            --format '{{.State.Status}}' "$ratos_stop_container")
        case "$stopped_status" in
            created | exited) ;;
            *)
                echo "RatOS container did not reach a safe stopped state; retained runtime" >&2
                exit 1
                ;;
        esac
    fi
done

ratos_run_overlay_tool check-overlay
if [ -e "$ratos_contract_succeeded" ] || [ -L "$ratos_contract_succeeded" ]; then
    ratos_require_private_file "$ratos_contract_succeeded" 600
    ratos_require_private_file "$ratos_contract_prepared" 600
    ratos_require_private_file "$ratos_contract_evidence" 600
    ratos_require_private_file "$ratos_probe_succeeded" 600
    ratos_require_private_file "$ratos_probe" 600
    run_outcome=contract-passed
elif [ -e "$ratos_probe_succeeded" ] || [ -L "$ratos_probe_succeeded" ]; then
    ratos_require_private_file "$ratos_probe_succeeded" 600
    ratos_require_private_file "$ratos_probe" 600
    if [ -e "$ratos_contract_prepared" ] || [ -L "$ratos_contract_prepared" ]; then
        ratos_require_private_file "$ratos_contract_prepared" 600
        if [ -e "$ratos_contract_evidence" ] || [ -L "$ratos_contract_evidence" ]; then
            ratos_require_private_file "$ratos_contract_evidence" 600
        fi
        run_outcome=contract-incomplete
    else
        run_outcome=probe-passed
    fi
else
    if [ -e "$ratos_probe" ] || [ -L "$ratos_probe" ]; then
        ratos_require_private_file "$ratos_probe" 600
    fi
    run_outcome=probe-incomplete
fi
for ratos_remove_container in \
    "$ratos_contract_container" "$ratos_klove_container" \
    "$ratos_proxy_container" "$ratos_container"
do
    if timeout 15 docker container inspect "$ratos_remove_container" >/dev/null 2>&1; then
        timeout 20 docker rm "$ratos_remove_container" >/dev/null
    fi
done
timeout 20 docker volume rm "$ratos_secret_volume" >/dev/null
ratos_archive_runtime "$run_outcome"
echo "RatOS emulator removed; secrets and COW destroyed; bounded evidence retained"
