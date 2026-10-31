#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
export PATH="/usr/local/go/bin:${PATH}"
export GEO_SERVER_ROOT="$DIR"
if [ -f "$DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$DIR/.env"
  set +a
fi
BIN="$DIR/bin/geo-server"
mkdir -p "$DIR/bin"
/usr/local/go/bin/go build -o "$BIN" ./cmd/geo-server

UNIT_SRC="$DIR/geo-server.service"
UNIT_DST="/etc/systemd/system/geo-server.service"
if [ ! -f "$UNIT_DST" ] || ! cmp -s "$UNIT_SRC" "$UNIT_DST"; then
  cp "$UNIT_SRC" "$UNIT_DST"
  systemctl daemon-reload
fi
systemctl enable geo-server.service >/dev/null

# leftover nohup/setsid process is not in the systemd cgroup
if systemctl is-active --quiet geo-server; then
  systemctl restart geo-server
else
  PID_FILE="$DIR/geo-server.pid"
  if [ -f "$PID_FILE" ]; then
    old="$(cat "$PID_FILE" || true)"
    if [ -n "${old:-}" ] && kill -0 "$old" 2>/dev/null; then
      if ! systemctl show -p MainPID --value geo-server 2>/dev/null | grep -qx "$old"; then
        kill -TERM "$old" 2>/dev/null || true
        for _ in $(seq 1 15); do
          kill -0 "$old" 2>/dev/null || break
          sleep 1
        done
        kill -KILL "$old" 2>/dev/null || true
      fi
    fi
    rm -f "$PID_FILE"
  fi
  systemctl start geo-server
fi

main_pid="$(systemctl show -p MainPID --value geo-server)"
echo "$main_pid" >"$DIR/geo-server.pid"
public_ip="${GEO_PUBLIC_IP:-127.0.0.1}"
echo "started systemd unit=geo-server pid=${main_pid}  https://${public_ip}:8443/admin"
