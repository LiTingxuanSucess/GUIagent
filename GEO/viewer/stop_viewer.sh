#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$DIR/viewer.pid"
if [ ! -f "$PID_FILE" ]; then
  echo "未找到 pid 文件"
  exit 0
fi
pid="$(cat "$PID_FILE" 2>/dev/null || true)"
if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
  kill "$pid"
  echo "已停止 viewer, pid=$pid"
else
  echo "viewer 未在运行"
fi
rm -f "$PID_FILE"
