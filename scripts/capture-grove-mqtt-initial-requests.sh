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
recorder_name="klove-mqtt-initial-request-$run_id"
state_dir="$repo_root/.klove-integration/grove-observation/$run_id"
recorder_origin="$state_dir/mqtt-initial-request-recorder-origin"
image="grove-observer:cdf6b829"
grove_source_revision="cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4"
recorder_started=0
grove_started=0
evidence_dir=

write_recorder_origin() {
    recorder_id_value=$1
    temporary_origin="$recorder_origin.tmp"
    umask 077
    printf '%s|%s|%s|%s\n' "$grove_observation_context" "$grove_observation_daemon_id" \
        "$recorder_name" "$recorder_id_value" > "$temporary_origin"
    mv -- "$temporary_origin" "$recorder_origin"
}

cleanup_recorder() {
    if [ "$recorder_started" -ne 1 ]; then
        return 0
    fi
    if [ ! -f "$recorder_origin" ]; then
        echo "MQTT recorder recovery state is missing" >&2
        return 1
    fi
    IFS='|' read -r stored_context stored_daemon stored_name stored_id < "$recorder_origin"
    if [ "$stored_id" = "-" ]; then
        echo "MQTT recorder identity was not persisted; retaining recovery state" >&2
        return 1
    fi
    current_context=$(timeout 15 docker context show)
    current_daemon=$(timeout 15 docker info --format '{{.ID}}')
    if [ "$stored_context" != "$current_context" ] \
        || [ "$stored_daemon" != "$current_daemon" ] \
        || [ "$stored_name" != "$recorder_name" ]; then
        echo "MQTT recorder belongs to a different Docker context or daemon" >&2
        return 1
    fi
    actual_id=$(timeout 15 docker inspect --format '{{.Id}}' "$recorder_name")
    actual_label=$(timeout 15 docker inspect \
        --format '{{index .Config.Labels "io.klove.grove-observation.run-id"}}' "$recorder_name")
    actual_role=$(timeout 15 docker inspect \
        --format '{{index .Config.Labels "io.klove.grove-observation.role"}}' "$recorder_name")
    actual_image=$(timeout 15 docker inspect --format '{{.Config.Image}}' "$recorder_name")
    if [ "$actual_id" != "$stored_id" ] \
        || [ "$actual_label" != "$run_id" ] \
        || [ "$actual_role" != "mqtt-initial-request-recorder" ] \
        || [ "$actual_image" != "$image" ]; then
        echo "MQTT recorder identity changed; refusing cleanup" >&2
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
    if ! cleanup_recorder; then cleanup_ok=0; fi
    if [ -n "$evidence_dir" ] && [ -d "$evidence_dir" ]; then
        rm -f -- "$evidence_dir/mqtt-initial-request-schema" \
            "$evidence_dir/mqtt-initial-request-schema.tmp" \
            "$evidence_dir/mqtt-initial-request-recorder-provenance" \
            "$evidence_dir/mqtt-initial-request-recorder-provenance.tmp" \
            "$evidence_dir/mqtt-initial-request-status" \
            "$evidence_dir/mqtt-initial-request-status.tmp" \
            "$evidence_dir/mqtt-initial-request-ready" \
            "$evidence_dir/mqtt-initial-request-ready.tmp"
        rmdir -- "$evidence_dir" 2>/dev/null || cleanup_ok=0
    fi
    if [ "$grove_started" -eq 1 ]; then
        if "$script_dir/grove-observation-down.sh" "$run_id"; then grove_started=0; else cleanup_ok=0; fi
    fi
    if [ "$cleanup_ok" -ne 1 ]; then
        echo "MQTT initial request observation cleanup needs recovery for RUN_ID: $run_id" >&2
        exit 1
    fi
    exit "$status"
}
trap cleanup EXIT HUP INT TERM

if ! harness_revision=$(git -C "$repo_root" rev-parse --verify 'HEAD^{commit}' 2>/dev/null); then
    echo "MQTT capture harness revision is unavailable" >&2
    exit 1
