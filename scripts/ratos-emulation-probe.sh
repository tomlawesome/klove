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
ratos_firstboot_required="$ratos_runtime_dir/firstboot-required"
ratos_restart_marker="$ratos_runtime_dir/firstboot-restarted"
ratos_probe="$ratos_runtime_dir/probe.json"
ratos_probe_succeeded="$ratos_runtime_dir/probe-succeeded"
firstboot_timeout_seconds=600
service_timeout_seconds=900
ratos_require_origin "$ratos_origin"
ratos_require_prepared_inputs
ratos_require_active "$ratos_active"
ratos_require_private_file "$ratos_runtime_dir/$ratos_overlay_name" 666
ratos_require_private_file "$ratos_firstboot_required" 600
ratos_require_absent "$ratos_restart_marker"
ratos_require_absent "$ratos_probe"
ratos_require_absent "$ratos_probe_succeeded"

umask 077
probe_output=$(mktemp "$ratos_runtime_dir/probe.XXXXXX")
probe_error=$(mktemp "$ratos_runtime_dir/probe-error.XXXXXX")
probe_success_partial=$(mktemp "$ratos_runtime_dir/probe-succeeded.XXXXXX")
reboot_log=$(mktemp "$ratos_runtime_dir/reboot-log.XXXXXX")
chmod 0600 "$probe_success_partial"
cleanup_probe_files() {
    rm -f -- "$probe_output" "$probe_error" "$probe_success_partial" "$reboot_log"
}
trap cleanup_probe_files EXIT HUP INT TERM

deadline=$(( $(date +%s) + firstboot_timeout_seconds ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    status=$(timeout 15 docker inspect --format '{{.State.Status}}' "$ratos_container")
    case "$status" in
        running)
            if [ ! -e "$ratos_firstboot_required" ] \
                && timeout 20 docker exec "$ratos_container" \
                /usr/local/bin/python /opt/klove-ratos/tool.py probe \
                > "$probe_output" 2> "$probe_error"; then
                chmod 0600 "$probe_output"
                mv -- "$probe_output" "$ratos_probe"
                cat "$ratos_probe"
                mv -- "$probe_success_partial" "$ratos_probe_succeeded"
                trap - EXIT HUP INT TERM
                rm -f -- "$probe_error"
                exit 0
            fi
            ;;
        exited)
            exit_code=$(timeout 15 docker inspect --format '{{.State.ExitCode}}' "$ratos_container")
            if [ "$exit_code" = 0 ] && [ -e "$ratos_firstboot_required" ] \
                && [ ! -e "$ratos_restart_marker" ]; then
                if ! timeout 15 docker logs "$ratos_container" > "$reboot_log" 2>&1; then
                    echo "RatOS emulator first-boot log retrieval failed" >&2
                    exit 1
                fi
                if grep -q 'Please reboot' "$reboot_log" \
                    && grep -q 'reboot: Restarting system' "$reboot_log"; then
                    rm -f -- "$reboot_log"
                    mv -- "$ratos_firstboot_required" "$ratos_restart_marker"
                    timeout 30 docker start "$ratos_container" >/dev/null
                    deadline=$(( $(date +%s) + service_timeout_seconds ))
                else
                    echo "RatOS emulator omitted required first-boot reboot evidence" >&2
                    exit 1
                fi
            else
                echo "RatOS emulator exited without the one accepted first-boot reboot" >&2
                exit 1
            fi
            ;;
        *)
            echo "RatOS emulator entered unexpected container state: $status" >&2
            exit 1
            ;;
    esac
    sleep 5
done

cat "$probe_error" >&2
if [ -e "$ratos_firstboot_required" ]; then
    echo "RatOS emulator did not complete first boot within the fixed deadline" >&2
else
    echo "RatOS emulator did not expose the service contract within the fixed post-reboot deadline" >&2
fi
exit 1
