#!/usr/bin/env bash
set -euo pipefail

BOSTO_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
if (( EUID != 0 )); then
    printf '%s\n' 'Run this display launcher with sudo.' >&2
    exit 1
fi
if [[ ! -x "$BOSTO_DIR/build/bosto-display" ]]; then
    printf '%s\n' 'Driver binary is missing; run make in the project directory.' >&2
    exit 1
fi
if [[ ! -d /sys/module/evdi ]]; then
    if [[ ! -f "$BOSTO_DIR/build/evdi.ko" ]]; then
        printf '%s\n' 'EVDI module is missing; run make module.' >&2
        exit 1
    fi
    BOSTO_VERMAGIC=$(modinfo -F vermagic "$BOSTO_DIR/build/evdi.ko")
    if [[ ${BOSTO_VERMAGIC%% *} != "$(uname -r)" ]]; then
        printf '%s\n' 'EVDI was built for a different kernel; run make module before launching.' >&2
        exit 1
    fi
    modprobe drm_ttm_helper
    insmod "$BOSTO_DIR/build/evdi.ko"
fi
exec "$BOSTO_DIR/build/bosto-display" --run "$@"
