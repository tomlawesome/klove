#!/usr/bin/env sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: $0 /path/to/2026-03-04-RatOS-2.1.0-raspberry-rpi32.img.xz" >&2
    exit 2
fi

repo_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
active=0
cleanup() {
    if [ "$active" -eq 1 ]; then
        "$repo_root/scripts/ratos-emulation-down.sh" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT HUP INT TERM

"$repo_root/scripts/ratos-emulation-prepare.sh" "$1"
"$repo_root/scripts/ratos-emulation-up.sh"
active=1
"$repo_root/scripts/ratos-emulation-probe.sh"
"$repo_root/scripts/ratos-emulation-down.sh"
active=0
trap - EXIT HUP INT TERM
