#!/bin/sh
# Install the separate data-only runtime on an explicitly selected Linux host.
set -eu
ATLAS_DEPLOY_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 -I "$ATLAS_DEPLOY_ROOT/scripts/install_collector.py" "$@"
