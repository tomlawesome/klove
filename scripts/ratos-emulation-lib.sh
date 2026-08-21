#!/usr/bin/env sh

ratos_asset_name=2026-03-04-RatOS-2.1.0-raspberry-rpi32.img.xz
ratos_raw_name=2026-03-04-RatOS-2.1.0-raspberry-rpi32.img
ratos_asset_bytes=2125243800
ratos_asset_sha256=513465cf6b233d73e5c9ed048568493894d77c7c67e93586609f9b7fbc182153
ratos_tool_tag=klove-ratos-qemu:v2.1.0-spike
ratos_klove_tag=klove-ratos-contract:v2.1.0-local
ratos_container=klove-ratos-v2-1-0
ratos_proxy_container=klove-ratos-v2-1-0-proxy
ratos_klove_container=klove-ratos-v2-1-0-klove
ratos_contract_container=klove-ratos-v2-1-0-contract
ratos_secret_volume=klove-ratos-v2-1-0-secrets
ratos_overlay_name=ratos-rpi32-run.qcow2

ratos_require_host_tools() {
    for ratos_command in \
        basename cat chmod cp date df docker git grep id mkdir mktemp mv rm sed sha256sum \
        sleep sort stat tail timeout tr wc
    do
        if ! command -v "$ratos_command" >/dev/null 2>&1; then
            echo "RatOS emulation requires host command: $ratos_command" >&2
            exit 1
        fi
    done
    if ! stat --version 2>/dev/null | grep -q 'GNU coreutils' \
        || ! cp --version 2>/dev/null | grep -q 'GNU coreutils'; then
        echo "RatOS emulation requires GNU coreutils on Linux" >&2
        exit 1
    fi
}

ratos_capture_source_state() {
    ratos_source_revision=$(git -C "$repo_root" rev-parse HEAD)
    ratos_source_manifest=$(mktemp)
    ratos_source_paths=$(mktemp)
    ratos_source_paths_sorted=$(mktemp)
    chmod 0600 "$ratos_source_manifest" "$ratos_source_paths" "$ratos_source_paths_sorted"
    for ratos_source_path in \
        .dockerignore \
        Dockerfile \
        README.md \
        pyproject.toml \
        requirements-build.lock \
        requirements.lock \
        scripts/ratos-emulation-down.sh \
        scripts/ratos-emulation-contract.sh \
        scripts/ratos-emulation-lib.sh \
        scripts/ratos-emulation-prepare.sh \
        scripts/ratos-emulation-probe.sh \
        scripts/ratos-emulation-up.sh \
        scripts/test-ratos-emulation.sh \
        tests/integration/ratos-emulation/Dockerfile \
        tests/integration/ratos-emulation/Dockerfile.dockerignore \
        tests/integration/ratos-emulation/SHA256SUMS \
        tests/integration/ratos-emulation/contract/klove.toml \
        tests/integration/ratos-emulation/contract/printer.cfg \
        tests/integration/ratos-emulation/contract/ratos_exercise_contract.py \
        tests/integration/ratos-emulation/debian.sources \
        tests/integration/ratos-emulation/tool.py \
        tests/integration/moonraker-sim/fixture/contract.gcode \
        tests/integration/moonraker-sim/fixture/exercise_contract.py \
        tests/integration/moonraker-sim/fixture/proxy.py
    do
        printf '%s\n' "$ratos_source_path" >> "$ratos_source_paths"
    done
    git -C "$repo_root" ls-files --cached --others --exclude-standard -- src \
        | sort >> "$ratos_source_paths"
    sort -u "$ratos_source_paths" > "$ratos_source_paths_sorted"
    mv -- "$ratos_source_paths_sorted" "$ratos_source_paths"
    while IFS= read -r ratos_source_path
    do
        ratos_source_file="$repo_root/$ratos_source_path"
        if [ ! -f "$ratos_source_file" ] || [ -L "$ratos_source_file" ]; then
            rm -f -- "$ratos_source_manifest" "$ratos_source_paths" \
                "$ratos_source_paths_sorted"
            echo "RatOS lane source is missing, non-regular, or a symlink" >&2
            exit 1
        fi
        ratos_source_sha=$(sha256sum -- "$ratos_source_file")
        ratos_source_sha=${ratos_source_sha%% *}
        if [ -x "$ratos_source_file" ]; then
            ratos_source_mode=100755
        else
            ratos_source_mode=100644
        fi
        printf '%s %s %s\n' \
            "$ratos_source_mode" "$ratos_source_sha" "$ratos_source_path" \
            >> "$ratos_source_manifest"
    done < "$ratos_source_paths"
    ratos_source_digest=$(sha256sum -- "$ratos_source_manifest")
    ratos_source_digest=${ratos_source_digest%% *}
    rm -f -- "$ratos_source_manifest" "$ratos_source_paths" "$ratos_source_paths_sorted"
}

