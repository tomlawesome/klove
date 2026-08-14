#!/usr/bin/env sh
set -eu

if [ "$#" -ne 0 ]; then
    echo "usage: $0" >&2
    exit 2
fi

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
. "$script_dir/ratos-emulation-lib.sh"
repo_root=$(CDPATH='' cd -- "$script_dir/.." && pwd)
ratos_integration_root="$repo_root/.klove-integration"
ratos_state_dir="$ratos_integration_root/ratos-v2.1.0"
ratos_runtime_dir="$ratos_state_dir/runtime"
ratos_evidence_dir="$ratos_state_dir/evidence"
ratos_origin="$ratos_state_dir/origin"
ratos_active="$ratos_runtime_dir/active"
ratos_probe="$ratos_runtime_dir/probe.json"
ratos_probe_succeeded="$ratos_runtime_dir/probe-succeeded"
ratos_contract_prepared="$ratos_runtime_dir/contract-prepared.json"
ratos_contract_evidence="$ratos_runtime_dir/contract.json"
ratos_contract_succeeded="$ratos_runtime_dir/contract-succeeded"

ratos_require_origin "$ratos_origin"
ratos_require_prepared_inputs
ratos_require_active "$ratos_active"
ratos_require_private_file "$ratos_runtime_dir/$ratos_overlay_name" 666
ratos_require_private_file "$ratos_probe" 600
ratos_require_private_file "$ratos_probe_succeeded" 600
ratos_require_absent "$ratos_contract_prepared"
ratos_require_absent "$ratos_contract_evidence"
ratos_require_absent "$ratos_contract_succeeded"
for ratos_contract_name in \
    "$ratos_proxy_container" "$ratos_klove_container" "$ratos_contract_container"
do
    if timeout 15 docker container inspect "$ratos_contract_name" >/dev/null 2>&1; then
        echo "an unbound container already uses a guarded RatOS contract name" >&2
        exit 1
    fi
done
if [ "$(timeout 15 docker inspect --format '{{.State.Status}}' "$ratos_container")" != running ]; then
    echo "the RatOS emulator must be running for the production contract" >&2
    exit 1
fi

umask 077
prepared_partial=$(mktemp "$ratos_runtime_dir/contract-prepared.XXXXXX")
contract_partial=$(mktemp "$ratos_runtime_dir/contract.XXXXXX")
contract_output=$(mktemp "$ratos_runtime_dir/contract-output.XXXXXX")
contract_success_partial=$(mktemp "$ratos_runtime_dir/contract-succeeded.XXXXXX")
chmod 0600 "$prepared_partial" "$contract_partial" \
    "$contract_output" "$contract_success_partial"
cleanup_contract_partials() {
    rm -f -- "$prepared_partial" "$contract_partial" \
        "$contract_output" "$contract_success_partial"
}
trap cleanup_contract_partials EXIT
trap 'exit 1' HUP INT TERM

if ! timeout 1200 docker exec "$ratos_container" \
    /usr/local/bin/python /opt/klove-ratos/tool.py contract-prepare \
    > "$prepared_partial"; then
    echo "RatOS controlled configuration preparation failed; retain runtime for teardown" >&2
    exit 1
fi
mv -- "$prepared_partial" "$ratos_contract_prepared"

timeout 30 docker run \
    --name "$ratos_proxy_container" \
    --detach \
    --network "container:$ratos_container" \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --pids-limit 32 \
    --memory 96m \
    --cpus 0.5 \
    --log-driver local \
    --log-opt max-size=512k \
    --log-opt max-file=2 \
    --tmpfs /tmp:rw,noexec,nosuid,size=4m \
    --label "io.klove.ratos.state=$ratos_state_dir" \
    --label "io.klove.ratos.source-digest=$ratos_stored_source_digest" \
    --label "io.klove.ratos.role=moonraker-proxy" \
    --env KLOVE_TEST_PROXY_LISTEN_HOST=127.0.0.1 \
    --env KLOVE_TEST_PROXY_LISTEN_PORT=27125 \
    --env KLOVE_TEST_PROXY_CONTROL_LISTEN_HOST=127.0.0.1 \
    --env KLOVE_TEST_PROXY_CONTROL_LISTEN_PORT=9126 \
    --env KLOVE_TEST_PROXY_UPSTREAM_URL=http://127.0.0.1:18080 \
    --env KLOVE_TEST_MOONRAKER_HOST_HEADER=ratos.local \
    "$ratos_tool_image_id" \
    /usr/local/bin/python /opt/klove-ratos/contract/proxy.py >/dev/null