fi
if ! capture_dirty=$(git -C "$repo_root" status --porcelain --untracked-files=all -- \
    scripts/capture-grove-mqtt-initial-requests.sh \
    scripts/grove-mqtt-initial-request-recorder.py \
    scripts/grove-mqtt-initial-request-drive.py \
    scripts/grove-observation-lib.sh \
    scripts/grove-observation-up.sh \
    scripts/grove-observation-down.sh 2>/dev/null); then
    echo "MQTT capture harness state is unavailable" >&2
    exit 1
fi
if [ -n "$capture_dirty" ]; then
    echo "MQTT capture harness is not committed" >&2
    exit 1
fi
if ! docker_versions=$(timeout 15 docker version --format '{{.Client.Version}}|{{.Server.Version}}' \
    2>/dev/null); then
    echo "MQTT capture Docker version is unavailable" >&2
    exit 1
fi
if ! image_binding=$(timeout 15 docker image inspect \
    --format '{{.Id}}|{{index .RepoDigests 0}}|{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "$image" 2>/dev/null); then
    echo "MQTT capture image binding is unavailable" >&2
    exit 1
fi
IFS='|' read -r grove_image_id grove_image_digest grove_image_source_revision <<EOF
$image_binding
EOF
if [ -z "$grove_image_id" ] || [ -z "$grove_image_digest" ] \
    || [ "$grove_image_source_revision" != "$grove_source_revision" ]; then
    echo "MQTT capture image binding is invalid" >&2
    exit 1
fi
if ! python3 - "$harness_revision" "$docker_versions" "$grove_image_id" \
    "$grove_image_digest" "$grove_image_source_revision" <<'PY'
import re
import sys

revision = re.compile(r"[0-9a-f]{40}\Z")
version = re.compile(r"(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})\Z")
image_id = re.compile(r"sha256:[0-9a-f]{64}\Z")
image_digest = re.compile(r"127\.0\.0\.1:5302/grove-observer@sha256:[0-9a-f]{64}\Z")

try:
    client, server = sys.argv[2].split("|", 1)
except ValueError:
    raise SystemExit(1) from None
raise SystemExit(
    0
    if revision.fullmatch(sys.argv[1])
    and version.fullmatch(client)
    and version.fullmatch(server)
    and image_id.fullmatch(sys.argv[3])
    and image_digest.fullmatch(sys.argv[4])
    and sys.argv[5] == "cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4"
    else 1
)
PY
then
    echo "MQTT capture binding is invalid" >&2
    exit 1
fi

surface_recorder_failure() {
    status_path="$evidence_dir/mqtt-initial-request-status"
    if [ ! -f "$status_path" ] || [ -L "$status_path" ]; then
        echo "MQTT recorder failure status is invalid" >&2
        return 1
    fi
    if ! failure_code=$(python3 - "$status_path" "$recorder_ip" <<'PY'
import json
import pathlib
import stat
import sys

def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def validate():
    path = pathlib.Path(sys.argv[1])
    metadata = path.stat()
    if not 0 < metadata.st_size <= 256 or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError
    serialized = path.read_text(encoding="ascii")
    for forbidden in (
        "MQTTOBS0000001",
        "TEST0000",
        sys.argv[2],
        "BEGIN CERTIFICATE",
        "PRIVATE KEY",
        "packet_id",
    ):
        if forbidden in serialized:
            raise ValueError
    document = json.loads(
        serialized,
        object_pairs_hook=unique_object,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
    )
    if type(document) is not dict or set(document) != {"status", "code"}:
        raise ValueError
    if type(document["status"]) is not str or type(document["code"]) is not str:
        raise ValueError
    if document["status"] != "failure" or document["code"] not in {
        "internal_failure",
        "protocol_failure",
        "timeout",
        "tls_failure",
        "transport_failure",
    }:
        raise ValueError
    return document["code"]


try:
    code = validate()
except Exception:
    raise SystemExit(1) from None
print(code)
PY
    ); then
        echo "MQTT recorder failure status is invalid" >&2
        return 1
    fi
    echo "MQTT recorder failed: $failure_code" >&2
}

validate_recorder_ready() {
    ready_path="$evidence_dir/mqtt-initial-request-ready"
    if [ ! -f "$ready_path" ] || [ -L "$ready_path" ]; then
        echo "MQTT recorder ready marker is invalid" >&2
        return 1
    fi
    if ! python3 - "$ready_path" <<'PY'
import json
import pathlib
import stat
import sys


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def validate():
    path = pathlib.Path(sys.argv[1])
    metadata = path.stat()
    if not 0 < metadata.st_size <= 64 or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError
    document = json.loads(
        path.read_text(encoding="ascii"),
        object_pairs_hook=unique_object,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
    )
    if type(document) is not dict or set(document) != {"status"}:
        raise ValueError
    if type(document["status"]) is not str or document["status"] != "ready":
        raise ValueError


try:
    validate()
except Exception:
    raise SystemExit(1) from None
PY
    then
        echo "MQTT recorder ready marker is invalid" >&2
        return 1
    fi
}

wait_for_recorder_ready() {
    ready_path="$evidence_dir/mqtt-initial-request-ready"
    attempts=0
    while [ "$attempts" -lt 50 ]; do
        if [ -e "$ready_path" ] || [ -L "$ready_path" ]; then
            validate_recorder_ready
            return
        fi
        if ! recorder_running=$(timeout 15 docker inspect --format '{{.State.Running}}' \
            "$recorder_name" 2>/dev/null); then
            echo "MQTT recorder bootstrap failed" >&2
            return 1
        fi
        if [ "$recorder_running" != "true" ]; then
            timeout 15 docker wait "$recorder_name" >/dev/null 2>&1 || true
            if [ -e "$evidence_dir/mqtt-initial-request-status" ] \
                || [ -L "$evidence_dir/mqtt-initial-request-status" ]; then
                surface_recorder_failure || true
            else
                echo "MQTT recorder bootstrap failed" >&2
            fi
            return 1
        fi
        attempts=$((attempts + 1))
        sleep 0.1
    done
    echo "MQTT recorder readiness timed out" >&2
    return 1
}

"$script_dir/grove-observation-up.sh" "$run_id"
grove_started=1
if ! actual_grove_image_id=$(timeout 15 docker inspect --format '{{.Image}}' "$grove_name" 2>/dev/null) \
    || [ "$actual_grove_image_id" != "$grove_image_id" ]; then
    echo "MQTT capture image identity changed" >&2
    exit 1
fi
grove_observation_require_origin "$state_dir/origin"
grove_observation_require_name "$run_id"
evidence_dir=$(mktemp -d "$state_dir/mqtt-initial-request.XXXXXX")
chmod 700 "$evidence_dir"
grove_observation_capture_daemon
write_recorder_origin -
recorder_started=1

if ! timeout 60 docker run --detach --name "$recorder_name" --network "$grove_name" \
    --user 0:0 --cap-drop ALL \
    --security-opt no-new-privileges --read-only --tmpfs /tmp:rw,noexec,nosuid,size=4m \
    --memory 128m --cpus 0.5 --pids-limit 64 \
    --label "io.klove.grove-observation.run-id=$run_id" \
    --label "io.klove.grove-observation.role=mqtt-initial-request-recorder" \
    --volume "$script_dir/grove-mqtt-initial-request-recorder.py:/opt/recorder.py:ro" \
    --volume "$evidence_dir:/evidence:rw" \
    --entrypoint sh "$image" -c \
    'openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=observation.invalid -keyout /tmp/observation-key.pem -out /tmp/observation-cert.pem >/dev/null 2>&1 || exit 64
case "$(uname -m)" in
    x86_64) loader=/lib64/ld-linux-x86-64.so.2 ;;
    aarch64) loader=/lib/ld-linux-aarch64.so.1 ;;
    *) exit 64 ;;
