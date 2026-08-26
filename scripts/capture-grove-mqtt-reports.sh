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
recorder_name="klove-mqtt-report-$run_id"
state_dir="$repo_root/.klove-integration/grove-observation/$run_id"
recorder_origin="$state_dir/mqtt-report-recorder-origin"
image="grove-observer:cdf6b829"
grove_source_revision="cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4"
recorder_started=0
grove_started=0
evidence_dir=

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
        || [ "$actual_role" != "mqtt-report-recorder" ] || [ "$actual_image" != "$image" ]; then
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
    if [ -n "$evidence_dir" ] && [ -d "$evidence_dir" ]; then
        rm -f -- "$evidence_dir/mqtt-report-schema" \
            "$evidence_dir/mqtt-report-schema.tmp" \
            "$evidence_dir/mqtt-report-recorder-provenance" \
            "$evidence_dir/mqtt-report-recorder-provenance.tmp" \
            "$evidence_dir/mqtt-report-status" \
            "$evidence_dir/mqtt-report-status.tmp" \
            "$evidence_dir/mqtt-report-ready" \
            "$evidence_dir/mqtt-report-ready.tmp"
        rmdir -- "$evidence_dir" 2>/dev/null || cleanup_ok=0
    fi
    if [ "$grove_started" -eq 1 ]; then
        "$script_dir/grove-observation-down.sh" "$run_id" || cleanup_ok=0
    fi
    if [ "$cleanup_ok" -ne 1 ]; then
        echo "MQTT report observation cleanup needs recovery for RUN_ID: $run_id" >&2
        exit 1
    fi
    exit "$status"
}
trap cleanup EXIT HUP INT TERM

if ! harness_revision=$(git -C "$repo_root" rev-parse --verify 'HEAD^{commit}' 2>/dev/null); then
    echo "MQTT report capture harness revision is unavailable" >&2; exit 1
fi
if ! capture_dirty=$(git -C "$repo_root" status --porcelain --untracked-files=all -- \
    scripts/capture-grove-mqtt-reports.sh \
    scripts/grove-mqtt-report-recorder.py \
    scripts/grove-mqtt-report-diagnostics.py \
    scripts/grove-mqtt-initial-request-recorder.py \
    scripts/grove-observation-lib.sh scripts/grove-observation-up.sh \
    scripts/grove-observation-down.sh 2>/dev/null); then
    echo "MQTT report capture harness state is unavailable" >&2; exit 1
fi
if [ -n "$capture_dirty" ]; then
    echo "MQTT report capture harness is not committed" >&2; exit 1
fi
if ! docker_versions=$(timeout 15 docker version --format '{{.Client.Version}}|{{.Server.Version}}' 2>/dev/null); then
    echo "MQTT report capture Docker version is unavailable" >&2; exit 1
fi
if ! image_binding=$(timeout 15 docker image inspect --format '{{.Id}}|{{index .RepoDigests 0}}|{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image" 2>/dev/null); then
    echo "MQTT report capture image binding is unavailable" >&2; exit 1
fi
IFS='|' read -r grove_image_id grove_image_digest grove_image_source_revision <<EOF
$image_binding
EOF
if [ -z "$grove_image_id" ] || [ -z "$grove_image_digest" ] \
    || [ "$grove_image_source_revision" != "$grove_source_revision" ]; then
    echo "MQTT report capture image binding is invalid" >&2; exit 1
fi

"$script_dir/grove-observation-up.sh" "$run_id"
grove_started=1
actual_grove_image_id=$(timeout 15 docker inspect --format '{{.Image}}' "$grove_name" 2>/dev/null) || {
    echo "MQTT report capture image identity changed" >&2; exit 1;
}
if [ "$actual_grove_image_id" != "$grove_image_id" ]; then
    echo "MQTT report capture image identity changed" >&2; exit 1
fi
grove_observation_require_origin "$state_dir/origin"
grove_observation_require_name "$run_id"

if ! virtual_serial=$(timeout 15 docker exec "$grove_name" curl --fail --silent --show-error \
    --max-time 10 http://127.0.0.1:8000/api/v1/virtual-printers 2>/dev/null \
    | python3 -c '
import json
import re
import sys

try:
    document = json.load(sys.stdin)
    printers = document["printers"]
    if type(printers) is not list or len(printers) != 1:
        raise ValueError
    serial = printers[0]["serial"]
    if type(serial) is not str or re.fullmatch(r"[A-Z0-9]{8,64}", serial) is None:
        raise ValueError
except (KeyError, TypeError, ValueError, json.JSONDecodeError, RecursionError):
    raise SystemExit(1)
print(serial)
' 2>/dev/null); then
    echo "MQTT report virtual-printer identity is unavailable" >&2; exit 1
fi

evidence_dir=$(mktemp -d "$state_dir/mqtt-report.XXXXXX")
chmod 700 "$evidence_dir"
grove_observation_capture_daemon
write_recorder_origin -
recorder_started=1
if ! timeout 60 docker run --detach --name "$recorder_name" --network "$grove_name" \
    --user 0:0 --cap-drop ALL --security-opt no-new-privileges --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,size=4m --memory 128m --cpus 0.5 --pids-limit 64 \
    --label "io.klove.grove-observation.run-id=$run_id" \
    --label "io.klove.grove-observation.role=mqtt-report-recorder" \
    --volume "$script_dir/grove-mqtt-report-recorder.py:/opt/report.py:ro" \
    --volume "$script_dir/grove-mqtt-initial-request-recorder.py:/opt/initial.py:ro" \
    --volume "$evidence_dir:/evidence:rw" --entrypoint sh "$image" -c \
    'case "$(uname -m)" in x86_64) loader=/lib64/ld-linux-x86-64.so.2 ;; aarch64) loader=/lib/ld-linux-aarch64.so.1 ;; *) exit 64 ;; esac
[ -x "$loader" ] || exit 64
KLOVE_INITIAL_RECORDER=/opt/initial.py exec "$loader" /usr/local/bin/python3.13 /opt/report.py "$@"' \
    sentinel "$grove_name" "$virtual_serial" >/dev/null 2>&1; then
    echo "MQTT report recorder bootstrap failed" >&2; exit 1
fi
recorder_id=$(timeout 15 docker inspect --format '{{.Id}}' "$recorder_name" 2>/dev/null) || {
    echo "MQTT report recorder bootstrap failed" >&2; exit 1;
}
case "$recorder_id" in '') echo "MQTT report recorder bootstrap failed" >&2; exit 1;; esac
write_recorder_origin "$recorder_id"

ready_path="$evidence_dir/mqtt-report-ready"
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
            echo "MQTT report recorder ready marker is invalid" >&2; exit 1
        fi
        break
    fi
    running=$(timeout 15 docker inspect --format '{{.State.Running}}' "$recorder_name" 2>/dev/null) || {
        echo "MQTT report recorder bootstrap failed" >&2; exit 1;
    }
    if [ "$running" != true ]; then echo "MQTT report recorder bootstrap failed" >&2; exit 1; fi
    attempt=$((attempt + 1)); sleep 0.1
done
if [ ! -f "$ready_path" ] || [ -L "$ready_path" ]; then
    echo "MQTT report recorder readiness timed out" >&2; exit 1
fi

recorder_status=$(timeout 45 docker wait "$recorder_name" 2>/dev/null) || recorder_status=
case "$recorder_status" in
    '' | *[!0-9]*) echo "MQTT_REPORT_CAPTURE_RECORDER_WAIT_INVALID" >&2; exit 1;;
