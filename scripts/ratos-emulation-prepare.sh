#!/usr/bin/env sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: $0 /path/to/2026-03-04-RatOS-2.1.0-raspberry-rpi32.img.xz" >&2
    exit 2
fi

source_archive=$1
script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
. "$script_dir/ratos-emulation-lib.sh"
repo_root=$(CDPATH='' cd -- "$script_dir/.." && pwd)
ratos_integration_root="$repo_root/.klove-integration"
ratos_state_dir="$ratos_integration_root/ratos-v2.1.0"
ratos_runtime_dir="$ratos_state_dir/runtime"
ratos_evidence_dir="$ratos_state_dir/evidence"
ratos_origin="$ratos_state_dir/origin"
ratos_active="$ratos_runtime_dir/active"
dockerfile="$repo_root/tests/integration/ratos-emulation/Dockerfile"
build_context="$repo_root/tests/integration/ratos-emulation"

ratos_require_host_tools
umask 077
ratos_ensure_private_dir "$ratos_integration_root"
ratos_ensure_private_dir "$ratos_state_dir"
ratos_ensure_private_dir "$ratos_runtime_dir"
ratos_ensure_private_dir "$ratos_evidence_dir"
ratos_validate_state_tree

if [ ! -f "$source_archive" ] || [ -L "$source_archive" ]; then
    echo "the RatOS source asset must be a regular non-symlink file" >&2
    exit 1
fi
if [ "$(basename -- "$source_archive")" != "$ratos_asset_name" ]; then
    echo "the RatOS source asset name is not the exact accepted release asset" >&2
    exit 1
fi
if [ "$(stat --format='%s' -- "$source_archive")" != "$ratos_asset_bytes" ]; then
    echo "the RatOS source asset byte size does not match v2.1.0" >&2
    exit 1
fi
source_sha=$(sha256sum -- "$source_archive")
source_sha=${source_sha%% *}
if [ "$source_sha" != "$ratos_asset_sha256" ]; then
    echo "the RatOS source asset SHA-256 does not match v2.1.0" >&2
    exit 1
fi

ratos_capture_daemon
if [ -e "$ratos_active" ] || [ -L "$ratos_active" ]; then
    ratos_require_private_file "$ratos_active" 600
    echo "an active RatOS emulator is recorded; tear it down before preparing" >&2
    exit 1
fi
if [ -e "$ratos_origin" ] || [ -L "$ratos_origin" ]; then
    ratos_require_replaceable_origin "$ratos_origin"
fi

destination="$ratos_state_dir/$ratos_asset_name"
if [ -e "$destination" ] || [ -L "$destination" ]; then
    ratos_require_private_file "$destination" 400
else
    available_bytes=$(df --output=avail -B1 "$ratos_state_dir" | tail -n 1 | tr -d ' ')
    if [ "$available_bytes" -lt 16000000000 ]; then
        echo "a fresh RatOS preparation requires at least 16 GB free" >&2
        exit 1
    fi
    partial=$(mktemp "$ratos_state_dir/asset.XXXXXX")
    cleanup_asset_partial() {
        rm -f -- "$partial"
    }
    trap cleanup_asset_partial EXIT HUP INT TERM
    cp --reflink=auto -- "$source_archive" "$partial"
    chmod 0400 "$partial"
    mv -- "$partial" "$destination"
    trap - EXIT HUP INT TERM
fi

ratos_capture_source_state
timeout 1200 docker build \
    --build-arg "KLOVE_SOURCE_REVISION=$ratos_source_revision" \
    --build-arg "KLOVE_SOURCE_DIGEST=$ratos_source_digest" \
    --file "$dockerfile" \
    --tag "$ratos_tool_tag" \
    "$build_context"
ratos_tool_image_id=$(timeout 15 docker image inspect --format '{{.Id}}' "$ratos_tool_tag")

origin_partial=$(mktemp "$ratos_state_dir/origin.XXXXXX")
identities_partial=$(mktemp "$ratos_state_dir/identities.XXXXXX")
cleanup_prepare_partials() {
    rm -f -- "$origin_partial" "$identities_partial"
}
trap cleanup_prepare_partials EXIT HUP INT TERM
printf '%s\n%s\n%s\n%s\n%s\n' \
    "$ratos_context" "$ratos_daemon_id" "$ratos_tool_image_id" \
    "$ratos_source_revision" "$ratos_source_digest" > "$origin_partial"
chmod 0600 "$origin_partial"
mv -- "$origin_partial" "$ratos_origin"

ratos_run_prepare_tool > "$identities_partial"
chmod 0600 "$identities_partial"
mv -- "$identities_partial" "$ratos_state_dir/identities.json"
trap - EXIT HUP INT TERM

ratos_require_prepared_inputs
echo "RatOS v2.1.0 emulation inputs prepared and verified"
