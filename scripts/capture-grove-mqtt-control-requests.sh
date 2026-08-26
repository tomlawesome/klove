#!/usr/bin/env sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: $0 RUN_ID" >&2
    exit 2
fi

run_id=$1
script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
. "$script_dir/grove-observation-lib.sh"
grove_observation_validate_run_id "$run_id"

repo_root=$(CDPATH='' cd -- "$script_dir/.." && pwd)
grove_name="klove-grove-observation-$run_id"
recorder_name="klove-mqtt-control-request-$run_id"
state_dir="$repo_root/.klove-integration/grove-observation/$run_id"
recorder_origin="$state_dir/mqtt-control-request-recorder-origin"
image="grove-observer:cdf6b829"
grove_source_revision="cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4"
recorder_started=0
grove_started=0
evidence_dir=
host_diagnostics_dir=

write_recorder_origin() {
    temporary_origin="$recorder_origin.tmp"
    umask 077
    printf '%s|%s|%s|%s\n' "$grove_observation_context" "$grove_observation_daemon_id" \
        "$recorder_name" "$1" > "$temporary_origin"
    mv -- "$temporary_origin" "$recorder_origin"
}

cleanup_recorder() {
    if [ "$recorder_started" -ne 1 ]; then return 0; fi
    if [ ! -f "$recorder_origin" ]; then return 1; fi
    IFS='|' read -r stored_context stored_daemon stored_name stored_id < "$recorder_origin"
    if [ "$stored_id" = "-" ] || [ "$stored_name" != "$recorder_name" ]; then return 1; fi
    grove_observation_capture_daemon
    if [ "$stored_context" != "$grove_observation_context" ] \
        || [ "$stored_daemon" != "$grove_observation_daemon_id" ]; then return 1; fi
    actual_id=$(timeout 15 docker inspect --format '{{.Id}}' "$recorder_name" 2>/dev/null) || return 1
    actual_label=$(timeout 15 docker inspect --format '{{index .Config.Labels "io.klove.grove-observation.run-id"}}' "$recorder_name" 2>/dev/null) || return 1
    actual_role=$(timeout 15 docker inspect --format '{{index .Config.Labels "io.klove.grove-observation.role"}}' "$recorder_name" 2>/dev/null) || return 1
    actual_image=$(timeout 15 docker inspect --format '{{.Config.Image}}' "$recorder_name" 2>/dev/null) || return 1
    if [ "$actual_id" != "$stored_id" ] || [ "$actual_label" != "$run_id" ] \
        || [ "$actual_role" != "mqtt-control-request-recorder" ] || [ "$actual_image" != "$image" ]; then
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
    cleanup_recorder || cleanup_ok=0
    if [ -n "$host_diagnostics_dir" ] && [ -d "$host_diagnostics_dir" ]; then
        if grove_observation_require_origin "$state_dir/origin" \
            && grove_observation_require_name "$run_id"; then
            rm -f -- "$host_diagnostics_dir/driver-status" \
                "$host_diagnostics_dir/driver-status.tmp"
            rmdir -- "$host_diagnostics_dir" 2>/dev/null || cleanup_ok=0
        else
            cleanup_ok=0
        fi
    fi
    if [ -n "$evidence_dir" ] && [ -d "$evidence_dir" ]; then
        rm -f -- "$evidence_dir/mqtt-control-request-schema" \
            "$evidence_dir/mqtt-control-request-schema.tmp" \
            "$evidence_dir/mqtt-control-request-recorder-provenance" \
            "$evidence_dir/mqtt-control-request-recorder-provenance.tmp" \
            "$evidence_dir/mqtt-control-request-status" \
            "$evidence_dir/mqtt-control-request-status.tmp" \
            "$evidence_dir/mqtt-control-request-driver-status" \
            "$evidence_dir/mqtt-control-request-driver-status.tmp" \
            "$evidence_dir/mqtt-control-request-ready" \
            "$evidence_dir/mqtt-control-request-ready.tmp"
        rmdir -- "$evidence_dir" 2>/dev/null || cleanup_ok=0
    fi
    if [ "$grove_started" -eq 1 ]; then
        "$script_dir/grove-observation-down.sh" "$run_id" || cleanup_ok=0
    fi
    if [ "$cleanup_ok" -ne 1 ]; then
        echo "MQTT control request observation cleanup needs recovery for RUN_ID: $run_id" >&2
        exit 1
    fi
    exit "$status"
}
trap cleanup EXIT HUP INT TERM