esac
if [ "$recorder_status" -ne 0 ]; then
    recorder_code=$(python3 "$script_dir/grove-mqtt-report-diagnostics.py" \
        "$evidence_dir/mqtt-report-status" "$recorder_status" 2>/dev/null) || recorder_code=
    case "$recorder_code" in
        MQTT_REPORT_CAPTURE_RECORDER_INTERNAL_FAILURE \
        | MQTT_REPORT_CAPTURE_RECORDER_TIMEOUT \
        | MQTT_REPORT_CAPTURE_RECORDER_TLS_FAILURE \
        | MQTT_REPORT_CAPTURE_RECORDER_TRANSPORT_FAILURE \
        | MQTT_REPORT_CAPTURE_PROTOCOL_*)
            echo "$recorder_code" >&2; exit 1;;
        *) echo "MQTT_REPORT_CAPTURE_RECORDER_STATUS_INVALID" >&2; exit 1;;
    esac
fi
captured_at_utc=$(date -u '+%Y-%m-%dT%H:%M:%SZ') || {
    echo "MQTT report capture timestamp is unavailable" >&2; exit 1;
}
python3 - "$evidence_dir/mqtt-report-schema" \
    "$evidence_dir/mqtt-report-recorder-provenance" "$captured_at_utc" \
    "$harness_revision" "$docker_versions" "$grove_image_digest" "$grove_image_source_revision" \
    "$script_dir/grove-mqtt-report-recorder.py" <<'PY'
import importlib.util
import json
import pathlib
import re
import stat
import sys

schema_path, recorder_path = map(pathlib.Path, sys.argv[1:3])
for path in (schema_path, recorder_path):
    if not path.is_file() or path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise SystemExit(1)
    if not 0 < path.stat().st_size <= 32 * 1024:
        raise SystemExit(1)
spec = importlib.util.spec_from_file_location("report_observer", sys.argv[8])
if spec is None or spec.loader is None:
    raise SystemExit(1)
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)
try:
    candidate = json.loads(schema_path.read_text(encoding="ascii"))
    tools = json.loads(recorder_path.read_text(encoding="ascii"))
    observer.validate_candidate(candidate)
except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
    raise SystemExit(1) from None
serialized = json.dumps(candidate, sort_keys=True)
if any(token in serialized for token in ("TEST0000", "70001", "observation.3mf")):
    raise SystemExit(1)
if not isinstance(candidate, dict) or candidate.get("profile_version") != 1:
    raise SystemExit(1)
if not isinstance(tools, dict) or set(tools) != {"python", "openssl"}:
    raise SystemExit(1)
if any(not isinstance(tools[key], str) for key in tools):
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
    "grove_source_revision": sys.argv[7], "recorder_tools": tools,
}}, indent=2))
PY
echo "Sanitized MQTT report observation captured: $run_id"
