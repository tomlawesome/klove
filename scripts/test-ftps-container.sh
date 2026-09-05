#!/usr/bin/env sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
fixture="$repo_root/tests/integration/ftps-container"
compose_file="$fixture/compose.yml"
run_id=${KLOVE_FTPS_RUN_ID:-local-$(date -u +%Y%m%d%H%M%S)-$$}

case "$run_id" in
    *[!a-z0-9-]* | "" | -* | *-) echo "invalid FTPS container RUN_ID" >&2; exit 2 ;;
esac
if [ "${#run_id}" -gt 32 ]; then
    echo "FTPS container RUN_ID exceeds 32 characters" >&2
    exit 2
fi
command -v docker >/dev/null 2>&1 || { echo "Docker is required" >&2; exit 1; }
command -v openssl >/dev/null 2>&1 || { echo "OpenSSL is required" >&2; exit 1; }
command -v timeout >/dev/null 2>&1 || { echo "coreutils timeout is required" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "Python 3 is required" >&2; exit 1; }
command -v git >/dev/null 2>&1 || { echo "Git is required" >&2; exit 1; }
command -v ip >/dev/null 2>&1 || { echo "iproute2 is required" >&2; exit 1; }

state_dir="$repo_root/.klove-integration/ftps-container/$run_id"
origin="$state_dir/origin"
project="klove-ftps-$run_id"
image="klove-ftps-contract:$run_id"
network_name="${project}_private"
secret_volume="${project}_runtime-secrets"
state_volume="${project}_klove-state"
staging_volume="${project}_staging-state"
trust_volume="${project}_contract-trust"
prepare_name="${project}-prepare-1"
runtime_name="${project}-klove-1"
contract_name="${project}-contract-1"

cleanup() {
    status=$?
    trap - EXIT HUP INT TERM
    echo "FTPS container cleanup retained recovery state: $run_id" >&2
    [ "$status" -ne 0 ] || status=1
    exit "$status"
}

umask 077
mkdir -p "$(dirname -- "$state_dir")"
if ! mkdir "$state_dir"; then
    echo "could not atomically claim FTPS container run: $run_id" >&2
    exit 1
fi
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

context=$(timeout 15 docker context show)
daemon_id=$(timeout 15 docker --context "$context" info --format '{{.ID}}')
security_options=$(timeout 15 docker --context "$context" info --format '{{json .SecurityOptions}}')
case "$security_options" in
    *name=rootless*) daemon_mode=rootless ;;
    *)
        if [ "${CI:-}" != "true" ] || [ "${KLOVE_FTPS_ALLOW_ROOTFUL_CI:-}" != "1" ]; then
            echo "local FTPS container checks require rootless Docker" >&2
            exit 1
        fi
        daemon_mode=rootful-ci
        ;;
esac
[ -n "$context" ] && [ -n "$daemon_id" ] || { echo "Docker identity unavailable" >&2; exit 1; }

mkdir "$state_dir/input"
host_low_port_before=$(cat /proc/sys/net/ipv4/ip_unprivileged_port_start)
network_ids=$(timeout 15 docker --context "$context" network ls -q)
if [ -n "$network_ids" ]; then
    timeout 30 docker --context "$context" network inspect $network_ids > "$state_dir/networks.json"
else
    printf '[]\n' > "$state_dir/networks.json"
fi
ip -j route > "$state_dir/routes.json"
timeout 15 python3 - "$run_id" "$state_dir/networks.json" "$state_dir/routes.json" > "$state_dir/candidates" <<'PY'
import hashlib, ipaddress, json, sys
seed = int.from_bytes(hashlib.sha256(sys.argv[1].encode("ascii")).digest()[:4], "big")
used = []
for network in json.load(open(sys.argv[2], encoding="utf-8")):
    for config in network.get("IPAM", {}).get("Config", []) or []:
        subnet = config.get("Subnet")
        if subnet:
            try: used.append(ipaddress.ip_network(subnet, strict=False))
            except ValueError: pass
