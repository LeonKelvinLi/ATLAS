#!/bin/sh
set -eu
ATLAS_COLLECT_SOURCE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec python3 -I "$ATLAS_COLLECT_SOURCE/scripts/atlas_collect.py" "$@"
