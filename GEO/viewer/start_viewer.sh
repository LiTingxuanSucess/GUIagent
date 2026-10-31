#!/usr/bin/env bash
# AutoDL / Linux：启动 GEO 结果展示（默认 127.0.0.1:18765）
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GEO_DIR="$(cd "$DIR/.." && pwd)"
cd "$DIR"

HOST="${VIEWER_HOST:-127.0.0.1}"
PORT="${VIEWER_PORT:-18765}"
PID_FILE="$DIR/viewer.pid"
OUT_LOG="$DIR/viewer.out.log"
ERR_LOG="$DIR/viewer.err.log"

# 优先项目根 .venv，其次 viewer/.venv，再退回 PATH 里的 python
PY=""
if [ -x "$GEO_DIR/.venv/bin/python" ]; then
  PY="$GEO_DIR/.venv/bin/python"
elif [ -x "$DIR/.venv/bin/python" ]; then
  PY="$DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
else
  PY="$(command -v python)"
fi

if [ -f "$PID_FILE" ]; then
  old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "${old_pid:-}" ] && kill -0 "$old_pid" 2>/dev/null; then
    echo "viewer 已在运行, pid=$old_pid  http://${HOST}:${PORT}/"
    exit 0
  fi
fi

nohup "$PY" -m uvicorn app:app --host "$HOST" --port "$PORT" \
  >"$OUT_LOG" 2>"$ERR_LOG" &
pid=$!
echo "$pid" >"$PID_FILE"
sleep 1
if kill -0 "$pid" 2>/dev/null; then
  echo "viewer 已启动, pid=$pid"
  echo "本机: http://${HOST}:${PORT}/"
  if [ -n "${GEO_PUBLIC_URL:-}" ]; then
    echo "公网: ${GEO_PUBLIC_URL}"
  fi
else
  echo "启动失败" >&2
  tail -50 "$ERR_LOG" >&2 || true
  rm -f "$PID_FILE"
  exit 1
fi
