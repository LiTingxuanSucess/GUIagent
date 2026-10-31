#!/bin/zsh
set -eu
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
"$TASK_ROOT/.venv/bin/python" "$TASK_ROOT/deploy/macos/manage.py" stop
