#!/usr/bin/env bash
# Keep the public shorthand on the same supported local installation path.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/install-internal.sh" "$@"
