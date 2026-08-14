#!/usr/bin/env sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: $0 /path/to/2026-03-04-RatOS-2.1.0-raspberry-rpi32.img.xz" >&2
    exit 2
fi

repo_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
active=0
cleanup() {
    original_status=$?
    trap - EXIT HUP INT TERM
    if [ "$active" -eq 1 ]; then
        if ! "$repo_root/scripts/ratos-emulation-down.sh"; then
            echo "RatOS automatic teardown failed; credential volume/COW may remain under the exact recorded runtime" >&2
            if [ "$original_status" -eq 0 ]; then
                exit 1
            fi
        fi
    fi
    exit "$original_status"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

"$repo_root/scripts/ratos-emulation-prepare.sh" "$1"
"$repo_root/scripts/ratos-emulation-up.sh"
active=1
"$repo_root/scripts/ratos-emulation-probe.sh"
"$repo_root/scripts/ratos-emulation-contract.sh"
"$repo_root/scripts/ratos-emulation-down.sh"
active=0
trap - EXIT HUP INT TERM