esac
[ -x "$loader" ] || exit 64
exec "$loader" /usr/local/bin/python3.13 /opt/recorder.py' \
    >/dev/null 2>&1; then
    echo "MQTT recorder bootstrap failed" >&2
    exit 1
fi
if ! recorder_id=$(timeout 15 docker inspect --format '{{.Id}}' \
    "$recorder_name" 2>/dev/null); then
    echo "MQTT recorder bootstrap failed" >&2
    exit 1
fi
if [ -z "$recorder_id" ]; then
    echo "MQTT recorder bootstrap failed" >&2
    exit 1
fi
write_recorder_origin "$recorder_id"

if ! actual_recorder_image_id=$(timeout 15 docker inspect --format '{{.Image}}' "$recorder_name" \
    2>/dev/null) || [ "$actual_recorder_image_id" != "$grove_image_id" ]; then
    echo "MQTT capture image identity changed" >&2
    exit 1
fi

if ! recorder_ip=$(timeout 15 docker inspect \
    --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \
    "$recorder_name" 2>/dev/null); then
    echo "MQTT recorder bootstrap failed" >&2
    exit 1
fi
case "$recorder_ip" in *[!0-9.]* | "") echo "MQTT recorder bootstrap failed" >&2; exit 1;; esac
wait_for_recorder_ready
driver_status=0
if timeout 30 docker exec --interactive "$grove_name" \
    python - "$recorder_ip" < "$script_dir/grove-mqtt-initial-request-drive.py" \
    >/dev/null 2>&1; then
    :
