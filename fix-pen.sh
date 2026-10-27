#!/usr/bin/env bash
# Install the exact inspected Stylus collection's native axis metadata quirk.
set -euo pipefail
BOSTO_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
BOSTO_SOURCE="$BOSTO_DIR/udev/99-bosto-bt101a.hwdb"
BOSTO_TARGET=/etc/udev/hwdb.d/99-bosto-bt101a.hwdb
if (( EUID != 0 )); then
    printf '%s\n' 'Run this one-time pen fix with sudo.' >&2
    exit 1
fi
if [[ ${1:-} == --undo ]]; then
    if [[ -f "$BOSTO_TARGET" ]] && cmp -s -- "$BOSTO_SOURCE" "$BOSTO_TARGET"; then
        rm -- "$BOSTO_TARGET"
        systemd-hwdb update
        printf '%s\n' 'Pen override removed. Reconnect the tablet to restore the original kernel ranges.'
        exit 0
    fi
    printf '%s\n' 'No matching installed override; refusing to remove a different file.' >&2
    exit 1
fi
if (( $# )); then
    printf '%s\n' 'Usage: fix-pen.sh [--undo]' >&2
    exit 2
fi
# Refuse another Bosto revision that happens to reuse the USB ID.
python3 - "$BOSTO_DIR" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'tools'))
from calibrate_pen import find_device
device = find_device()
print('Verified BT-101A Stylus descriptor:', device['descriptor_sha256'])
PY
if [[ -L "$BOSTO_TARGET" ]] || { [[ -e "$BOSTO_TARGET" ]] && ! cmp -s -- "$BOSTO_SOURCE" "$BOSTO_TARGET"; }; then
    printf '%s\n' 'A different local pen override already exists; refusing to overwrite it.' >&2
    exit 1
fi
install -d -m 755 /etc/udev/hwdb.d
install -m 644 -- "$BOSTO_SOURCE" "$BOSTO_TARGET"
if ! systemd-hwdb update; then
    printf '%s\n' 'hwdb compilation failed; remove the new override with fix-pen.sh --undo.' >&2
    exit 1
fi
printf '%s\n' 'Installed Stylus X range 0..17920 and physical axis resolutions 83/76.'
printf '%s\n' 'Stop the running display driver (Ctrl+C), reconnect the tablet, then start run-display.sh again.'
