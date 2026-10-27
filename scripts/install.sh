#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
BOSTO_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec python3 "$BOSTO_DIR/install.py" "$@"
