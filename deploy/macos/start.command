#!/bin/zsh
set -eu
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
"$TASK_ROOT/.venv/bin/python" "$TASK_ROOT/deploy/macos/manage.py" start
open http://127.0.0.1:8000