else
    driver_status=$?
fi
recorder_wait_ok=1
if recorder_status=$(timeout 60 docker wait "$recorder_name"); then
    :
else
    recorder_wait_ok=0
    recorder_status=
fi
if [ "$driver_status" -ne 0 ] || [ "$recorder_wait_ok" -ne 1 ] \
    || [ "$recorder_status" != "0" ]; then
    surface_recorder_failure || true
    exit 1
fi
if ! captured_at_utc=$(date -u '+%Y-%m-%dT%H:%M:%SZ'); then
    echo "MQTT initial request capture timestamp is unavailable" >&2
    exit 1
fi
if [ ! -f "$evidence_dir/mqtt-initial-request-schema" ] \
    || [ -L "$evidence_dir/mqtt-initial-request-schema" ]; then
    echo "MQTT recorder evidence is unavailable" >&2
    exit 1
fi
if [ ! -f "$evidence_dir/mqtt-initial-request-recorder-provenance" ] \
    || [ -L "$evidence_dir/mqtt-initial-request-recorder-provenance" ]; then
    echo "MQTT recorder provenance is unavailable" >&2
    exit 1
fi
python3 - "$evidence_dir/mqtt-initial-request-schema" \
    "$evidence_dir/mqtt-initial-request-recorder-provenance" "$captured_at_utc" \
    "$recorder_ip" "$run_id" "$harness_revision" "$docker_versions" "$grove_image_digest" \
    "$grove_image_source_revision" <<'PY'
import datetime
import json
import pathlib
import re
import stat
import sys

MAX_DEPTH = 12
MAX_MEMBERS = 64
MAX_SCHEMA_NODES = 256
MEMBER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
PYTHON_VERSION = re.compile(r"3\.13\.(?:0|[1-9][0-9]{0,2})\Z")
OPENSSL_VERSION = re.compile(r"OpenSSL 3\.[0-9]+\.[0-9]+\Z")
DOCKER_VERSION = re.compile(r"(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})\Z")
HEX_REVISION = re.compile(r"[0-9a-f]{40}\Z")
IMAGE_DIGEST = re.compile(
    r"127\.0\.0\.1:5302/grove-observer@(sha256:[0-9a-f]{64})\Z"
)
RUN_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?\Z")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate candidate member")
        result[key] = value
    return result


