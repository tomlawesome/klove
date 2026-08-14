#!/usr/bin/env sh

ratos_asset_name=2026-03-04-RatOS-2.1.0-raspberry-rpi32.img.xz
ratos_raw_name=2026-03-04-RatOS-2.1.0-raspberry-rpi32.img
ratos_asset_bytes=2125243800
ratos_asset_sha256=513465cf6b233d73e5c9ed048568493894d77c7c67e93586609f9b7fbc182153
ratos_tool_tag=klove-ratos-qemu:v2.1.0-spike
ratos_container=klove-ratos-v2-1-0
ratos_overlay_name=ratos-rpi32-run.qcow2

ratos_require_host_tools() {
    for ratos_command in \
        basename cat chmod cp date df docker git grep id mkdir mktemp mv rm sed sha256sum \
        sleep stat tail timeout tr wc
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
    chmod 0600 "$ratos_source_manifest"
    for ratos_source_path in \
        scripts/ratos-emulation-down.sh \
        scripts/ratos-emulation-lib.sh \
        scripts/ratos-emulation-prepare.sh \
        scripts/ratos-emulation-probe.sh \
        scripts/ratos-emulation-up.sh \
        scripts/test-ratos-emulation.sh \
        tests/integration/ratos-emulation/Dockerfile \
        tests/integration/ratos-emulation/debian.sources \
        tests/integration/ratos-emulation/tool.py
    do
        ratos_source_file="$repo_root/$ratos_source_path"
        if [ ! -f "$ratos_source_file" ] || [ -L "$ratos_source_file" ]; then
            rm -f -- "$ratos_source_manifest"
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
    done
    ratos_source_digest=$(sha256sum -- "$ratos_source_manifest")
    ratos_source_digest=${ratos_source_digest%% *}
    rm -f -- "$ratos_source_manifest"
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
    if [ "$(wc -l < "$ratos_origin_file")" -ne 5 ]; then
        echo "RatOS emulation origin is malformed; run prepare first" >&2
        exit 1
    fi
    ratos_stored_context=$(sed -n '1p' "$ratos_origin_file")
    ratos_stored_daemon_id=$(sed -n '2p' "$ratos_origin_file")
    ratos_tool_image_id=$(sed -n '3p' "$ratos_origin_file")
    ratos_stored_source_revision=$(sed -n '4p' "$ratos_origin_file")
    ratos_stored_source_digest=$(sed -n '5p' "$ratos_origin_file")
}

ratos_require_replaceable_origin() {
    ratos_origin_file=$1
    ratos_validate_state_tree
    ratos_require_private_file "$ratos_origin_file" 600
    ratos_origin_lines=$(wc -l < "$ratos_origin_file")
    if [ "$ratos_origin_lines" -ne 4 ] && [ "$ratos_origin_lines" -ne 5 ]; then
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

ratos_require_origin() {
    ratos_origin_file=$1
    ratos_read_origin "$ratos_origin_file"
    ratos_capture_daemon
    if [ "$ratos_context" != "$ratos_stored_context" ] \
        || [ "$ratos_daemon_id" != "$ratos_stored_daemon_id" ]; then
        echo "RatOS emulation state belongs to a different Docker context or daemon" >&2
        exit 1
    fi
    ratos_capture_source_state
    if [ "$ratos_source_revision" != "$ratos_stored_source_revision" ] \
        || [ "$ratos_source_digest" != "$ratos_stored_source_digest" ]; then
        echo "RatOS emulation state belongs to different lane source" >&2
        exit 1
    fi
    if ! timeout 15 docker image inspect "$ratos_tool_image_id" >/dev/null 2>&1; then
        echo "the exact recorded RatOS emulator image is unavailable" >&2
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
}

ratos_require_active() {
    ratos_active_file=$1
    ratos_require_private_file "$ratos_active_file" 600
    if [ "$(wc -l < "$ratos_active_file")" -ne 3 ]; then
        echo "RatOS emulator active state is malformed" >&2
        exit 1
    fi
    ratos_active_container=$(sed -n '1p' "$ratos_active_file")
    ratos_active_image_id=$(sed -n '2p' "$ratos_active_file")
    ratos_active_state_dir=$(sed -n '3p' "$ratos_active_file")
    if [ "$ratos_active_container" != "$ratos_container" ] \
        || [ "$ratos_active_image_id" != "$ratos_tool_image_id" ] \
        || [ "$ratos_active_state_dir" != "$ratos_state_dir" ]; then
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
    if [ "$ratos_actual_image" != "$ratos_tool_image_id" ] \
        || [ "$ratos_label_state" != "$ratos_state_dir" ] \
        || [ "$ratos_label_image" != "$ratos_tool_image_id" ]; then
        echo "RatOS emulator image or labels do not match the guarded local state" >&2
        exit 1
    fi
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
        active firstboot-required firstboot-restarted probe-succeeded probe.json
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

    mv -- "$ratos_runtime_dir/$ratos_overlay_name" "$ratos_archived_run/overlay.qcow2"
    chmod 0600 "$ratos_archived_run/overlay.qcow2"
    ratos_overlay_sha=$(sha256sum -- "$ratos_archived_run/overlay.qcow2")
    ratos_overlay_sha=${ratos_overlay_sha%% *}
    ratos_manifest=$(mktemp "$ratos_archived_run/manifest.XXXXXX")
    printf 'outcome=%s\noverlay_sha256=%s\norigin_sha256=%s\nidentities_sha256=%s\n' \
        "$ratos_run_outcome" "$ratos_overlay_sha" \
        "$ratos_origin_sha" "$ratos_identities_sha" > "$ratos_manifest"
    chmod 0600 "$ratos_manifest"
    mv -- "$ratos_manifest" "$ratos_archived_run/manifest"

    for ratos_runtime_file in \
        active firstboot-required firstboot-restarted probe-succeeded probe.json
    do
        if [ -e "$ratos_runtime_dir/$ratos_runtime_file" ]; then
            mv -- "$ratos_runtime_dir/$ratos_runtime_file" \
                "$ratos_archived_run/$ratos_runtime_file"
        fi
    done
}
