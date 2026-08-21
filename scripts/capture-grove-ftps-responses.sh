#!/usr/bin/env sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: $0 RUN_ID" >&2
    exit 2
fi

run_id=$1
script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
# shellcheck disable=SC1091 # Resolved relative to this script at runtime.
. "$script_dir/grove-observation-lib.sh"
grove_observation_validate_run_id "$run_id"

repo_root=$(CDPATH='' cd -- "$script_dir/.." && pwd)
grove_name="klove-grove-observation-$run_id"
recorder_name="klove-ftps-response-$run_id"
state_dir="$repo_root/.klove-integration/grove-observation/$run_id"
recorder_origin="$state_dir/ftps-recorder-origin"
fixture="$repo_root/tests/fixtures/grove-observations/ftps-server-response-profile"
image="grove-observer:cdf6b829"
recorder_started=0
grove_started=0
evidence_dir=

cleanup_recorder() {
    if [ "$recorder_started" -ne 1 ]; then
        return 0
    fi
    if [ ! -f "$recorder_origin" ]; then
        echo "FTPS recorder recovery state is missing" >&2
        return 1
    fi
    IFS='|' read -r stored_context stored_daemon stored_name stored_id < "$recorder_origin"
    current_context=$(timeout 15 docker context show)
    current_daemon=$(timeout 15 docker info --format '{{.ID}}')
    if [ "$stored_context" != "$current_context" ] \
        || [ "$stored_daemon" != "$current_daemon" ] \
        || [ "$stored_name" != "$recorder_name" ]; then
        echo "FTPS recorder belongs to a different Docker context or daemon" >&2
        return 1
    fi
    actual_id=$(timeout 15 docker inspect --format '{{.Id}}' "$recorder_name")
    actual_label=$(timeout 15 docker inspect \
        --format '{{index .Config.Labels "io.klove.grove-observation.run-id"}}' \
        "$recorder_name")
    actual_role=$(timeout 15 docker inspect \
        --format '{{index .Config.Labels "io.klove.grove-observation.role"}}' \
        "$recorder_name")
    actual_image=$(timeout 15 docker inspect --format '{{.Config.Image}}' "$recorder_name")
    if [ "$actual_id" != "$stored_id" ] \
        || [ "$actual_label" != "$run_id" ] \
        || [ "$actual_role" != "ftps-response-recorder" ] \
        || [ "$actual_image" != "$image" ]; then
        echo "FTPS recorder identity changed; refusing cleanup" >&2
        return 1
    fi
    timeout 30 docker rm --force "$recorder_name" >/dev/null
    rm -f -- "$recorder_origin"
    recorder_started=0
}

cleanup() {
    status=$?
    trap - EXIT HUP INT TERM
    cleanup_ok=1
    if ! cleanup_recorder; then
        cleanup_ok=0
    fi
    if [ -n "$evidence_dir" ] && [ -d "$evidence_dir" ]; then
        rm -f -- "$evidence_dir/ftps-server-response-profile" \
            "$evidence_dir/ftps-server-response-profile.tmp"
        rmdir -- "$evidence_dir" 2>/dev/null || cleanup_ok=0
    fi
    if [ "$grove_started" -eq 1 ]; then
        if "$script_dir/grove-observation-down.sh" "$run_id"; then
            grove_started=0
        else
            cleanup_ok=0
        fi
    fi
    if [ "$cleanup_ok" -ne 1 ]; then
        echo "FTPS response observation cleanup needs recovery for RUN_ID: $run_id" >&2
        exit 1
    fi
    exit "$status"
}
trap cleanup EXIT HUP INT TERM

"$script_dir/grove-observation-up.sh" "$run_id"
grove_started=1
grove_observation_require_origin "$state_dir/origin"
grove_observation_require_name "$run_id"

evidence_dir=$(mktemp -d "$state_dir/ftps-response.XXXXXX")
chmod 700 "$evidence_dir"
grove_observation_capture_daemon

timeout 60 docker run --detach --name "$recorder_name" --network "$grove_name" \
    --user 0:0 --cap-drop ALL --cap-add NET_BIND_SERVICE \
    --security-opt no-new-privileges --read-only --tmpfs /tmp:rw,noexec,nosuid,size=4m \
    --memory 128m --cpus 0.5 --pids-limit 64 \
    --label "io.klove.grove-observation.run-id=$run_id" \
    --label "io.klove.grove-observation.role=ftps-response-recorder" \
    --volume "$script_dir/grove-ftps-response-recorder.py:/opt/recorder.py:ro" \
    --volume "$evidence_dir:/evidence:rw" \
    --entrypoint sh "$image" -c \
    'openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=observation.invalid -keyout /tmp/observation-key.pem -out /tmp/observation-cert.pem >/dev/null 2>&1 && exec python /opt/recorder.py' \
    >/dev/null
recorder_id=$(timeout 15 docker inspect --format '{{.Id}}' "$recorder_name")
if [ -z "$recorder_id" ]; then
    echo "FTPS recorder identity is unavailable" >&2
    exit 1
fi
umask 077
# shellcheck disable=SC2154 # Assigned by grove_observation_capture_daemon.
printf '%s|%s|%s|%s\n' "$grove_observation_context" "$grove_observation_daemon_id" \
    "$recorder_name" "$recorder_id" > "$recorder_origin"
recorder_started=1

recorder_ip=$(timeout 15 docker inspect \
    --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$recorder_name")
case "$recorder_ip" in
    *[!0-9.]* | "")
        echo "FTPS recorder network IP is unavailable" >&2
        exit 1
        ;;
esac

timeout 90 docker exec --interactive "$grove_name" \
    python - "$recorder_ip" < "$script_dir/grove-ftps-response-drive.py"
recorder_status=$(timeout 90 docker wait "$recorder_name")
if [ "$recorder_status" != "0" ]; then
    timeout 15 docker logs --tail 10 "$recorder_name" >&2
    echo "FTPS recorder did not complete successfully" >&2
    exit 1
fi
if [ ! -f "$evidence_dir/ftps-server-response-profile" ] \
    || [ -L "$evidence_dir/ftps-server-response-profile" ]; then
    echo "FTPS recorder evidence is unavailable" >&2
    exit 1
fi
python3 - "$evidence_dir/ftps-server-response-profile" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
if path.stat().st_size > 16 * 1024:
    raise SystemExit("FTPS recorder evidence exceeds bound")
document = json.loads(path.read_text(encoding="ascii"))
if document.get("observed", {}).get("successful_client_flow") is not True:
    raise SystemExit("FTPS recorder evidence is incomplete")
PY
cp -- "$evidence_dir/ftps-server-response-profile" "$fixture"
chmod 644 "$fixture"
echo "Sanitized FTPS response observation captured: $run_id"
