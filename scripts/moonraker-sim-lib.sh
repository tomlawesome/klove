#!/usr/bin/env sh

moonraker_sim_validate_run_id() {
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

moonraker_sim_capture_daemon() {
    if ! command -v timeout >/dev/null 2>&1; then
        echo "moonraker simulation requires the coreutils timeout command" >&2
        exit 1
    fi
    sim_context=$(timeout 15 docker context show)
    sim_daemon_id=$(timeout 15 docker info --format '{{.ID}}')
    sim_security_options=$(timeout 15 docker info --format '{{json .SecurityOptions}}')
    if [ -z "$sim_context" ] || [ -z "$sim_daemon_id" ]; then
        echo "Docker context or daemon identity is unavailable" >&2
        exit 1
    fi
    case "$sim_security_options" in
        *name=rootless*) sim_daemon_mode=rootless ;;
        *) sim_daemon_mode=rootful ;;
    esac
}

moonraker_sim_require_origin() {
    origin_file=$1
    if [ ! -f "$origin_file" ] || [ "$(wc -l < "$origin_file")" -ne 4 ]; then
        echo "integration state is missing or malformed" >&2
        exit 1
    fi
    stored_project=$(sed -n '1p' "$origin_file")
    stored_context=$(sed -n '2p' "$origin_file")
    stored_daemon_id=$(sed -n '3p' "$origin_file")
    stored_daemon_mode=$(sed -n '4p' "$origin_file")

    moonraker_sim_capture_daemon
    if [ "$sim_context" != "$stored_context" ] || [ "$sim_daemon_id" != "$stored_daemon_id" ]; then
        echo "integration run belongs to a different Docker context or daemon" >&2
        exit 1
    fi
    case "$stored_daemon_mode:$sim_daemon_mode" in
        rootless:rootless) ;;
        rootful-ci:rootful)
            if [ "${CI:-}" != "true" ] || [ "${KLOVE_SIM_ALLOW_ROOTFUL_CI:-}" != "1" ]; then
                echo "rootful integration state may be used only by explicit CI" >&2
                exit 1
            fi
            ;;
        *)
            echo "integration daemon mode does not match its recorded origin" >&2
            exit 1
            ;;
    esac
}
