#!/usr/bin/env sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: $0 RUN_ID" >&2
    exit 2
fi

run_id=$1
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$script_dir/grove-observation-lib.sh"
grove_observation_validate_run_id "$run_id"

repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
state_dir="$repo_root/.klove-integration/grove-observation/$run_id"
origin_file="$state_dir/origin"
resource_name="klove-grove-observation-$run_id"
image="grove-observer:cdf6b829"

if [ -e "$origin_file" ]; then
    echo "Grove observation run already exists: $run_id" >&2
    exit 1
fi

grove_observation_capture_daemon
umask 077
mkdir -p "$state_dir"
grove_observation_write_origin "$origin_file" "$resource_name" - "$resource_name" -

cleanup_failed_start() {
    if grove_observation_require_origin "$origin_file" \
        && grove_observation_require_name "$run_id" \
        && grove_observation_remove_recorded "$run_id"; then
        rm -f -- "$origin_file"
        rmdir -- "$state_dir" 2>/dev/null || true
    else
        echo "Grove observation cleanup failed; retained recovery state for RUN_ID: $run_id" >&2
    fi
}
trap cleanup_failed_start EXIT HUP INT TERM

timeout 30 docker network create --internal \
    --label "io.klove.grove-observation.run-id=$run_id" \
    "$resource_name" >/dev/null
network_id=$(timeout 15 docker network inspect --format '{{.Id}}' "$resource_name")
if [ -z "$network_id" ]; then
    echo "Grove observation network identity is unavailable" >&2
    exit 1
fi
grove_observation_write_origin "$origin_file" "$resource_name" "$network_id" "$resource_name" -

timeout 60 docker run --detach --name "$resource_name" --network "$resource_name" \
    --user 0:0 --cap-drop ALL --cap-add NET_BIND_SERVICE \
    --security-opt no-new-privileges \
    --memory 512m --cpus 1 --pids-limit 256 \
    --label "io.klove.grove-observation.run-id=$run_id" \
    --entrypoint sh "$image" -c \
    'python -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --loop asyncio' \
    >/dev/null
container_id=$(timeout 15 docker inspect --format '{{.Id}}' "$resource_name")
if [ -z "$container_id" ]; then
    echo "Grove observation container identity is unavailable" >&2
    exit 1
fi
grove_observation_write_origin \
    "$origin_file" "$resource_name" "$network_id" "$resource_name" "$container_id"

container_ip=$(timeout 15 docker inspect \
    --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$resource_name")
case "$container_ip" in
    *[!0-9.]* | "")
        echo "Grove observation container network IP is unavailable" >&2
        exit 1
        ;;
esac

ready=0
attempt=0
while [ "$attempt" -lt 60 ]; do
    if timeout 5 docker exec "$resource_name" curl --fail --silent --show-error \
        --max-time 3 http://127.0.0.1:8000/health >/dev/null 2>&1; then
        ready=1
        break
    fi
    attempt=$((attempt + 1))
    sleep 1
done
if [ "$ready" -ne 1 ]; then
    echo "Grove observation health check timed out" >&2
    exit 1
fi

if ! printf '%s' '{"auth_enabled":false}' | timeout 15 docker exec --interactive \
    "$resource_name" curl --fail --silent --show-error --max-time 10 --request POST \
    --header 'Content-Type: application/json' --data-binary @- \
    http://127.0.0.1:8000/api/v1/auth/setup >/dev/null; then
    echo "Grove observation setup failed" >&2
    exit 1
fi

access_code=TEST0000
if ! printf '%s' \
    "{\"name\":\"Klove Observation\",\"enabled\":true,\"mode\":\"archive\",\"model\":\"BL-P001\",\"access_code\":\"$access_code\",\"auto_dispatch\":false,\"queue_force_color_match\":false,\"gcode_injection\":false,\"bind_ip\":\"$container_ip\"}" \
    | timeout 15 docker exec --interactive "$resource_name" curl --fail --silent \
        --show-error --max-time 10 --request POST --header 'Content-Type: application/json' \
        --data-binary @- http://127.0.0.1:8000/api/v1/virtual-printers >/dev/null; then
    echo "Grove observation virtual-printer registration failed" >&2
    exit 1
fi
unset access_code

running=0
attempt=0
while [ "$attempt" -lt 60 ]; do
    if timeout 10 docker exec "$resource_name" curl --fail --silent --show-error \
        --max-time 5 http://127.0.0.1:8000/api/v1/virtual-printers 2>/dev/null \
        | python3 -c '
import json
import sys

def entries(value):
    if isinstance(value, list):
        yield from value
    elif isinstance(value, dict):
        yield value
        for child in value.values():
            yield from entries(child)

try:
    document = json.load(sys.stdin)
except (json.JSONDecodeError, RecursionError):
    raise SystemExit(1)
raise SystemExit(
    0 if any(
        isinstance(entry, dict)
        and entry.get("name") == "Klove Observation"
        and isinstance(entry.get("status"), dict)
        and entry["status"].get("running") is True
        for entry in entries(document)
    ) else 1
)
'; then
        running=1
        break
    fi
    attempt=$((attempt + 1))
    sleep 1
done
if [ "$running" -ne 1 ]; then
    echo "Grove observation virtual printer did not reach running" >&2
    exit 1
fi

trap - EXIT HUP INT TERM
echo "Grove observation ready: $run_id"
