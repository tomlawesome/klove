#!/usr/bin/env sh

grove_observation_validate_run_id() {
    candidate=$1
    case "$candidate" in
        *[!a-z0-9-]* | "" | -* | *-)
            echo "RUN_ID must contain 1-32 lowercase letters, digits, or interior hyphens" >&2
            exit 2
            ;;
    esac
    if [ "${#candidate}" -gt 32 ]; then
        echo "RUN_ID must contain at most 32 characters" >&2
        exit 2
    fi
}

grove_observation_capture_daemon() {
    if ! command -v timeout >/dev/null 2>&1; then
        echo "Grove observation requires the coreutils timeout command" >&2
        exit 1
    fi
    grove_observation_context=$(timeout 15 docker context show)
    grove_observation_daemon_id=$(timeout 15 docker info --format '{{.ID}}')
    grove_observation_security_options=$(timeout 15 docker info --format '{{json .SecurityOptions}}')
    if [ -z "$grove_observation_context" ] || [ -z "$grove_observation_daemon_id" ]; then
        echo "Docker context or daemon identity is unavailable" >&2
        exit 1
    fi
    case "$grove_observation_security_options" in
        *name=rootless*) grove_observation_daemon_mode=rootless ;;
        *)
            echo "Grove observation requires rootless Docker" >&2
            exit 1
            ;;
    esac
}

grove_observation_write_origin() {
    origin_file=$1
    network_name=$2
    network_id=$3
    container_name=$4
    container_id=$5
    temporary_file="$origin_file.tmp"
    umask 077
    printf '%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n' \
        "1" \
        "$grove_observation_context" \
        "$grove_observation_daemon_id" \
        "$grove_observation_daemon_mode" \
        "$network_name" \
        "$network_id" \
        "$container_name" \
        "$container_id" \
        "grove-observer:cdf6b829" > "$temporary_file"
    mv -- "$temporary_file" "$origin_file"
}

grove_observation_read_origin() {
    origin_file=$1
    if [ ! -f "$origin_file" ] || [ "$(wc -l < "$origin_file")" -ne 9 ]; then
        echo "Grove observation state is missing or malformed" >&2
        exit 1
    fi
    stored_format=$(sed -n '1p' "$origin_file")
    stored_context=$(sed -n '2p' "$origin_file")
    stored_daemon_id=$(sed -n '3p' "$origin_file")
    stored_daemon_mode=$(sed -n '4p' "$origin_file")
    stored_network_name=$(sed -n '5p' "$origin_file")
    stored_network_id=$(sed -n '6p' "$origin_file")
    stored_container_name=$(sed -n '7p' "$origin_file")
    stored_container_id=$(sed -n '8p' "$origin_file")
    stored_image=$(sed -n '9p' "$origin_file")
    if [ "$stored_format" != "1" ] \
        || [ "$stored_daemon_mode" != "rootless" ] \
        || [ "$stored_image" != "grove-observer:cdf6b829" ]; then
        echo "Grove observation state is malformed" >&2
        exit 1
    fi
}

grove_observation_require_origin() {
    origin_file=$1
    grove_observation_read_origin "$origin_file"
    grove_observation_capture_daemon
    if [ "$grove_observation_context" != "$stored_context" ] \
        || [ "$grove_observation_daemon_id" != "$stored_daemon_id" ]; then
        echo "Grove observation belongs to a different Docker context or daemon" >&2
        exit 1
    fi
}

grove_observation_require_name() {
    run_id=$1
    expected_name="klove-grove-observation-$run_id"
    if [ "$stored_network_name" != "$expected_name" ] \
        || [ "$stored_container_name" != "$expected_name" ]; then
        echo "Grove observation state does not match RUN_ID" >&2
        exit 1
    fi
}

grove_observation_remove_recorded() {
    run_id=$1
    actual_container_id=$(timeout 15 docker inspect --format '{{.Id}}' "$stored_container_name" \
        2>/dev/null || true)
    if [ -n "$actual_container_id" ]; then
        actual_container_run_id=$(timeout 15 docker inspect \
            --format '{{ index .Config.Labels "io.klove.grove-observation.run-id" }}' \
            "$stored_container_name" 2>/dev/null || true)
        actual_container_image=$(timeout 15 docker inspect --format '{{.Config.Image}}' \
            "$stored_container_name" 2>/dev/null || true)
        if { [ "$stored_container_id" != "-" ] \
            && [ "$actual_container_id" != "$stored_container_id" ]; } \
            || [ "$actual_container_run_id" != "$run_id" ] \
            || [ "$actual_container_image" != "$stored_image" ]; then
            echo "recorded Grove observation container is absent or does not match state" >&2
            return 1
        fi
        timeout 30 docker rm --force "$stored_container_name" >/dev/null
    elif [ "$stored_container_id" != "-" ]; then
        echo "recorded Grove observation container is absent or does not match state" >&2
        return 1
    fi
    actual_network_id=$(timeout 15 docker network inspect --format '{{.Id}}' \
        "$stored_network_name" 2>/dev/null || true)
    if [ -n "$actual_network_id" ]; then
        actual_network_run_id=$(timeout 15 docker network inspect \
            --format '{{ index .Labels "io.klove.grove-observation.run-id" }}' \
            "$stored_network_name" 2>/dev/null || true)
        if { [ "$stored_network_id" != "-" ] \
            && [ "$actual_network_id" != "$stored_network_id" ]; } \
            || [ "$actual_network_run_id" != "$run_id" ]; then
            echo "recorded Grove observation network is absent or does not match state" >&2
            return 1
        fi
        timeout 30 docker network rm "$stored_network_name" >/dev/null
    elif [ "$stored_network_id" != "-" ]; then
        echo "recorded Grove observation network is absent or does not match state" >&2
        return 1
    fi
}