ratos_require_private_dir() {
    ratos_directory=$1
    if [ ! -d "$ratos_directory" ] || [ -L "$ratos_directory" ]; then
        echo "RatOS state directory is missing, non-directory, or a symlink: $ratos_directory" >&2
        exit 1
    fi
    ratos_directory_owner=$(stat --format='%u' -- "$ratos_directory")
    ratos_directory_mode=$(stat --format='%a' -- "$ratos_directory")
    if [ "$ratos_directory_owner" != "$(id -u)" ] || [ "$ratos_directory_mode" != 700 ]; then
        echo "RatOS state directories must be owned by the current user with mode 0700" >&2
        exit 1
    fi
}

ratos_ensure_private_dir() {
    ratos_directory=$1
    if [ -e "$ratos_directory" ] || [ -L "$ratos_directory" ]; then
        ratos_require_private_dir "$ratos_directory"
    else
        mkdir -- "$ratos_directory"
        chmod 0700 "$ratos_directory"
        ratos_require_private_dir "$ratos_directory"
    fi
}

ratos_require_private_file() {
    ratos_file=$1
    ratos_expected_mode=$2
    if [ ! -f "$ratos_file" ] || [ -L "$ratos_file" ]; then
        echo "RatOS state file is missing, non-regular, or a symlink: $ratos_file" >&2
        exit 1
    fi
    ratos_file_owner=$(stat --format='%u' -- "$ratos_file")
    ratos_file_mode=$(stat --format='%a' -- "$ratos_file")
    if [ "$ratos_file_owner" != "$(id -u)" ] || [ "$ratos_file_mode" != "$ratos_expected_mode" ]; then
        echo "RatOS state file has unexpected ownership or mode: $ratos_file" >&2
        exit 1
    fi
}

ratos_require_absent() {
    ratos_path=$1
    if [ -e "$ratos_path" ] || [ -L "$ratos_path" ]; then
        echo "unexpected pre-existing RatOS runtime path: $ratos_path" >&2
        exit 1
    fi
}

ratos_validate_state_tree() {
    ratos_require_private_dir "$ratos_integration_root"
    ratos_require_private_dir "$ratos_state_dir"
    ratos_require_private_dir "$ratos_runtime_dir"
    ratos_require_private_dir "$ratos_evidence_dir"
}

ratos_capture_daemon() {
    ratos_require_host_tools
    ratos_context=$(timeout 15 docker context show)
    ratos_daemon_id=$(timeout 15 docker info --format '{{.ID}}')
    ratos_security_options=$(timeout 15 docker info --format '{{json .SecurityOptions}}')
    if [ -z "$ratos_context" ] || [ -z "$ratos_daemon_id" ]; then
        echo "Docker context or daemon identity is unavailable" >&2
        exit 1
    fi
    case "$ratos_security_options" in
        *name=rootless*) ;;
        *)
            echo "RatOS emulation requires rootless Docker" >&2
            exit 1
            ;;
    esac
}

ratos_require_prepared_inputs() {
    ratos_validate_state_tree
    ratos_require_private_file "$ratos_state_dir/$ratos_asset_name" 400
    ratos_require_private_file "$ratos_state_dir/$ratos_raw_name" 444
    ratos_require_private_dir "$ratos_state_dir/extracted"
    ratos_require_private_file "$ratos_state_dir/extracted/kernel8.Image" 444
    ratos_require_private_file "$ratos_state_dir/extracted/bcm2710-rpi-3-b.dtb" 444
    ratos_require_private_file "$ratos_state_dir/identities.json" 600
}

ratos_read_origin() {
    ratos_origin_file=$1
    ratos_validate_state_tree
    ratos_require_private_file "$ratos_origin_file" 600
    if [ "$(wc -l < "$ratos_origin_file")" -ne 6 ]; then
        echo "RatOS emulation origin is malformed; run prepare first" >&2
        exit 1
    fi
    ratos_stored_context=$(sed -n '1p' "$ratos_origin_file")
    ratos_stored_daemon_id=$(sed -n '2p' "$ratos_origin_file")
    ratos_tool_image_id=$(sed -n '3p' "$ratos_origin_file")
    ratos_klove_image_id=$(sed -n '4p' "$ratos_origin_file")
    ratos_stored_source_revision=$(sed -n '5p' "$ratos_origin_file")
    ratos_stored_source_digest=$(sed -n '6p' "$ratos_origin_file")
}

