#!/usr/bin/env bash
# Restart the deployed services without building or replacing their binaries.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/python3 "$DIR/scripts/restart_services.py" "$@"