if ! harness_revision=$(git -C "$repo_root" rev-parse --verify 'HEAD^{commit}' 2>/dev/null); then
    echo "MQTT control capture harness revision is unavailable" >&2; exit 1
fi
if ! capture_dirty=$(git -C "$repo_root" status --porcelain --untracked-files=all -- \
    scripts/capture-grove-mqtt-control-requests.sh \
    scripts/grove-mqtt-control-request-recorder.py \
    scripts/grove-mqtt-control-request-drive.py \
    scripts/grove-mqtt-control-request-diagnostics.py \
    scripts/run-grove-mqtt-control-driver.py \
    scripts/grove-mqtt-initial-request-recorder.py \
    scripts/grove-observation-lib.sh scripts/grove-observation-up.sh \
    scripts/grove-observation-down.sh 2>/dev/null); then
    echo "MQTT control capture harness state is unavailable" >&2; exit 1
fi
if [ -n "$capture_dirty" ]; then
    echo "MQTT control capture harness is not committed" >&2; exit 1
fi
if ! docker_versions=$(timeout 15 docker version --format '{{.Client.Version}}|{{.Server.Version}}' 2>/dev/null); then
    echo "MQTT control capture Docker version is unavailable" >&2; exit 1
fi
if ! image_binding=$(timeout 15 docker image inspect --format '{{.Id}}|{{index .RepoDigests 0}}|{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image" 2>/dev/null); then
    echo "MQTT control capture image binding is unavailable" >&2; exit 1
fi
IFS='|' read -r grove_image_id grove_image_digest grove_image_source_revision <<EOF
$image_binding
EOF
if [ -z "$grove_image_id" ] || [ -z "$grove_image_digest" ] \
    || [ "$grove_image_source_revision" != "$grove_source_revision" ]; then
    echo "MQTT control capture binding is invalid" >&2; exit 1
fi

"$script_dir/grove-observation-up.sh" "$run_id"
grove_started=1
actual_grove_image_id=$(timeout 15 docker inspect --format '{{.Image}}' "$grove_name" 2>/dev/null) || {
    echo "MQTT control capture image identity changed" >&2; exit 1;
}
if [ "$actual_grove_image_id" != "$grove_image_id" ]; then
    echo "MQTT control capture image identity changed" >&2; exit 1
fi
grove_observation_require_origin "$state_dir/origin"
grove_observation_require_name "$run_id"
evidence_dir=$(mktemp -d "$state_dir/mqtt-control-request.XXXXXX")
chmod 700 "$evidence_dir"
grove_observation_capture_daemon
write_recorder_origin -
recorder_started=1

if ! timeout 60 docker run --detach --name "$recorder_name" --network "$grove_name" \
    --user 0:0 --cap-drop ALL --security-opt no-new-privileges --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,size=4m --memory 128m --cpus 0.5 --pids-limit 64 \
    --label "io.klove.grove-observation.run-id=$run_id" \
    --label "io.klove.grove-observation.role=mqtt-control-request-recorder" \
    --volume "$script_dir/grove-mqtt-control-request-recorder.py:/opt/control.py:ro" \
    --volume "$script_dir/grove-mqtt-initial-request-recorder.py:/opt/initial.py:ro" \
    --volume "$evidence_dir:/evidence:rw" --entrypoint sh "$image" -c \
    'openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=observation.invalid -keyout /tmp/observation-key.pem -out /tmp/observation-cert.pem >/dev/null 2>&1 || exit 64
case "$(uname -m)" in x86_64) loader=/lib64/ld-linux-x86-64.so.2 ;; aarch64) loader=/lib/ld-linux-aarch64.so.1 ;; *) exit 64 ;; esac
[ -x "$loader" ] || exit 64
KLOVE_INITIAL_RECORDER=/opt/initial.py exec "$loader" /usr/local/bin/python3.13 /opt/control.py' \
    >/dev/null 2>&1; then
    echo "MQTT control recorder bootstrap failed" >&2; exit 1