for route in json.load(open(sys.argv[3], encoding="utf-8")):
    destination = route.get("dst")
    if destination and destination != "default":
        try: used.append(ipaddress.ip_network(destination, strict=False))
        except ValueError: pass
for offset in range(4096):
    value = (seed + offset) % 4096
    candidate = ipaddress.ip_network(f"10.{200 + value // 256}.{value % 256}.0/24")
    if all(not candidate.overlaps(other) for other in used):
        print(candidate, candidate.network_address + 10, candidate.network_address + 20)
PY

revision=$(timeout 15 git -C "$repo_root" rev-parse HEAD)
branch=$(timeout 15 git -C "$repo_root" branch --show-current)
digest_values=$(timeout 30 python3 - "$repo_root" <<'PY'
import hashlib, os, pathlib, stat, sys

root = pathlib.Path(sys.argv[1])


def exact_file(relative: str) -> pathlib.Path:
    path = root / relative
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SystemExit(f"manifest input unavailable: {relative}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(f"manifest input is not one regular file: {relative}")
    return path


def exact_tree(relative: str) -> list[pathlib.Path]:
    directory = root / relative
    try:
        metadata = directory.lstat()
    except OSError as exc:
        raise SystemExit(f"manifest tree unavailable: {relative}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise SystemExit(f"manifest tree is not one directory: {relative}")
    result: list[pathlib.Path] = []
    for current, directories, filenames in os.walk(directory, followlinks=False):
        current_path = pathlib.Path(current)
        for name in directories:
            candidate = current_path / name
            if not stat.S_ISDIR(candidate.lstat().st_mode):
                raise SystemExit(f"manifest tree contains a non-directory: {candidate}")
        for name in filenames:
            candidate = current_path / name
            if not stat.S_ISREG(candidate.lstat().st_mode):
                raise SystemExit(f"manifest tree contains a non-regular file: {candidate}")
            result.append(candidate)
    return result


def bound_digest(paths: list[pathlib.Path]) -> str:
    digest = hashlib.sha256()
    unique = {path.relative_to(root).as_posix(): path for path in paths}
    for relative, path in sorted(unique.items()):
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise SystemExit(f"manifest input changed identity: {relative}") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise SystemExit(f"manifest input changed identity: {relative}")
            content = hashlib.sha256()
            while chunk := os.read(descriptor, 64 * 1024):
                content.update(chunk)
            final_metadata = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        current = path.lstat()
        bound_identity = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_size,
            metadata.st_mtime_ns,
        )
        if bound_identity != (
            final_metadata.st_dev,
            final_metadata.st_ino,
            final_metadata.st_mode,
            final_metadata.st_size,
            final_metadata.st_mtime_ns,
        ) or bound_identity != (
            current.st_dev,
            current.st_ino,
            current.st_mode,
            current.st_size,
            current.st_mtime_ns,
        ):
            raise SystemExit(f"manifest input changed while hashing: {relative}")
        encoded_path = relative.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(4, "big"))
        digest.update(encoded_path)
        digest.update(stat.S_IMODE(metadata.st_mode).to_bytes(4, "big"))
        digest.update(content.digest())
    return digest.hexdigest()


source = [
    exact_file(name)
    for name in (
        ".dockerignore",
        "Dockerfile",
        "README.md",
        "pyproject.toml",
        "requirements-build.lock",
        "requirements.lock",
    )
] + exact_tree("src")
lane = [
    exact_file(name)
    for name in (
        "Dockerfile",
        ".dockerignore",
        "README.md",
        "compose.example.yml",
        "config.example.toml",
        ".github/workflows/validate.yml",
        "scripts/test-ftps-container.sh",
    )
] + exact_tree("src") + exact_tree("tests/integration/ftps-container")
print(bound_digest(source), bound_digest(lane))
PY
)
set -- $digest_values
source_digest=$1
lane_digest=$2

verify_daemon() {
    actual_daemon=$(timeout 15 docker --context "$context" info --format '{{.ID}}')
    [ "$actual_daemon" = "$daemon_id" ] || {
        echo "FTPS container Docker daemon identity changed" >&2
        exit 1
    }
}