def reject_constant(_value):
    raise ValueError("nonstandard candidate constant")


def require_keys(value, expected):
    if type(value) is not dict or set(value) != expected:
        raise ValueError("candidate shape invalid")


schema_nodes = 0


def schema_for(container, names):
    return {
        "type": "object",
        "members": [
            {
                "name": container,
                "present": True,
                "schema": {
                    "type": "object",
                    "members": [
                        {"name": name, "present": True, "schema": {"type": "string"}}
                        for name in names
                    ],
                },
            }
        ],
    }


EXPECTED_SCHEMAS = {
    "pushall": schema_for("pushing", ("command",)),
    "get_version": schema_for("info", ("sequence_id", "command")),
    "extrusion_cali_get": schema_for(
        "print", ("command", "filament_id", "nozzle_diameter", "sequence_id")
    ),
}


def validate_schema(value, depth=0):
    global schema_nodes
    schema_nodes += 1
    if schema_nodes > MAX_SCHEMA_NODES or depth > MAX_DEPTH:
        raise ValueError("candidate schema exceeds bound")
    if type(value) is not dict or type(value.get("type")) is not str:
        raise ValueError("candidate schema invalid")
    kind = value["type"]
    if kind == "object":
        require_keys(value, {"type", "members"})
        members = value["members"]
        if type(members) is not list or not 1 <= len(members) <= MAX_MEMBERS:
            raise ValueError("candidate object members invalid")
        names = set()
        for member in members:
            require_keys(member, {"name", "present", "schema"})
            name = member["name"]
            if type(name) is not str or MEMBER_NAME.fullmatch(name) is None or name in names:
                raise ValueError("candidate member name invalid")
            if member["present"] is not True:
                raise ValueError("candidate presence fact invalid")
            names.add(name)
            validate_schema(member["schema"], depth + 1)
        return
    if kind == "array":
        require_keys(value, {"type", "items"})
        items = value["items"]
        if type(items) is not list or len(items) > MAX_MEMBERS:
            raise ValueError("candidate array items invalid")
        for item in items:
            validate_schema(item, depth + 1)
        return
    if kind not in {"null", "boolean", "string", "number"}:
        raise ValueError("candidate schema type invalid")
    require_keys(value, {"type"})

path = pathlib.Path(sys.argv[1])
if path.stat().st_size > 16 * 1024:
    raise SystemExit("MQTT recorder evidence exceeds bound")
if stat.S_IMODE(path.stat().st_mode) != 0o600:
    raise SystemExit("MQTT recorder evidence permissions are not owner-private")
serialized = path.read_text(encoding="ascii")
for forbidden in (
    "MQTTOBS0000001",
    "TEST0000",
    sys.argv[4],
    "BEGIN CERTIFICATE",
    "PRIVATE KEY",
    "packet_id",
):
    if forbidden in serialized:
        raise SystemExit("MQTT recorder evidence contains forbidden material")
