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
ratos_overlay="$ratos_runtime_dir/$ratos_overlay_name"
ratos_firstboot_required="$ratos_runtime_dir/firstboot-required"
ratos_firstboot_restarted="$ratos_runtime_dir/firstboot-restarted"
ratos_probe="$ratos_runtime_dir/probe.json"
ratos_probe_succeeded="$ratos_runtime_dir/probe-succeeded"

ratos_require_origin "$ratos_origin"
ratos_require_prepared_inputs
for ratos_runtime_path in \
    "$ratos_active" "$ratos_overlay" "$ratos_firstboot_required" \
    "$ratos_firstboot_restarted" "$ratos_probe" "$ratos_probe_succeeded"
do
    ratos_require_absent "$ratos_runtime_path"
done
if timeout 15 docker container inspect "$ratos_container" >/dev/null 2>&1; then
    echo "an unbound container already uses the guarded RatOS emulator name" >&2
    exit 1
fi

ratos_run_overlay_tool overlay
ratos_require_private_file "$ratos_overlay" 666
umask 077
firstboot_partial=$(mktemp "$ratos_runtime_dir/firstboot.XXXXXX")
active_partial=$(mktemp "$ratos_runtime_dir/active.XXXXXX")
cleanup_state_partials() {
    rm -f -- "$firstboot_partial" "$active_partial"
}
trap cleanup_state_partials EXIT HUP INT TERM
chmod 0600 "$firstboot_partial" "$active_partial"
mv -- "$firstboot_partial" "$ratos_firstboot_required"
printf '%s\n%s\n%s\n' \
    "$ratos_container" "$ratos_tool_image_id" "$ratos_state_dir" > "$active_partial"
mv -- "$active_partial" "$ratos_active"
trap - EXIT HUP INT TERM

cleanup_failed_start() {
    cleanup_complete=0
    if timeout 15 docker container inspect "$ratos_container" >/dev/null 2>&1; then
        cleanup_actual_image=$(timeout 15 docker inspect \
            --format '{{.Image}}' "$ratos_container" 2>/dev/null || true)
        cleanup_label_state=$(timeout 15 docker inspect \
            --format '{{index .Config.Labels "io.klove.ratos.state"}}' \
            "$ratos_container" 2>/dev/null || true)
        cleanup_label_image=$(timeout 15 docker inspect \
            --format '{{index .Config.Labels "io.klove.ratos.tool-image"}}' \
            "$ratos_container" 2>/dev/null || true)
        if [ "$cleanup_actual_image" = "$ratos_tool_image_id" ] \
            && [ "$cleanup_label_state" = "$ratos_state_dir" ] \
            && [ "$cleanup_label_image" = "$ratos_tool_image_id" ]; then
            cleanup_status=$(timeout 15 docker inspect \
                --format '{{.State.Status}}' "$ratos_container" 2>/dev/null || true)
            if { [ "$cleanup_status" != running ] \
                || timeout 20 docker stop --timeout 10 "$ratos_container" >/dev/null 2>&1; } \
                && timeout 20 docker rm "$ratos_container" >/dev/null 2>&1; then
                cleanup_complete=1
            fi
        fi
    else
        cleanup_complete=1
    fi
    if [ "$cleanup_complete" -eq 1 ] \
        && ratos_run_overlay_tool check-overlay >/dev/null 2>&1 \
        && ratos_archive_runtime startup-failed; then
        echo "RatOS emulator startup failed; retained a guarded aborted run" >&2
    else
        echo "RatOS emulator cleanup failed; retained guarded runtime state" >&2
    fi
}
trap cleanup_failed_start EXIT HUP INT TERM

timeout 30 docker run \
    --name "$ratos_container" \
    --detach \
    --network none \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --pids-limit 256 \
    --memory 2g \
    --cpus 4 \
    --log-driver local \
    --log-opt max-size=1m \
    --log-opt max-file=2 \
    --tmpfs /tmp:rw,noexec,nosuid,size=128m \
    --label "io.klove.ratos.state=$ratos_state_dir" \
    --label "io.klove.ratos.tool-image=$ratos_tool_image_id" \
    --mount "type=bind,source=$ratos_state_dir/$ratos_raw_name,target=/inputs/$ratos_raw_name,readonly" \
    --mount "type=bind,source=$ratos_state_dir/extracted/kernel8.Image,target=/inputs/kernel8.Image,readonly" \
    --mount "type=bind,source=$ratos_state_dir/extracted/bcm2710-rpi-3-b.dtb,target=/inputs/bcm2710-rpi-3-b.dtb,readonly" \
    --mount "type=bind,source=$ratos_overlay,target=/run-state/$ratos_overlay_name" \
    "$ratos_tool_image_id" \
    /usr/bin/qemu-system-aarch64 \
        -machine raspi3b \
        -cpu cortex-a53 \
        -accel tcg,thread=multi \
        -m 1G \
        -smp 4 \
        -kernel /inputs/kernel8.Image \
        -dtb /inputs/bcm2710-rpi-3-b.dtb \
        -append 'rw earlycon=pl011,0x3f201000 earlyprintk loglevel=8 console=ttyAMA0,115200 dwc_otg.lpm_enable=0 root=/dev/mmcblk0p2 rootdelay=1 modules-load=usbnet,cdc_ether,cdc_subset,rndis_host systemd.watchdog_device=/dev/watchdog9 systemd.show_status=true systemd.log_target=console' \
        -drive file=/run-state/ratos-rpi32-run.qcow2,if=sd,index=0,format=qcow2 \
        -usb \
        -netdev user,id=net0,restrict=on,hostfwd=tcp:127.0.0.1:18080-:80,hostfwd=tcp:127.0.0.1:17125-:7125,hostfwd=tcp:127.0.0.1:12222-:22 \
        -device usb-net,netdev=net0,msos-desc=off \
        -display none \
        -serial stdio \
        -serial null \
        -monitor none \
        -no-reboot \
        -sandbox on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny

trap - EXIT HUP INT TERM
echo "RatOS v2.1.0 emulator boot started from a fresh COW overlay"