fi
recorder_id=$(timeout 15 docker inspect --format '{{.Id}}' "$recorder_name" 2>/dev/null) || {
    echo "MQTT control recorder bootstrap failed" >&2; exit 1;
}
case "$recorder_id" in '') echo "MQTT control recorder bootstrap failed" >&2; exit 1;; esac
write_recorder_origin "$recorder_id"
recorder_ip=$(timeout 15 docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$recorder_name" 2>/dev/null) || {
    echo "MQTT control recorder bootstrap failed" >&2; exit 1;
}
case "$recorder_ip" in *[!0-9.]*|'') echo "MQTT control recorder bootstrap failed" >&2; exit 1;; esac

ready_path="$evidence_dir/mqtt-control-request-ready"
validate_recorder_ready() {
    python3 - "$ready_path" <<'PY'
import json
import pathlib
import stat
import sys

path = pathlib.Path(sys.argv[1])
try:
    if not path.is_file() or path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError
    document = json.loads(path.read_text(encoding="ascii"))
    if type(document) is not dict or document != {"status": "ready"}:
        raise ValueError
except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
    raise SystemExit(1) from None
PY
}
attempt=0
while [ "$attempt" -lt 50 ]; do
    if [ -e "$ready_path" ] || [ -L "$ready_path" ]; then
        if ! validate_recorder_ready; then
            echo "MQTT control recorder ready marker is invalid" >&2; exit 1
        fi
        break
    fi
    running=$(timeout 15 docker inspect --format '{{.State.Running}}' "$recorder_name" 2>/dev/null) || {
        echo "MQTT control recorder bootstrap failed" >&2; exit 1;
    }
    if [ "$running" != true ]; then echo "MQTT control recorder bootstrap failed" >&2; exit 1; fi
    attempt=$((attempt + 1)); sleep 0.1
done
if [ ! -f "$ready_path" ] || [ -L "$ready_path" ]; then
    echo "MQTT control recorder readiness timed out" >&2; exit 1
fi

host_diagnostics_dir=$(mktemp -d "$state_dir/mqtt-control-host.XXXXXX")
chmod 700 "$host_diagnostics_dir"
driver_status_path="$host_diagnostics_dir/driver-status"
driver_status=0
if python3 "$script_dir/run-grove-mqtt-control-driver.py" \
    "$driver_status_path" "$grove_name" "$recorder_ip" \
    "$script_dir/grove-mqtt-control-request-drive.py"; then :; else driver_status=$?; fi
recorder_status=$(timeout 60 docker wait "$recorder_name" 2>/dev/null) || recorder_status=
case "$driver_status" in
    124) echo "MQTT_CONTROL_CAPTURE_DRIVER_TIMEOUT" >&2; exit 1;;
    125) echo "MQTT_CONTROL_CAPTURE_DRIVER_STATUS_OVERSIZE" >&2; exit 1;;
    126) echo "MQTT_CONTROL_CAPTURE_DRIVER_STATUS_INVALID" >&2; exit 1;;
esac
driver_code=$(python3 "$script_dir/grove-mqtt-control-request-diagnostics.py" \
    driver "$driver_status_path" "$driver_status") || {
    echo "MQTT_CONTROL_CAPTURE_DRIVER_STATUS_INVALID" >&2; exit 1;
}
if [ "$driver_code" != "MQTT_CONTROL_CAPTURE_DRIVER_COMPLETE" ]; then
    echo "$driver_code" >&2; exit 1