ratos_require_replaceable_origin() {
    ratos_origin_file=$1
    ratos_validate_state_tree
    ratos_require_private_file "$ratos_origin_file" 600
    ratos_origin_lines=$(wc -l < "$ratos_origin_file")
    if [ "$ratos_origin_lines" -ne 4 ] \
        && [ "$ratos_origin_lines" -ne 5 ] \
        && [ "$ratos_origin_lines" -ne 6 ]; then
        echo "RatOS emulation origin is malformed; retain state for recovery" >&2
        exit 1
    fi
    ratos_stored_context=$(sed -n '1p' "$ratos_origin_file")
    ratos_stored_daemon_id=$(sed -n '2p' "$ratos_origin_file")
    ratos_capture_daemon
    if [ "$ratos_context" != "$ratos_stored_context" ] \
        || [ "$ratos_daemon_id" != "$ratos_stored_daemon_id" ]; then
        echo "RatOS emulation state belongs to a different Docker context or daemon" >&2
        exit 1
    fi
}

ratos_require_recovery_origin() {
    ratos_origin_file=$1
    ratos_read_origin "$ratos_origin_file"
    ratos_capture_daemon
    if [ "$ratos_context" != "$ratos_stored_context" ] \
        || [ "$ratos_daemon_id" != "$ratos_stored_daemon_id" ]; then
        echo "RatOS emulation state belongs to a different Docker context or daemon" >&2
        exit 1
    fi
    if ! timeout 15 docker image inspect "$ratos_tool_image_id" >/dev/null 2>&1; then
        echo "the exact recorded RatOS emulator image is unavailable" >&2
        exit 1
    fi
    if ! timeout 15 docker image inspect "$ratos_klove_image_id" >/dev/null 2>&1; then
        echo "the exact recorded production Klove image is unavailable" >&2
        exit 1
    fi
    ratos_image_revision=$(timeout 15 docker image inspect \
        --format '{{index .Config.Labels "io.klove.source-revision"}}' \
        "$ratos_tool_image_id")
    ratos_image_source_digest=$(timeout 15 docker image inspect \
        --format '{{index .Config.Labels "io.klove.source-digest"}}' \
        "$ratos_tool_image_id")
    if [ "$ratos_image_revision" != "$ratos_stored_source_revision" ] \
        || [ "$ratos_image_source_digest" != "$ratos_stored_source_digest" ]; then
        echo "the exact RatOS emulator image has unexpected source labels" >&2
        exit 1
    fi
    ratos_klove_revision=$(timeout 15 docker image inspect \
        --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
        "$ratos_klove_image_id")
    ratos_klove_source_digest=$(timeout 15 docker image inspect \
        --format '{{index .Config.Labels "io.github.tomlawesome.klove.source-digest"}}' \
        "$ratos_klove_image_id")
    if [ "$ratos_klove_revision" != "$ratos_stored_source_revision" ] \
        || [ "$ratos_klove_source_digest" != "$ratos_stored_source_digest" ]; then
        echo "the exact production Klove image has unexpected source labels" >&2
        exit 1
    fi
}

ratos_require_origin() {
    ratos_origin_file=$1
    ratos_require_recovery_origin "$ratos_origin_file"
    ratos_capture_source_state
    if [ "$ratos_source_revision" != "$ratos_stored_source_revision" ] \
        || [ "$ratos_source_digest" != "$ratos_stored_source_digest" ]; then
        echo "RatOS emulation state belongs to different lane source" >&2
        exit 1
    fi
}