ratos_require_contract_container \
    "$ratos_proxy_container" "$ratos_tool_image_id" moonraker-proxy
timeout 180 docker exec "$ratos_container" \
    /usr/local/bin/python /opt/klove-ratos/tool.py wait-service proxy

timeout 30 docker run \
    --name "$ratos_klove_container" \
    --detach \
    --network "container:$ratos_container" \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --pids-limit 64 \
    --memory 256m \
    --cpus 1 \
    --log-driver local \
    --log-opt max-size=512k \
    --log-opt max-file=2 \
    --tmpfs /tmp:rw,noexec,nosuid,size=16m \
    --label "io.klove.ratos.state=$ratos_state_dir" \
    --label "io.klove.ratos.source-digest=$ratos_stored_source_digest" \
    --label "io.klove.ratos.role=production-klove" \
    --mount "type=volume,source=$ratos_secret_volume,target=/run/klove-secrets,readonly" \
    "$ratos_klove_image_id" \
    --config /run/klove-secrets/config.toml >/dev/null
ratos_require_contract_container \
    "$ratos_klove_container" "$ratos_klove_image_id" production-klove
timeout 180 docker exec "$ratos_container" \
    /usr/local/bin/python /opt/klove-ratos/tool.py wait-service klove

if ! timeout 600 docker run \
    --name "$ratos_contract_container" \
    --network "container:$ratos_container" \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --pids-limit 24 \
    --memory 96m \
    --cpus 0.5 \
    --log-driver local \
    --log-opt max-size=512k \
    --log-opt max-file=2 \
    --tmpfs /tmp:rw,noexec,nosuid,size=4m \
    --tmpfs /run/test-state:rw,noexec,nosuid,size=1m,mode=0700,uid=10001,gid=10001 \
    --label "io.klove.ratos.state=$ratos_state_dir" \
    --label "io.klove.ratos.source-digest=$ratos_stored_source_digest" \
    --label "io.klove.ratos.role=contract-runner" \
    --mount "type=volume,source=$ratos_secret_volume,target=/run/klove-secrets,readonly" \
    --env KLOVE_TEST_KLOVE_URL=http://127.0.0.1:8080 \
    --env KLOVE_TEST_MOONRAKER_URL=http://127.0.0.1:18080 \
    --env KLOVE_TEST_MOONRAKER_PROXY_URL=http://127.0.0.1:27125 \
    --env KLOVE_TEST_PROXY_CONTROL_URL=http://127.0.0.1:9126 \
    --env KLOVE_TEST_MOONRAKER_HOST_HEADER=ratos.local \
    --env KLOVE_TEST_MOONRAKER_AUTH_EXPECTATION=trusted \
    "$ratos_tool_image_id" \
    /usr/local/bin/python /opt/klove-ratos/contract/exercise_contract.py \
    > "$contract_output"; then
    echo "RatOS production Klove contract failed; retain runtime for exact teardown" >&2
    exit 1
fi
ratos_require_contract_container \
    "$ratos_contract_container" "$ratos_tool_image_id" contract-runner

if ! timeout 180 docker exec "$ratos_container" \
    /usr/local/bin/python /opt/klove-ratos/tool.py contract-evidence \
    > "$contract_partial"; then
    echo "RatOS production contract evidence reconciliation failed" >&2
    exit 1
fi
mv -- "$contract_partial" "$ratos_contract_evidence"
cat "$ratos_contract_evidence"
mv -- "$contract_success_partial" "$ratos_contract_succeeded"
rm -f -- "$contract_output"
trap - EXIT HUP INT TERM
echo "RatOS v2.1.0 production Klove job-control contract passed"