try:
    document = json.loads(
        serialized,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )
    require_keys(document, {"profile_version", "transport", "observed"})
    if type(document["profile_version"]) is not int or document["profile_version"] != 1:
        raise ValueError("candidate profile version invalid")
    if document["transport"] != "mqtt-over-tls":
        raise ValueError("candidate transport invalid")
    observed = document["observed"]
    require_keys(observed, {"initial_requests", "first_puback_after_next_initial_publish"})
    if observed["first_puback_after_next_initial_publish"] is not True:
        raise ValueError("candidate PUBACK ordering invalid")
    requests = observed["initial_requests"]
    if type(requests) is not list or len(requests) != 3:
        raise ValueError("candidate request count invalid")
    expected_commands = ("pushall", "get_version", "extrusion_cali_get")
    expected_lengths = (35, 56, 109)
    expected_markers = (False, True, True)
    expected_pubacks = ("after_next_initial_publish", "after_publish", "after_publish")
    request_keys = {
        "topic", "qos", "dup", "retain", "command", "members",
        "generated_sequence_marker", "payload_bytes", "puback_order",
    }
    for request, command, length, marker, puback in zip(
        requests,
        expected_commands,
        expected_lengths,
        expected_markers,
        expected_pubacks,
        strict=True,
    ):
        require_keys(request, request_keys)
        if request["topic"] != "device/{serial}/request":
            raise ValueError("candidate topic invalid")
        if type(request["qos"]) is not int or request["qos"] != 1:
            raise ValueError("candidate QoS invalid")
        if request["dup"] is not False or request["retain"] is not False:
            raise ValueError("candidate flags invalid")
        if request["command"] != command or request["puback_order"] != puback:
            raise ValueError("candidate ordering invalid")
        if request["generated_sequence_marker"] is not marker:
            raise ValueError("candidate sequence marker invalid")
        if type(request["payload_bytes"]) is not int or request["payload_bytes"] != length:
            raise ValueError("candidate payload length invalid")
        validate_schema(request["members"])
        if request["members"] != EXPECTED_SCHEMAS[command]:
            raise ValueError("candidate member schema invalid")
except (KeyError, TypeError, ValueError):
    raise SystemExit("MQTT recorder evidence shape is invalid") from None

provenance_path = pathlib.Path(sys.argv[2])
try:
    provenance_metadata = provenance_path.stat()
    if (
        not provenance_path.is_file()
        or provenance_path.is_symlink()
        or not 0 < provenance_metadata.st_size <= 512
        or stat.S_IMODE(provenance_metadata.st_mode) != 0o600
    ):
        raise ValueError
    provenance_serialized = provenance_path.read_text(encoding="ascii")
    for forbidden in (
        "MQTTOBS0000001",
        "TEST0000",
        sys.argv[4],
        "BEGIN CERTIFICATE",
        "PRIVATE KEY",
        "packet_id",
    ):
        if forbidden in provenance_serialized:
            raise ValueError
    provenance = json.loads(
        provenance_serialized,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )
    require_keys(provenance, {"python", "openssl"})
    if (
        type(provenance["python"]) is not str
        or PYTHON_VERSION.fullmatch(provenance["python"]) is None
        or type(provenance["openssl"]) is not str
        or OPENSSL_VERSION.fullmatch(provenance["openssl"]) is None
    ):
        raise ValueError
    captured_at_utc = sys.argv[3]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", captured_at_utc) is None:
        raise ValueError
    datetime.datetime.strptime(captured_at_utc, "%Y-%m-%dT%H:%M:%SZ")
    run_id = sys.argv[5]
    harness_revision = sys.argv[6]
    docker_client, docker_server = sys.argv[7].split("|", 1)
    image_digest = IMAGE_DIGEST.fullmatch(sys.argv[8])
    image_source_revision = sys.argv[9]
    if (
        RUN_ID.fullmatch(run_id) is None
        or HEX_REVISION.fullmatch(harness_revision) is None
        or DOCKER_VERSION.fullmatch(docker_client) is None
        or DOCKER_VERSION.fullmatch(docker_server) is None
        or image_digest is None
        or image_source_revision != "cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4"
    ):
        raise ValueError
except (OSError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit("MQTT recorder provenance is invalid") from None

print(
    json.dumps(
        {
            "captured_at_utc": captured_at_utc,
            "command": f"scripts/capture-grove-mqtt-initial-requests.sh {run_id}",
            "grove_image": {
                "digest": image_digest.group(1),
                "source_revision": image_source_revision,
            },
            "harness_revision": harness_revision,
            "run_id": run_id,
            "tool_versions": {
                "docker_client": docker_client,
                "docker_server": docker_server,
                "openssl": provenance["openssl"],
                "python": provenance["python"],
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    )
)
PY
cat -- "$evidence_dir/mqtt-initial-request-schema"
