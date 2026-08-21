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
                ratos_contract_command='["/usr/local/bin/python","/opt/klove-ratos/contract/ratos_exercise_contract.py"]'
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

