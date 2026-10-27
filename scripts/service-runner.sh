#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
BOSTO_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
BOSTO_FPS=${BOSTO_FPS:-60}
BOSTO_QUALITY=${BOSTO_QUALITY:-90}

fail() { printf 'bosto-display: %s\n' "$*" >&2; exit 78; }
[[ $BOSTO_FPS =~ ^([1-9]|[1-5][0-9]|60)$ ]] || fail 'BOSTO_FPS must be 1..60'
[[ $BOSTO_QUALITY =~ ^([1-9]|[1-9][0-9]|100)$ ]] || fail 'BOSTO_QUALITY must be 1..100'
(( EUID == 0 )) || fail 'The system service requires root'
[[ -x "$BOSTO_DIR/bosto-display" ]] || fail 'Installed driver binary is missing'
# Hold the lock across exec. Rapid replug can overlap old/new device units.
exec 9>/run/lock/bosto-display.lock
if ! flock -n 9; then
    printf '%s\n' 'bosto-display: another display service is still stopping; retrying' >&2
    exit 75
fi

if [[ ! -d /sys/module/evdi ]]; then
    # Prefer a DKMS/distribution module for the currently running kernel.
    if ! modprobe evdi; then
        [[ -f "$BOSTO_DIR/evdi.ko" ]] || fail 'EVDI is unavailable; rebuild/install the module for this kernel'
        BOSTO_VERMAGIC=$(modinfo -F vermagic "$BOSTO_DIR/evdi.ko") || fail 'Cannot inspect the bundled EVDI module'
        [[ ${BOSTO_VERMAGIC%% *} == "$(uname -r)" ]] || fail 'Bundled EVDI targets another kernel; install matching headers and rebuild, or use DKMS'
        BOSTO_DEPS=$(modinfo -F depends "$BOSTO_DIR/evdi.ko") || fail 'Cannot inspect EVDI dependencies'
        IFS=, read -r -a BOSTO_MODULES <<< "$BOSTO_DEPS"
        for BOSTO_MODULE in "${BOSTO_MODULES[@]}"; do
            [[ -z $BOSTO_MODULE ]] && continue
            [[ $BOSTO_MODULE =~ ^[a-zA-Z0-9_]+$ ]] || fail 'Invalid EVDI dependency name'
            modprobe "$BOSTO_MODULE" || fail "Cannot load dependency $BOSTO_MODULE"
        done
        insmod "$BOSTO_DIR/evdi.ko" || fail 'Cannot load EVDI; inspect the kernel journal and Secure Boot status'
    fi
fi
exec "$BOSTO_DIR/bosto-display" --run --fps "$BOSTO_FPS" --quality "$BOSTO_QUALITY"
