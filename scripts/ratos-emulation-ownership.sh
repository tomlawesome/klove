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
        scripts/ratos-emulation-evidence.sh \
        scripts/ratos-emulation-lib.sh \
        scripts/ratos-emulation-ownership.sh \
        scripts/ratos-emulation-prepare.sh \
        scripts/ratos-emulation-probe.sh \
        scripts/ratos-emulation-qemu.sh \
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
