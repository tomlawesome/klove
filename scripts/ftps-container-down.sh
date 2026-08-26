#!/usr/bin/env sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: $0 RUN_ID" >&2
    exit 2
fi
run_id=$1
case "$run_id" in
    *[!a-z0-9-]* | "" | -* | *-) echo "invalid FTPS container RUN_ID" >&2; exit 2 ;;
esac
if [ "${#run_id}" -gt 32 ]; then
    echo "FTPS container RUN_ID exceeds 32 characters" >&2
    exit 2
fi
for prerequisite in docker timeout id stat sed wc; do
    command -v "$prerequisite" >/dev/null 2>&1 || {
        echo "FTPS container recovery prerequisite is unavailable" >&2
        exit 1
    }
done

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
state_dir="$repo_root/.klove-integration/ftps-container/$run_id"
origin="$state_dir/origin"
if [ ! -f "$origin" ] || [ -L "$origin" ]; then
    echo "FTPS container recovery origin is unavailable" >&2
    exit 1
fi
[ "$(stat -c '%u:%a' "$origin")" = "$(id -u):600" ] || {
    echo "FTPS container recovery origin identity or mode changed" >&2
    exit 1
}
line_count=$(wc -l < "$origin")
case "$line_count" in 17 | 21 | 22 | 30) ;; *) echo "FTPS container recovery origin is malformed" >&2; exit 1 ;; esac

project=$(sed -n '1p' "$origin")
context=$(sed -n '2p' "$origin")
daemon_id=$(sed -n '3p' "$origin")
image=$(sed -n '5p' "$origin")
revision=$(sed -n '6p' "$origin")
source_digest=$(sed -n '7p' "$origin")
network_name=$(sed -n '10p' "$origin")
secret_volume=$(sed -n '11p' "$origin")
state_volume=$(sed -n '12p' "$origin")
staging_volume=$(sed -n '13p' "$origin")
trust_volume=$(sed -n '14p' "$origin")
prepare_name=$(sed -n '15p' "$origin")
runtime_name=$(sed -n '16p' "$origin")
contract_name=$(sed -n '17p' "$origin")
[ "$project" = "klove-ftps-$run_id" ]
[ "$image" = "klove-ftps-contract:$run_id" ]
[ "$network_name" = "${project}_private" ]
[ "$secret_volume" = "${project}_runtime-secrets" ]
[ "$state_volume" = "${project}_klove-state" ]
[ "$staging_volume" = "${project}_staging-state" ]
[ "$trust_volume" = "${project}_contract-trust" ]
[ "$prepare_name" = "${project}-prepare-1" ]
[ "$runtime_name" = "${project}-klove-1" ]
[ "$contract_name" = "${project}-contract-1" ]
case "$revision:$source_digest" in
    *[!0-9a-f:]* | *:*:*) echo "FTPS container recovery digests are malformed" >&2; exit 1 ;;
esac
[ "${#revision}" -eq 40 ] && [ "${#source_digest}" -eq 64 ]
[ "$(timeout 15 docker context show)" = "$context" ]
[ "$(timeout 15 docker --context "$context" info --format '{{.ID}}')" = "$daemon_id" ]

container_ids=""
for role_name in "prepare:$prepare_name" "runtime:$runtime_name" "contract:$contract_name"; do
    role=${role_name%%:*}; name=${role_name#*:}
    id=$(timeout 15 docker --context "$context" inspect --format '{{.Id}}' "$name" 2>/dev/null || true)
    [ -z "$id" ] && continue
    actual_project=$(timeout 15 docker --context "$context" inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' "$id" 2>/dev/null || true)
    actual_role=$(timeout 15 docker --context "$context" inspect --format '{{ index .Config.Labels "io.klove.ftps-container.role" }}' "$id" 2>/dev/null || true)
    [ "$actual_project" = "$project" ] && [ "$actual_role" = "$role" ] || {
        echo "FTPS container recovery refused a changed container" >&2
        exit 1
    }
    container_ids="$container_ids $id"
done
for id in $container_ids; do
    timeout 30 docker --context "$context" container rm --force "$id" >/dev/null
done

network_id=$(timeout 15 docker --context "$context" network inspect --format '{{.Id}}' "$network_name" 2>/dev/null || true)
if [ -n "$network_id" ]; then
    [ "$(timeout 15 docker --context "$context" network inspect --format '{{ index .Labels "com.docker.compose.project" }}' "$network_id")" = "$project" ]
    [ "$(timeout 15 docker --context "$context" network inspect --format '{{ index .Labels "com.docker.compose.network" }}' "$network_id")" = "private" ]
    [ "$(timeout 15 docker --context "$context" network inspect --format '{{ index .Labels "io.klove.ftps-container.role" }}' "$network_id")" = "network" ]
    timeout 30 docker --context "$context" network rm "$network_id" >/dev/null
fi

for volume in "$secret_volume" "$state_volume" "$staging_volume" "$trust_volume"; do
    actual_project=$(timeout 15 docker --context "$context" volume inspect --format '{{ index .Labels "com.docker.compose.project" }}' "$volume" 2>/dev/null || true)
    [ -z "$actual_project" ] && continue
    expected_role=${volume#"${project}_"}
    actual_role=$(timeout 15 docker --context "$context" volume inspect --format '{{ index .Labels "com.docker.compose.volume" }}' "$volume")
    [ "$actual_project" = "$project" ] && [ "$actual_role" = "$expected_role" ] || {
        echo "FTPS container recovery refused a changed volume" >&2
        exit 1
    }
    timeout 30 docker --context "$context" volume rm "$volume" >/dev/null
done

if [ "$line_count" -ge 22 ]; then
    image_id=$(sed -n '22p' "$origin")
    actual_image_id=$(timeout 15 docker --context "$context" image inspect --format '{{.Id}}' "$image" 2>/dev/null || true)
    if [ -n "$actual_image_id" ]; then
        [ "$actual_image_id" = "$image_id" ]
        [ "$(timeout 15 docker --context "$context" image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image")" = "$revision" ]
        [ "$(timeout 15 docker --context "$context" image inspect --format '{{ index .Config.Labels "io.github.tomlawesome.klove.source-digest" }}' "$image")" = "$source_digest" ]
        timeout 30 docker --context "$context" image rm "$image" >/dev/null
    fi
fi

rm -rf -- "$state_dir"
echo "FTPS container recovery removed exact owned resources: $run_id"
