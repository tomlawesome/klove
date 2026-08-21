#!/usr/bin/env sh

ratos_asset_name=2026-03-04-RatOS-2.1.0-raspberry-rpi32.img.xz
ratos_raw_name=2026-03-04-RatOS-2.1.0-raspberry-rpi32.img
ratos_asset_bytes=2125243800
ratos_asset_sha256=513465cf6b233d73e5c9ed048568493894d77c7c67e93586609f9b7fbc182153
ratos_tool_tag=klove-ratos-qemu:v2.1.0-spike
ratos_klove_tag=klove-ratos-contract:v2.1.0-local
ratos_container=klove-ratos-v2-1-0
ratos_proxy_container=klove-ratos-v2-1-0-proxy
ratos_klove_container=klove-ratos-v2-1-0-klove
ratos_contract_container=klove-ratos-v2-1-0-contract
ratos_secret_volume=klove-ratos-v2-1-0-secrets
ratos_overlay_name=ratos-rpi32-run.qcow2

ratos_stage_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
. "$ratos_stage_dir/ratos-emulation-ownership.sh"
. "$ratos_stage_dir/ratos-emulation-qemu.sh"
. "$ratos_stage_dir/ratos-emulation-evidence.sh"