ratos_require_container_confinement() {
    ratos_confined_target=$1
    ratos_expected_network=$2
    ratos_expected_pids=$3
    ratos_expected_memory=$4
    ratos_expected_nano_cpus=$5
    ratos_expected_mount_count=$6
    ratos_expected_user=$7
    ratos_expected_log_size=$8
    ratos_expected_log_files=$9
    ratos_confined_network=$(timeout 15 docker inspect \
        --format '{{.HostConfig.NetworkMode}}' "$ratos_confined_target")
    ratos_confined_readonly=$(timeout 15 docker inspect \
        --format '{{.HostConfig.ReadonlyRootfs}}' "$ratos_confined_target")
    ratos_confined_privileged=$(timeout 15 docker inspect \
        --format '{{.HostConfig.Privileged}}' "$ratos_confined_target")
    ratos_confined_cap_drop=$(timeout 15 docker inspect \
        --format '{{json .HostConfig.CapDrop}}' "$ratos_confined_target")
    ratos_confined_cap_add=$(timeout 15 docker inspect \
        --format '{{json .HostConfig.CapAdd}}' "$ratos_confined_target")
    ratos_confined_security=$(timeout 15 docker inspect \
        --format '{{json .HostConfig.SecurityOpt}}' "$ratos_confined_target")
    ratos_confined_devices=$(timeout 15 docker inspect \
        --format '{{json .HostConfig.Devices}}' "$ratos_confined_target")
    ratos_confined_device_requests=$(timeout 15 docker inspect \
        --format '{{json .HostConfig.DeviceRequests}}' "$ratos_confined_target")
    ratos_confined_ports=$(timeout 15 docker inspect \
        --format '{{json .HostConfig.PortBindings}}' "$ratos_confined_target")
    ratos_confined_publish_all=$(timeout 15 docker inspect \
        --format '{{.HostConfig.PublishAllPorts}}' "$ratos_confined_target")
    ratos_confined_pids=$(timeout 15 docker inspect \
        --format '{{.HostConfig.PidsLimit}}' "$ratos_confined_target")
    ratos_confined_memory=$(timeout 15 docker inspect \
        --format '{{.HostConfig.Memory}}' "$ratos_confined_target")
    ratos_confined_nano_cpus=$(timeout 15 docker inspect \
        --format '{{.HostConfig.NanoCpus}}' "$ratos_confined_target")
    ratos_confined_mount_count=$(timeout 15 docker inspect \
        --format '{{len .Mounts}}' "$ratos_confined_target")
    ratos_confined_user=$(timeout 15 docker inspect \
        --format '{{.Config.User}}' "$ratos_confined_target")
    ratos_confined_restart=$(timeout 15 docker inspect \
        --format '{{.HostConfig.RestartPolicy.Name}}' "$ratos_confined_target")
    ratos_confined_log_type=$(timeout 15 docker inspect \
        --format '{{.HostConfig.LogConfig.Type}}' "$ratos_confined_target")
    ratos_confined_log_size=$(timeout 15 docker inspect \
        --format '{{index .HostConfig.LogConfig.Config "max-size"}}' "$ratos_confined_target")
    ratos_confined_log_files=$(timeout 15 docker inspect \
        --format '{{index .HostConfig.LogConfig.Config "max-file"}}' "$ratos_confined_target")
    for ratos_empty_container_setting in \
        "$ratos_confined_cap_add" "$ratos_confined_devices" "$ratos_confined_device_requests"
    do
        case "$ratos_empty_container_setting" in
            null | '[]') ;;
            *)
                echo "RatOS container unexpectedly adds capabilities or devices" >&2
                exit 1
                ;;
        esac
    done
    case "$ratos_confined_ports" in
        null | '{}') ;;
        *)
            echo "RatOS container unexpectedly publishes ports" >&2
            exit 1
            ;;
    esac
    if [ "$ratos_confined_network" != "$ratos_expected_network" ] \
        || [ "$ratos_confined_readonly" != true ] \
        || [ "$ratos_confined_privileged" != false ] \
        || [ "$ratos_confined_cap_drop" != '["ALL"]' ] \
        || [ "$ratos_confined_security" != '["no-new-privileges"]' ] \
        || [ "$ratos_confined_publish_all" != false ] \
        || [ "$ratos_confined_pids" != "$ratos_expected_pids" ] \
        || [ "$ratos_confined_memory" != "$ratos_expected_memory" ] \
        || [ "$ratos_confined_nano_cpus" != "$ratos_expected_nano_cpus" ] \
        || [ "$ratos_confined_mount_count" != "$ratos_expected_mount_count" ] \
        || [ "$ratos_confined_user" != "$ratos_expected_user" ] \
        || [ "$ratos_confined_restart" != no ] \
        || [ "$ratos_confined_log_type" != local ] \
        || [ "$ratos_confined_log_size" != "$ratos_expected_log_size" ] \
        || [ "$ratos_confined_log_files" != "$ratos_expected_log_files" ]; then
        echo "RatOS container confinement or resource limits are unexpected" >&2
        exit 1
    fi
}

