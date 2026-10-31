#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if systemctl list-unit-files geo-server.service >/dev/null 2>&1; then
  systemctl stop geo-server.service
  rm -f "$DIR/geo-server.pid"
  echo stopped
  exit 0
fi
if [ ! -f "$DIR/geo-server.pid" ]; then
  echo not running
  exit 0
fi
old="$(cat "$DIR/geo-server.pid" || true)"
if [ -z "${old:-}" ] || ! kill -0 "$old" 2>/dev/null; then
  rm -f "$DIR/geo-server.pid"
  echo not running
  exit 0
fi
kill -TERM "$old" 2>/dev/null || true
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if ! kill -0 "$old" 2>/dev/null; then
    rm -f "$DIR/geo-server.pid"
    echo stopped
    exit 0
  fi
  sleep 1
done
kill -KILL "$old" 2>/dev/null || true
rm -f "$DIR/geo-server.pid"
echo stopped-forced