cleanup() {
    status=$?
    trap - EXIT HUP INT TERM
    cleanup_ok=true
    current_context=$(timeout 15 docker context show 2>/dev/null || true)
    current_daemon=$(timeout 15 docker --context "$context" info --format '{{.ID}}' 2>/dev/null || true)
    if [ "$current_context" != "$context" ] || [ "$current_daemon" != "$daemon_id" ]; then
        cleanup_ok=false
    fi
    if [ -z "${prepare_id:-}" ]; then
        prepare_id=$(timeout 15 docker --context "$context" inspect --format '{{.Id}}' "$prepare_name" 2>/dev/null || true)
    fi
    if [ -z "${runtime_id:-}" ]; then
        runtime_id=$(timeout 15 docker --context "$context" inspect --format '{{.Id}}' "$runtime_name" 2>/dev/null || true)
    fi
    if [ -z "${contract_id:-}" ]; then
        contract_id=$(timeout 15 docker --context "$context" inspect --format '{{.Id}}' "$contract_name" 2>/dev/null || true)
    fi
    if [ -z "${network_id:-}" ]; then
        network_id=$(timeout 15 docker --context "$context" network inspect --format '{{.Id}}' "$network_name" 2>/dev/null || true)
    fi
    for role_id in "${prepare_id:-}:prepare" "${runtime_id:-}:runtime" "${contract_id:-}:contract"; do
        id=${role_id%%:*}; role=${role_id#*:}
        [ -z "$id" ] && continue
        if timeout 15 docker --context "$context" inspect "$id" >/dev/null 2>&1; then
            actual_project=$(timeout 15 docker --context "$context" inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' "$id" 2>/dev/null || true)
            actual_role=$(timeout 15 docker --context "$context" inspect --format '{{ index .Config.Labels "io.klove.ftps-container.role" }}' "$id" 2>/dev/null || true)
            if [ "$actual_project" != "$project" ] || [ "$actual_role" != "$role" ]; then
                cleanup_ok=false
            fi
        fi
    done
    if [ "$cleanup_ok" = true ]; then
        for role_id in "${prepare_id:-}:prepare" "${runtime_id:-}:runtime" "${contract_id:-}:contract"; do
            id=${role_id%%:*}; role=${role_id#*:}
            [ -z "$id" ] && continue
            if timeout 15 docker --context "$context" inspect "$id" >/dev/null 2>&1; then
                actual_project=$(timeout 15 docker --context "$context" inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' "$id" 2>/dev/null || true)
                actual_role=$(timeout 15 docker --context "$context" inspect --format '{{ index .Config.Labels "io.klove.ftps-container.role" }}' "$id" 2>/dev/null || true)
                if [ "$actual_project" = "$project" ] && [ "$actual_role" = "$role" ]; then
                    timeout 30 docker --context "$context" container rm --force "$id" >/dev/null 2>&1 || cleanup_ok=false
                else
                    cleanup_ok=false
                fi
            fi
        done
    fi
    if [ "$cleanup_ok" = true ] && [ -n "${network_id:-}" ]; then
        actual_network_name=$(timeout 15 docker --context "$context" network inspect --format '{{.Name}}' "$network_id" 2>/dev/null || true)
        actual_network_project=$(timeout 15 docker --context "$context" network inspect --format '{{ index .Labels "com.docker.compose.project" }}' "$network_id" 2>/dev/null || true)
        actual_network_role=$(timeout 15 docker --context "$context" network inspect --format '{{ index .Labels "io.klove.ftps-container.role" }}' "$network_id" 2>/dev/null || true)
        actual_compose_network=$(timeout 15 docker --context "$context" network inspect --format '{{ index .Labels "com.docker.compose.network" }}' "$network_id" 2>/dev/null || true)
        if [ -z "$actual_network_name" ] || { [ "$actual_network_name" = "$network_name" ] && [ "$actual_network_project" = "$project" ] && [ "$actual_network_role" = "network" ] && [ "$actual_compose_network" = "private" ]; }; then
            [ -z "$actual_network_name" ] || timeout 30 docker --context "$context" network rm "$network_id" >/dev/null 2>&1 || cleanup_ok=false
        else
            cleanup_ok=false
        fi
    fi
    if [ "$cleanup_ok" = true ]; then
        for volume in "${secret_volume:-}" "${state_volume:-}" "${staging_volume:-}" "${trust_volume:-}"; do
            [ -z "$volume" ] && continue
            actual_volume_project=$(timeout 15 docker --context "$context" volume inspect --format '{{ index .Labels "com.docker.compose.project" }}' "$volume" 2>/dev/null || true)
            actual_volume_role=$(timeout 15 docker --context "$context" volume inspect --format '{{ index .Labels "com.docker.compose.volume" }}' "$volume" 2>/dev/null || true)
            expected_volume_role=${volume#"${project}_"}
            if [ -z "$actual_volume_project" ] || { [ "$actual_volume_project" = "$project" ] && [ "$actual_volume_role" = "$expected_volume_role" ]; }; then
                [ -z "$actual_volume_project" ] || timeout 30 docker --context "$context" volume rm "$volume" >/dev/null 2>&1 || cleanup_ok=false
            else
                cleanup_ok=false
            fi
        done
    fi
    if [ "$cleanup_ok" = true ]; then
        actual_image_id=$(timeout 15 docker --context "$context" image inspect --format '{{.Id}}' "$image" 2>/dev/null || true)
        if [ -z "$actual_image_id" ] || [ "${image_id:-}" = "$actual_image_id" ]; then
            [ -z "$actual_image_id" ] || timeout 30 docker --context "$context" image rm "$image" >/dev/null 2>&1 || cleanup_ok=false
        else
            cleanup_ok=false
        fi
    fi
    if [ "$cleanup_ok" = true ]; then
        rm -rf -- "$state_dir"
        echo "FTPS container cleanup removed exact owned resources: $run_id"
        exit "$status"
    fi
    chmod 0700 "$state_dir/input" 2>/dev/null || true
    chmod 0600 "$state_dir/input/"* 2>/dev/null || true
    echo "FTPS container cleanup needs recovery for RUN_ID: $run_id" >&2
    [ "$status" -ne 0 ] || status=1
    exit "$status"
}
printf '%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n' \
    "$project" "$context" "$daemon_id" "$daemon_mode" "$image" "$revision" \
    "$source_digest" "$lane_digest" "$host_low_port_before" "$network_name" \
    "$secret_volume" "$state_volume" "$staging_volume" "$trust_volume" \
    "$prepare_name" "$runtime_name" "$contract_name" \
    > "$origin"
echo "FTPS container daemon mode: $daemon_mode"
echo "FTPS container source digest: $source_digest"
echo "FTPS container lane digest: $lane_digest"

if timeout 15 docker --context "$context" network inspect "$network_name" >/dev/null 2>&1; then
    echo "FTPS container network name already exists" >&2
    exit 1
fi
while read -r candidate_subnet candidate_service candidate_client; do
    network_id=$(timeout 30 docker --context "$context" network create \
        --driver bridge --internal --subnet "$candidate_subnet" \
        --label "com.docker.compose.project=$project" \
        --label "com.docker.compose.network=private" \
        --label "io.klove.ftps-container.role=network" \
        "$network_name" 2>/dev/null || true)
    if [ -n "$network_id" ]; then
        KLOVE_FTPS_SUBNET=$candidate_subnet
        KLOVE_FTPS_SERVICE_IP=$candidate_service
        KLOVE_FTPS_CLIENT_IP=$candidate_client
        break
    fi
    if timeout 15 docker --context "$context" network inspect "$network_name" >/dev/null 2>&1; then
        echo "FTPS container network name was concurrently claimed" >&2
        exit 1
    fi
done < "$state_dir/candidates"
[ -n "${network_id:-}" ] || { echo "no disjoint private fixture subnet available" >&2; exit 1; }
printf '%s\n%s\n%s\n%s\n' "$network_id" "$KLOVE_FTPS_SUBNET" "$KLOVE_FTPS_SERVICE_IP" "$KLOVE_FTPS_CLIENT_IP" >> "$origin"

KLOVE_FTPS_IMAGE=$image
KLOVE_FTPS_INPUT="$state_dir/input"
KLOVE_FTPS_CERTIFICATE="$state_dir/input/tls-certificate.pem"
KLOVE_FTPS_NETWORK=$network_name
export KLOVE_FTPS_SUBNET KLOVE_FTPS_SERVICE_IP KLOVE_FTPS_CLIENT_IP KLOVE_FTPS_IMAGE KLOVE_FTPS_INPUT KLOVE_FTPS_CERTIFICATE KLOVE_FTPS_NETWORK

timeout 30 openssl req -x509 -newkey rsa:2048 -nodes -days 1 -sha256 \
    -subj "/CN=klove-ftps-container" \
    -addext "subjectAltName=IP:$KLOVE_FTPS_SERVICE_IP" \
    -keyout "$state_dir/input/tls-private-key.pem" \
    -out "$state_dir/input/tls-certificate.pem" >/dev/null 2>&1
timeout 15 openssl rand -base64 48 > "$state_dir/input/api-token"
cat > "$state_dir/input/config.toml" <<EOF
[api]
listen_host = "0.0.0.0"
listen_port = 8080
token_file = "/run/klove-secrets/api-token"

[registry]
database_file = "/var/lib/klove/printer-registry.sqlite3"
secret_directory = "/var/lib/klove/registry-secrets"
allowed_probe_cidrs = ["$KLOVE_FTPS_SUBNET"]

[control]
enabled = false

[dispatch]
enabled = false
journal_file = "/var/lib/klove/start-journal.sqlite3"

[grove_bridge]
enabled = true
listen_host = "0.0.0.0"
mqtt_port = 8883
ftps_control_port = 990
ftps_passive_port_min = 50000
ftps_passive_port_max = 50009
ftps_advertised_ipv4 = "$KLOVE_FTPS_SERVICE_IP"
tls_certificate_file = "/run/klove-secrets/tls-certificate.pem"
tls_private_key_file = "/run/klove-secrets/tls-private-key.pem"
mqtt_journal_file = "/var/lib/klove/mqtt-ingress.sqlite3"
staging_directory = "/var/lib/klove/ftps-staging"
EOF
chmod 0700 "$state_dir/input"
chmod 0600 "$state_dir/input/"*

verify_daemon
timeout 900 docker --context "$context" build --network=host \
    --build-arg "VCS_REF=$revision" \
    --build-arg "SOURCE_BRANCH=$branch" \
    --build-arg "SOURCE_DIGEST=$source_digest" \
    --tag "$image" "$repo_root"
image_id=$(timeout 15 docker --context "$context" image inspect --format '{{.Id}}' "$image")
[ -n "$image_id" ] || { echo "built image identity unavailable" >&2; exit 1; }
[ "$(timeout 15 docker --context "$context" image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image")" = "$revision" ]
[ "$(timeout 15 docker --context "$context" image inspect --format '{{ index .Config.Labels "io.github.tomlawesome.klove.source-digest" }}' "$image")" = "$source_digest" ]
printf '%s\n' "$image_id" >> "$origin"
echo "FTPS container image ID: $image_id"
KLOVE_FTPS_IMAGE=$image_id
export KLOVE_FTPS_IMAGE

verify_daemon
timeout 60 docker --context "$context" compose --project-name "$project" --file "$compose_file" create
prepare_id=$(timeout 15 docker --context "$context" compose --project-name "$project" --file "$compose_file" ps --all -q prepare)
runtime_id=$(timeout 15 docker --context "$context" compose --project-name "$project" --file "$compose_file" ps --all -q klove)
contract_id=$(timeout 15 docker --context "$context" compose --project-name "$project" --file "$compose_file" ps --all -q contract)
[ -n "$prepare_id" ] && [ -n "$runtime_id" ] && [ -n "$contract_id" ]
[ "$(timeout 15 docker --context "$context" inspect --format '{{.Image}}' "$runtime_id")" = "$image_id" ]
[ "$(timeout 15 docker --context "$context" inspect --format '{{.Image}}' "$contract_id")" = "$image_id" ]
network_id=$(timeout 15 docker --context "$context" network inspect --format '{{.Id}}' "$network_name")
timeout 15 docker --context "$context" volume inspect "$secret_volume" "$state_volume" "$staging_volume" "$trust_volume" >/dev/null
printf '%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n' "$prepare_id" "$runtime_id" "$contract_id" "$network_id" "$secret_volume" "$state_volume" "$staging_volume" "$trust_volume" >> "$origin"
echo "FTPS container fixture created: $run_id"

timeout 15 docker --context "$context" inspect "$prepare_id" > "$state_dir/prepare-inspect.json"
timeout 15 python3 - "$state_dir/prepare-inspect.json" "$project" "$secret_volume" "$trust_volume" <<'PY'
import json, pathlib, sys
documents = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(documents, list) or len(documents) != 1:
    raise SystemExit("secret preparer inspection shape changed")
container = documents[0]
host = container["HostConfig"]
if container["Config"]["User"] != "0:0":
    raise SystemExit("secret preparer configured identity changed")
if host["ReadonlyRootfs"] is not True or host["Privileged"] is not False:
    raise SystemExit("secret preparer filesystem or privilege policy changed")
if host["CapDrop"] != ["ALL"] or set(host.get("CapAdd") or []) != {"CAP_SETGID", "CAP_SETUID"}:
    raise SystemExit("secret preparer capability policy changed")
if host["SecurityOpt"] != ["no-new-privileges:true"] or host.get("Devices") not in (None, []):
    raise SystemExit("secret preparer security policy changed")
if host.get("Sysctls") not in (None, {}) or host["PortBindings"] != {}:
    raise SystemExit("secret preparer network policy changed")
if host["NetworkMode"] != f"{sys.argv[2]}_private":
    raise SystemExit("secret preparer network attachment changed")
mounts = {
    mount["Destination"]: (mount["Type"], mount["RW"], mount["Name"] if mount["Type"] == "volume" else None)
    for mount in container["Mounts"]
}
expected = {
    "/fixture/prepare.py": ("bind", False, None),
    "/input": ("bind", False, None),
    "/run/klove-secrets": ("volume", True, sys.argv[3]),
    "/var/lib/klove/ftps-staging": ("volume", True, sys.argv[4]),
}
if mounts != expected:
    raise SystemExit("secret preparer mount set changed")
PY

verify_daemon
timeout 15 docker --context "$context" start "$prepare_id" >/dev/null
[ "$(timeout 30 docker --context "$context" wait "$prepare_id")" = "0" ] || {
    timeout 15 docker --context "$context" logs --tail 50 "$prepare_id" >&2 || true
    echo "secret preparation failed" >&2
    exit 1
}
chmod 0700 "$state_dir/input"
chmod 0600 "$state_dir/input/"*
echo "FTPS container secrets prepared: $run_id"

verify_daemon
timeout 15 docker --context "$context" start "$contract_id" >/dev/null
timeout 15 docker --context "$context" start "$runtime_id" >/dev/null
[ "$(timeout 45 docker --context "$context" wait "$contract_id")" = "0" ] || {
    timeout 15 docker --context "$context" logs --tail 50 "$contract_id" >&2 || true
    echo "private FTPS contract failed" >&2
    exit 1
}
echo "FTPS private peer completed: $run_id"

verify_daemon
echo "FTPS runtime confinement inspection started: $run_id"
timeout 15 docker --context "$context" exec "$runtime_id" cat /proc/1/status > "$state_dir/runtime-status"
timeout 15 python3 - "$state_dir/runtime-status" <<'PY'
import pathlib, sys
values = {}
for line in pathlib.Path(sys.argv[1]).read_text(encoding="ascii").splitlines():
    if ":" in line:
        name, value = line.split(":", 1)
        values[name] = value.strip()
for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
    if values.get(name) != "0000000000000000":
        raise SystemExit(f"runtime {name} was not empty")
if values.get("NoNewPrivs") != "1":
    raise SystemExit("runtime no-new-privileges was not active")
PY
[ "$(timeout 15 docker --context "$context" exec "$runtime_id" id -u)" = "10001" ]
[ "$(timeout 15 docker --context "$context" exec "$runtime_id" id -g)" = "10001" ]
echo "FTPS runtime identity and effective capabilities verified: $run_id"
timeout 15 docker --context "$context" inspect "$runtime_id" > "$state_dir/runtime-inspect.json"
timeout 15 python3 - "$state_dir/runtime-inspect.json" "$project" "$KLOVE_FTPS_SERVICE_IP" "$secret_volume" "$state_volume" "$staging_volume" <<'PY'
import json, pathlib, sys
document = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(document, list) or len(document) != 1:
    raise SystemExit("runtime inspection shape changed")
container = document[0]
host = container["HostConfig"]
if container["Config"]["User"] != "10001:10001":
    raise SystemExit("runtime configured identity changed")
if host["ReadonlyRootfs"] is not True or host["Privileged"] is not False:
    raise SystemExit("runtime filesystem or privilege policy changed")
if host["CapDrop"] != ["ALL"] or host.get("CapAdd") not in (None, []):
    raise SystemExit("runtime capability policy changed")
if host["SecurityOpt"] != ["no-new-privileges:true"]:
    raise SystemExit("runtime security options changed")
if host["Sysctls"] != {"net.ipv4.ip_unprivileged_port_start": "0"}:
    raise SystemExit("runtime sysctl policy changed")
if host.get("Devices") not in (None, []):
    raise SystemExit("runtime device policy changed")
if host["PidMode"] != "" or host["IpcMode"] not in ("", "private"):
    raise SystemExit("runtime namespace policy changed")
if host["NetworkMode"] != f"{sys.argv[2]}_private" or host["PortBindings"] != {}:
    raise SystemExit("runtime network mode or host ports changed")
if host["PidsLimit"] != 64 or host["Memory"] != 268435456 or host["NanoCpus"] != 1000000000:
    raise SystemExit("runtime resource policy changed")
if host["Tmpfs"] != {"/tmp": "size=16m,mode=1777"}:
    raise SystemExit("runtime tmpfs policy changed")
mounts = {
    mount["Destination"]: (mount["Type"], mount["RW"], mount["Name"] if mount["Type"] == "volume" else None)
    for mount in container["Mounts"]
}
expected_mounts = {
    "/fixture/runtime.py": ("bind", False, None),
    "/run/klove-secrets": ("volume", False, sys.argv[4]),
    "/var/lib/klove": ("volume", True, sys.argv[5]),
    "/var/lib/klove/ftps-staging": ("volume", True, sys.argv[6]),
}
if mounts != expected_mounts:
    raise SystemExit("runtime mount set changed")
networks = container["NetworkSettings"]["Networks"]
if set(networks) != {f"{sys.argv[2]}_private"}:
    raise SystemExit("runtime network attachment set changed")
if next(iter(networks.values()))["IPAddress"] != sys.argv[3]:
    raise SystemExit("runtime private address changed")
PY
[ "$(timeout 15 docker --context "$context" exec "$runtime_id" cat /proc/sys/net/ipv4/ip_unprivileged_port_start)" = "0" ]
[ "$(cat /proc/sys/net/ipv4/ip_unprivileged_port_start)" = "$host_low_port_before" ]
echo "FTPS runtime filesystem and low-port confinement verified: $run_id"
echo "FTPS runtime namespaces and resource bounds verified: $run_id"
timeout 15 docker --context "$context" inspect "$contract_id" > "$state_dir/contract-inspect.json"
timeout 15 python3 - "$state_dir/contract-inspect.json" "$project" "$KLOVE_FTPS_CLIENT_IP" "$staging_volume" "$trust_volume" <<'PY'
import json, pathlib, sys
documents = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(documents, list) or len(documents) != 1:
    raise SystemExit("private peer inspection shape changed")
container = documents[0]
host = container["HostConfig"]
if container["Config"]["User"] != "10001:10001":
    raise SystemExit("private peer configured identity changed")
if host["ReadonlyRootfs"] is not True or host["Privileged"] is not False:
    raise SystemExit("private peer filesystem or privilege policy changed")
if host["CapDrop"] != ["ALL"] or host.get("CapAdd") not in (None, []):
    raise SystemExit("private peer capability policy changed")
if host["SecurityOpt"] != ["no-new-privileges:true"] or host.get("Devices") not in (None, []):
    raise SystemExit("private peer security policy changed")
if host.get("Sysctls") not in (None, {}) or host["PortBindings"] != {}:
    raise SystemExit("private peer network policy changed")
if host["PidsLimit"] != 32 or host["Memory"] != 67108864 or host["NanoCpus"] != 500000000:
    raise SystemExit("private peer resource policy changed")
if host["Tmpfs"] != {"/tmp": "size=4m,mode=1777"}:
    raise SystemExit("private peer tmpfs policy changed")
mounts = {
    mount["Destination"]: (mount["Type"], mount["RW"], mount["Name"] if mount["Type"] == "volume" else None)
    for mount in container["Mounts"]
}
expected = {
    "/fixture/probe.py": ("bind", False, None),
    "/staging": ("volume", False, sys.argv[4]),
    "/trust": ("volume", False, sys.argv[5]),
}
if mounts != expected:
    raise SystemExit("private peer mount set changed")
networks = container["NetworkSettings"]["Networks"]
if set(networks) != {f"{sys.argv[2]}_private"}:
    raise SystemExit("private peer network attachment set changed")
network = next(iter(networks.values()))
if network.get("IPAMConfig", {}).get("IPv4Address") != sys.argv[3]:
    raise SystemExit("private peer configured address changed")
if network["IPAddress"] not in ("", sys.argv[3]):
    raise SystemExit("private peer observed an unexpected address")
PY
echo "FTPS private peer confinement verified: $run_id"
[ "$(timeout 15 docker --context "$context" network inspect --format '{{.Internal}}' "$network_id")" = "true" ]
[ "$(timeout 15 docker --context "$context" network inspect --format '{{(index .IPAM.Config 0).Subnet}}' "$network_id")" = "$KLOVE_FTPS_SUBNET" ]
echo "FTPS private network identity verified: $run_id"
exposed_ports=$(timeout 15 docker --context "$context" image inspect --format '{{json .Config.ExposedPorts}}' "$image_id")
printf 'FTPS image exposed-port manifest: %s\n' "$exposed_ports"
python3 - "$exposed_ports" <<'PY'
import json, sys
actual = set(json.loads(sys.argv[1]))
expected = {"8080/tcp", "990/tcp", *(f"{port}/tcp" for port in range(50000, 50010))}
if actual != expected:
    raise SystemExit("image exposed-port manifest is not exact")
PY
echo "FTPS image listener manifest verified: $run_id"
timeout 15 docker --context "$context" exec "$runtime_id" python -c 'import pathlib,stat,tomllib; c=tomllib.loads(pathlib.Path("/run/klove-secrets/config.toml").read_text()); assert c["control"]["enabled"] is False; assert c["dispatch"]["enabled"] is False; assert c["grove_bridge"]["ftps_control_port"] == 990; assert c["grove_bridge"]["ftps_passive_port_min"] == 50000; assert c["grove_bridge"]["ftps_passive_port_max"] == 50009; files=tuple(pathlib.Path("/run/klove-secrets").iterdir()); assert files and all(stat.S_IMODE(path.stat().st_mode) == 0o600 and path.stat().st_uid == 10001 and path.stat().st_gid == 10001 for path in files)'
echo "FTPS private configuration and secret modes verified: $run_id"
openssl x509 -in "$state_dir/input/tls-certificate.pem" -noout -checkip "$KLOVE_FTPS_SERVICE_IP" >/dev/null
timeout 15 docker --context "$context" logs "$contract_id"
echo "FTPS production-container contract passed: $run_id"
exit 0