ratos_require_mount() {
    ratos_mount_target=$1
    ratos_mount_destination=$2
    ratos_expected_mount_type=$3
    ratos_expected_mount_identity=$4
    ratos_expected_mount_rw=$5
    ratos_mount_format="{{range .Mounts}}{{if eq .Destination \"$ratos_mount_destination\"}}{{.Type}}|{{.Source}}|{{.Name}}|{{.RW}}{{end}}{{end}}"
    ratos_mount_actual=$(timeout 15 docker inspect \
        --format "$ratos_mount_format" "$ratos_mount_target")
    case "$ratos_expected_mount_type" in
        bind)
            ratos_expected_mount="bind|$ratos_expected_mount_identity||$ratos_expected_mount_rw"
            ;;
        volume)
            case "$ratos_mount_actual" in
                "volume|"*"|$ratos_expected_mount_identity|$ratos_expected_mount_rw")
                    return
                    ;;
            esac
            echo "RatOS container volume mount is unexpected" >&2
            exit 1
            ;;
        *)
            echo "unknown RatOS expected mount type" >&2
            exit 1
            ;;
    esac
    if [ "$ratos_mount_actual" != "$ratos_expected_mount" ]; then
        echo "RatOS container bind mount is unexpected" >&2
        exit 1
    fi
}

ratos_require_container_command() {
    ratos_command_target=$1
    ratos_expected_entrypoint=$2
    ratos_expected_command=$3
    ratos_actual_entrypoint=$(timeout 15 docker inspect \
        --format '{{json .Config.Entrypoint}}' "$ratos_command_target")
    ratos_actual_command=$(timeout 15 docker inspect \
        --format '{{json .Config.Cmd}}' "$ratos_command_target")
    if [ "$ratos_actual_entrypoint" != "$ratos_expected_entrypoint" ] \
        || [ "$ratos_actual_command" != "$ratos_expected_command" ]; then
        echo "RatOS container command is unexpected" >&2
        exit 1
    fi
}

ratos_require_active_identity() {
    ratos_active_file=$1
    ratos_require_private_file "$ratos_active_file" 600
    if [ "$(wc -l < "$ratos_active_file")" -ne 5 ]; then
        echo "RatOS emulator active state is malformed" >&2
        exit 1
    fi
    ratos_active_container=$(sed -n '1p' "$ratos_active_file")
    ratos_active_image_id=$(sed -n '2p' "$ratos_active_file")
    ratos_active_klove_image_id=$(sed -n '3p' "$ratos_active_file")
    ratos_active_state_dir=$(sed -n '4p' "$ratos_active_file")
    ratos_active_secret_volume=$(sed -n '5p' "$ratos_active_file")
    if [ "$ratos_active_container" != "$ratos_container" ] \
        || [ "$ratos_active_image_id" != "$ratos_tool_image_id" ] \
        || [ "$ratos_active_klove_image_id" != "$ratos_klove_image_id" ] \
        || [ "$ratos_active_state_dir" != "$ratos_state_dir" ] \
        || [ "$ratos_active_secret_volume" != "$ratos_secret_volume" ]; then
        echo "RatOS emulator active state does not match the expected exact target" >&2
        exit 1
    fi
    if ! timeout 15 docker container inspect "$ratos_container" >/dev/null 2>&1; then
        echo "recorded RatOS emulator container is unavailable; retain state for recovery" >&2
        exit 1
    fi
    ratos_actual_image=$(timeout 15 docker inspect --format '{{.Image}}' "$ratos_container")
    ratos_label_state=$(timeout 15 docker inspect \
        --format '{{index .Config.Labels "io.klove.ratos.state"}}' "$ratos_container")
    ratos_label_image=$(timeout 15 docker inspect \
        --format '{{index .Config.Labels "io.klove.ratos.tool-image"}}' "$ratos_container")
    ratos_label_source=$(timeout 15 docker inspect \
        --format '{{index .Config.Labels "io.klove.ratos.source-digest"}}' "$ratos_container")
    ratos_mounted_secret_volume=$(timeout 15 docker inspect \
        --format '{{range .Mounts}}{{if eq .Destination "/run/klove-secrets"}}{{.Name}}{{end}}{{end}}' \
        "$ratos_container")
    if [ "$ratos_actual_image" != "$ratos_tool_image_id" ] \
        || [ "$ratos_label_state" != "$ratos_state_dir" ] \
        || [ "$ratos_label_image" != "$ratos_tool_image_id" ] \
        || [ "$ratos_label_source" != "$ratos_stored_source_digest" ] \
        || [ "$ratos_mounted_secret_volume" != "$ratos_secret_volume" ]; then
        echo "RatOS emulator image or labels do not match the guarded local state" >&2
        exit 1
    fi
    ratos_identity_network=$(timeout 15 docker inspect \
        --format '{{.HostConfig.NetworkMode}}' "$ratos_container")
    ratos_identity_mount_count=$(timeout 15 docker inspect \
        --format '{{len .Mounts}}' "$ratos_container")
    if [ "$ratos_identity_network" != none ] || [ "$ratos_identity_mount_count" -ne 5 ]; then
        echo "RatOS emulator recovery network or mount count is unexpected" >&2
        exit 1
    fi
    ratos_require_mount "$ratos_container" "/inputs/$ratos_raw_name" bind \
        "$ratos_state_dir/$ratos_raw_name" false
    ratos_require_mount "$ratos_container" /inputs/kernel8.Image bind \
        "$ratos_state_dir/extracted/kernel8.Image" false
    ratos_require_mount "$ratos_container" /inputs/bcm2710-rpi-3-b.dtb bind \
        "$ratos_state_dir/extracted/bcm2710-rpi-3-b.dtb" false
    ratos_require_mount "$ratos_container" "/run-state/$ratos_overlay_name" bind \
        "$ratos_runtime_dir/$ratos_overlay_name" true
    ratos_require_mount "$ratos_container" /run/klove-secrets volume \
        "$ratos_secret_volume" true
    ratos_require_secret_volume
}