fi
case "$recorder_status" in
    '' | *[!0-9]*) echo "MQTT_CONTROL_CAPTURE_RECORDER_WAIT_INVALID" >&2; exit 1;;
esac
if [ "$recorder_status" -ne 0 ]; then
    recorder_code=$(python3 "$script_dir/grove-mqtt-control-request-diagnostics.py" \
        recorder "$evidence_dir/mqtt-control-request-status" "$recorder_status") || {
        echo "MQTT_CONTROL_CAPTURE_RECORDER_STATUS_INVALID" >&2; exit 1;
    }
    echo "$recorder_code" >&2; exit 1
fi
captured_at_utc=$(date -u '+%Y-%m-%dT%H:%M:%SZ') || {
    echo "MQTT control capture timestamp is unavailable" >&2; exit 1;
}
python3 - "$evidence_dir/mqtt-control-request-schema" \
    "$evidence_dir/mqtt-control-request-recorder-provenance" "$captured_at_utc" \
    "$harness_revision" "$docker_versions" "$grove_image_digest" "$grove_image_source_revision" \
    "$script_dir/grove-mqtt-control-request-recorder.py" <<'PY'
import importlib.util
import json
import pathlib
import re
import stat
import sys

forbidden = ("MQTTOBS0000001", "TEST0000", "packet_id", "BEGIN CERTIFICATE", "PRIVATE KEY")
schema_path, recorder_path = map(pathlib.Path, sys.argv[1:3])


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def reject_constant(_value):
    raise ValueError


for path in (schema_path, recorder_path):
    if not path.is_file() or path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise SystemExit(1)
    if not 0 < path.stat().st_size <= 32 * 1024:
        raise SystemExit(1)
spec = importlib.util.spec_from_file_location("control_observer", sys.argv[8])
if spec is None or spec.loader is None:
    raise SystemExit(1)
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)
try:
    candidate = observer.parse_candidate_json(schema_path.read_text(encoding="ascii"))
    recorder_tools = json.loads(
        recorder_path.read_text(encoding="ascii"),
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )
    observer.validate_candidate(candidate)
except Exception:
    raise SystemExit(1)
serialized = json.dumps(candidate, sort_keys=True)
if any(token in serialized for token in forbidden):
    raise SystemExit(1)
if (
    type(recorder_tools) is not dict
    or set(recorder_tools) != {"python", "openssl"}
    or type(recorder_tools["python"]) is not str
    or re.fullmatch(r"3\.13\.(?:0|[1-9][0-9]{0,2})", recorder_tools["python"]) is None
    or type(recorder_tools["openssl"]) is not str
    or re.fullmatch(r"OpenSSL 3\.[0-9]+\.[0-9]+", recorder_tools["openssl"]) is None
):
    raise SystemExit(1)
if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", sys.argv[3]) is None:
    raise SystemExit(1)
if re.fullmatch(r"[0-9a-f]{40}", sys.argv[4]) is None:
    raise SystemExit(1)
if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+\|[0-9]+\.[0-9]+\.[0-9]+", sys.argv[5]) is None:
    raise SystemExit(1)
if re.fullmatch(r"127\.0\.0\.1:5302/grove-observer@sha256:[0-9a-f]{64}", sys.argv[6]) is None:
    raise SystemExit(1)
if sys.argv[7] != "cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4":
    raise SystemExit(1)
print(json.dumps({"candidate": candidate, "provenance": {
    "captured_at_utc": sys.argv[3], "harness_revision": sys.argv[4],
    "docker": sys.argv[5], "image_digest": sys.argv[6],
    "grove_source_revision": sys.argv[7], "recorder_tools": recorder_tools,
}}, indent=2))
PY
echo "Sanitized MQTT control request observation captured: $run_id"