ratos_require_active() {
    ratos_active_file=$1
    ratos_require_active_identity "$ratos_active_file"
    ratos_require_container_confinement \
        "$ratos_container" none 256 3221225472 4000000000 5 10001:10001 1m 2
    ratos_expected_qemu_command='["/usr/bin/qemu-system-aarch64","-machine","raspi3b","-cpu","cortex-a53","-accel","tcg,thread=multi","-m","1G","-smp","4","-kernel","/inputs/kernel8.Image","-dtb","/inputs/bcm2710-rpi-3-b.dtb","-append","rw earlycon=pl011,0x3f201000 earlyprintk loglevel=8 console=ttyAMA0,115200 dwc_otg.lpm_enable=0 root=/dev/mmcblk0p2 rootdelay=1 modules-load=usbnet,cdc_ether,cdc_subset,rndis_host systemd.watchdog_device=/dev/watchdog9 systemd.show_status=true systemd.log_target=console","-drive","file=/run-state/ratos-rpi32-run.qcow2,if=sd,index=0,format=qcow2","-usb","-netdev","user,id=net0,restrict=on,hostfwd=tcp:127.0.0.1:18080-:80,hostfwd=tcp:127.0.0.1:17125-:7125,hostfwd=tcp:127.0.0.1:12222-:22","-device","usb-net,netdev=net0,msos-desc=off","-display","none","-serial","stdio","-serial","null","-monitor","none","-no-reboot","-sandbox","on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny"]'
    ratos_require_container_command "$ratos_container" null "$ratos_expected_qemu_command"
}

ratos_require_secret_volume() {
    if ! timeout 15 docker volume inspect "$ratos_secret_volume" >/dev/null 2>&1; then
        echo "the exact RatOS contract secret volume is unavailable" >&2
        exit 1
    fi
    ratos_volume_state=$(timeout 15 docker volume inspect \
        --format '{{index .Labels "io.klove.ratos.state"}}' "$ratos_secret_volume")
    ratos_volume_source=$(timeout 15 docker volume inspect \
        --format '{{index .Labels "io.klove.ratos.source-digest"}}' "$ratos_secret_volume")
    ratos_volume_purpose=$(timeout 15 docker volume inspect \
        --format '{{index .Labels "io.klove.ratos.purpose"}}' "$ratos_secret_volume")
    if [ "$ratos_volume_state" != "$ratos_state_dir" ] \
        || [ "$ratos_volume_source" != "$ratos_stored_source_digest" ] \
        || [ "$ratos_volume_purpose" != contract-secrets ]; then
        echo "RatOS contract secret volume labels do not match guarded state" >&2
        exit 1
    fi
}

ratos_require_contract_container_identity() {
    ratos_contract_target=$1
    ratos_contract_image=$2
    ratos_contract_role=$3
    if ! timeout 15 docker container inspect "$ratos_contract_target" >/dev/null 2>&1; then
        echo "the recorded RatOS contract container is unavailable" >&2
        exit 1
    fi
    ratos_contract_actual_image=$(timeout 15 docker inspect \
        --format '{{.Image}}' "$ratos_contract_target")
    ratos_contract_label_state=$(timeout 15 docker inspect \
        --format '{{index .Config.Labels "io.klove.ratos.state"}}' "$ratos_contract_target")
    ratos_contract_label_source=$(timeout 15 docker inspect \
        --format '{{index .Config.Labels "io.klove.ratos.source-digest"}}' \
        "$ratos_contract_target")
    ratos_contract_label_role=$(timeout 15 docker inspect \
        --format '{{index .Config.Labels "io.klove.ratos.role"}}' "$ratos_contract_target")
    ratos_qemu_id=$(timeout 15 docker inspect --format '{{.Id}}' "$ratos_container")
    ratos_contract_network=$(timeout 15 docker inspect \
        --format '{{.HostConfig.NetworkMode}}' "$ratos_contract_target")
    ratos_contract_secret_mount=$(timeout 15 docker inspect \
        --format '{{range .Mounts}}{{if eq .Destination "/run/klove-secrets"}}{{.Name}}:{{.RW}}{{end}}{{end}}' \
        "$ratos_contract_target")
    ratos_contract_actual_mount_count=$(timeout 15 docker inspect \
        --format '{{len .Mounts}}' "$ratos_contract_target")
    case "$ratos_contract_role" in
        moonraker-proxy)
            ratos_expected_contract_secret_mount=
            ratos_contract_pids=32
            ratos_contract_memory=100663296
            ratos_contract_nano_cpus=500000000
            ratos_contract_mount_count=0
            ratos_contract_entrypoint=null
            ratos_contract_command='["/usr/local/bin/python","/opt/klove-ratos/contract/proxy.py"]'
            ;;
        contract-runner | production-klove)
            ratos_expected_contract_secret_mount="$ratos_secret_volume:false"
            ratos_contract_mount_count=1
            if [ "$ratos_contract_role" = contract-runner ]; then
                ratos_contract_pids=24
                ratos_contract_memory=100663296
                ratos_contract_nano_cpus=500000000
                ratos_contract_entrypoint=null
                ratos_contract_command='["/usr/local/bin/python","/opt/klove-ratos/contract/exercise_contract.py"]'
            else
                ratos_contract_pids=64
                ratos_contract_memory=268435456
                ratos_contract_nano_cpus=1000000000
                ratos_contract_entrypoint='["klove"]'
                ratos_contract_command='["--config","/run/klove-secrets/config.toml"]'
            fi
            ;;
        *)
            echo "unknown RatOS contract container role" >&2
            exit 1
            ;;
    esac
    if [ "$ratos_contract_actual_image" != "$ratos_contract_image" ] \
        || [ "$ratos_contract_label_state" != "$ratos_state_dir" ] \
        || [ "$ratos_contract_label_source" != "$ratos_stored_source_digest" ] \
        || [ "$ratos_contract_label_role" != "$ratos_contract_role" ] \
        || [ "$ratos_contract_network" != "container:$ratos_qemu_id" ] \
        || [ "$ratos_contract_actual_mount_count" != "$ratos_contract_mount_count" ] \
        || [ "$ratos_contract_secret_mount" != "$ratos_expected_contract_secret_mount" ]; then
        echo "RatOS contract container identity or network is unexpected" >&2
        exit 1
    fi
    if [ "$ratos_contract_mount_count" -eq 1 ]; then
        ratos_require_mount "$ratos_contract_target" /run/klove-secrets volume \
            "$ratos_secret_volume" false
    fi
}

ratos_require_contract_container() {
    ratos_contract_target=$1
    ratos_contract_image=$2
    ratos_contract_role=$3
    ratos_require_contract_container_identity \
        "$ratos_contract_target" "$ratos_contract_image" "$ratos_contract_role"
    ratos_require_container_confinement \
        "$ratos_contract_target" "container:$ratos_qemu_id" \
        "$ratos_contract_pids" "$ratos_contract_memory" "$ratos_contract_nano_cpus" \
        "$ratos_contract_mount_count" 10001:10001 512k 2
    ratos_require_container_command \
        "$ratos_contract_target" "$ratos_contract_entrypoint" "$ratos_contract_command"
}

ratos_run_prepare_tool() {
    timeout 1200 docker run --rm \
        --network none \
        --read-only \
        --cap-drop ALL \
        --security-opt no-new-privileges \
        --pids-limit 64 \
        --memory 1g \
        --cpus 2 \
        --tmpfs /tmp:rw,noexec,nosuid,size=64m \
        --user 0:0 \
        --workdir /work \
        --mount "type=bind,source=$ratos_state_dir,target=/work" \
        "$ratos_tool_image_id" \
        /usr/local/bin/python /opt/klove-ratos/tool.py prepare
}

ratos_run_overlay_tool() {
    ratos_overlay_command=$1
    timeout 1200 docker run --rm \
        --network none \
        --read-only \
        --cap-drop ALL \
        --security-opt no-new-privileges \
        --pids-limit 64 \
        --memory 1g \
        --cpus 2 \
        --tmpfs /tmp:rw,noexec,nosuid,size=64m \
        --user 0:0 \
        --mount "type=bind,source=$ratos_state_dir/$ratos_raw_name,target=/inputs/$ratos_raw_name,readonly" \
        --mount "type=bind,source=$ratos_state_dir/extracted/kernel8.Image,target=/inputs/kernel8.Image,readonly" \
        --mount "type=bind,source=$ratos_state_dir/extracted/bcm2710-rpi-3-b.dtb,target=/inputs/bcm2710-rpi-3-b.dtb,readonly" \
        --mount "type=bind,source=$ratos_runtime_dir,target=/run-state" \
        "$ratos_tool_image_id" \
        /usr/local/bin/python /opt/klove-ratos/tool.py "$ratos_overlay_command"
}

ratos_archive_runtime() {
    ratos_run_outcome=$1
    ratos_validate_state_tree
    ratos_require_private_file "$ratos_origin" 600
    ratos_require_private_file "$ratos_state_dir/identities.json" 600
    ratos_require_private_file "$ratos_runtime_dir/$ratos_overlay_name" 666
    for ratos_runtime_file in \
        active contract-prepared.json contract-prepare-failure.json contract-succeeded contract.json \
        firstboot-required firstboot-restarted probe-succeeded probe.json
    do
        if [ -e "$ratos_runtime_dir/$ratos_runtime_file" ] \
            || [ -L "$ratos_runtime_dir/$ratos_runtime_file" ]; then
            ratos_require_private_file "$ratos_runtime_dir/$ratos_runtime_file" 600
        fi
    done

    ratos_archive_time=$(date -u +%Y%m%dT%H%M%SZ)
    ratos_archived_run=$(mktemp -d "$ratos_evidence_dir/run-$ratos_archive_time.XXXXXX")
    chmod 0700 "$ratos_archived_run"

    cp -- "$ratos_origin" "$ratos_archived_run/origin"
    cp -- "$ratos_state_dir/identities.json" "$ratos_archived_run/identities.json"
    chmod 0600 "$ratos_archived_run/origin" "$ratos_archived_run/identities.json"
    ratos_origin_sha=$(sha256sum -- "$ratos_archived_run/origin")
    ratos_origin_sha=${ratos_origin_sha%% *}
    ratos_identities_sha=$(sha256sum -- "$ratos_archived_run/identities.json")
    ratos_identities_sha=${ratos_identities_sha%% *}

    ratos_overlay_sha=$(sha256sum -- "$ratos_runtime_dir/$ratos_overlay_name")
    ratos_overlay_sha=${ratos_overlay_sha%% *}
    rm -f -- "$ratos_runtime_dir/$ratos_overlay_name"
    ratos_require_absent "$ratos_runtime_dir/$ratos_overlay_name"

    for ratos_runtime_file in \
        active contract-prepared.json contract-prepare-failure.json contract-succeeded contract.json \
        firstboot-required firstboot-restarted probe-succeeded probe.json
    do
        if [ -e "$ratos_runtime_dir/$ratos_runtime_file" ]; then
            mv -- "$ratos_runtime_dir/$ratos_runtime_file" \
                "$ratos_archived_run/$ratos_runtime_file"
        fi
    done

    ratos_checksums=$(mktemp "$ratos_archived_run/checksums.XXXXXX")
    for ratos_evidence_file in \
        active contract-prepared.json contract-prepare-failure.json contract-succeeded contract.json identities.json \
        firstboot-required firstboot-restarted origin probe-succeeded probe.json
    do
        if [ -e "$ratos_archived_run/$ratos_evidence_file" ]; then
            ratos_evidence_sha=$(sha256sum -- "$ratos_archived_run/$ratos_evidence_file")
            ratos_evidence_sha=${ratos_evidence_sha%% *}
            printf '%s  %s\n' "$ratos_evidence_sha" "$ratos_evidence_file" \
                >> "$ratos_checksums"
        fi
    done
    chmod 0600 "$ratos_checksums"
    mv -- "$ratos_checksums" "$ratos_archived_run/SHA256SUMS"
    ratos_checksums_sha=$(sha256sum -- "$ratos_archived_run/SHA256SUMS")
    ratos_checksums_sha=${ratos_checksums_sha%% *}
    ratos_manifest=$(mktemp "$ratos_archived_run/manifest.XXXXXX")
    printf 'outcome=%s\noverlay_retained=false\noverlay_sha256=%s\norigin_sha256=%s\nidentities_sha256=%s\nchecksums_sha256=%s\n' \
        "$ratos_run_outcome" "$ratos_overlay_sha" \
        "$ratos_origin_sha" "$ratos_identities_sha" "$ratos_checksums_sha" \
        > "$ratos_manifest"
    chmod 0600 "$ratos_manifest"
    mv -- "$ratos_manifest" "$ratos_archived_run/manifest"
}
