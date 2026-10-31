#!/usr/bin/env bash
# 在 AutoDL 上执行：解压更新包、装依赖、重启 viewer+frpc 并做本机检查
set -euo pipefail

GEO="${AUTODL_GEO_DIR:-/root/autodl-tmp/general-data-platform/projects/GEO}"
TGZ="${1:-}"
if [ -z "$TGZ" ] || [ ! -f "$TGZ" ]; then
  echo "usage: $0 /tmp/geo-update.tgz" >&2
  exit 1
fi

mkdir -p "$GEO"
cd "$GEO"

echo "[apply] stop old services"
./viewer/stop_viewer.sh 2>/dev/null || true
./frp-geo-40216/stop_frpc_40216.sh 2>/dev/null || true
pkill -f "uvicorn app:app --host 127.0.0.1 --port 18765" 2>/dev/null || true
pkill -f "frpc -c .*frpc.40216" 2>/dev/null || true
sleep 1

echo "[apply] extract $TGZ"
tar -xzf "$TGZ" -C "$GEO"
rm -f "$TGZ"

chmod +x viewer/start_viewer.sh viewer/stop_viewer.sh \
  frp-geo-40216/start_frpc_40216.sh frp-geo-40216/stop_frpc_40216.sh 2>/dev/null || true

if [ ! -x frp-geo-40216/frpc ]; then
  if [ -x /usr/local/bin/frpc ]; then
    echo "[apply] use /usr/local/bin/frpc"
    cp -f /usr/local/bin/frpc frp-geo-40216/frpc
    chmod +x frp-geo-40216/frpc
  else
    echo "[apply] download frpc 0.60.0 linux_amd64"
    TMP="$(mktemp -d)"
    URL="https://github.com/fatedier/frp/releases/download/v0.60.0/frp_0.60.0_linux_amd64.tar.gz"
    if ! curl -fsSL --connect-timeout 20 -o "$TMP/frp.tgz" "$URL"; then
      curl -fsSL --connect-timeout 20 -o "$TMP/frp.tgz" "https://ghproxy.com/${URL}"
    fi
    tar -xzf "$TMP/frp.tgz" -C "$TMP"
    cp "$TMP"/frp_0.60.0_linux_amd64/frpc frp-geo-40216/frpc
    chmod +x frp-geo-40216/frpc
    rm -rf "$TMP"
  fi
fi

if [ ! -x .venv/bin/python ]; then
  echo "[apply] create .venv"
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r viewer/requirements.txt

echo "[apply] start viewer + frpc"
./viewer/start_viewer.sh
./frp-geo-40216/start_frpc_40216.sh
sleep 2

echo "[apply] local checks"
code_home="$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 3 http://127.0.0.1:18765/ || echo 000)"
code_api="$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 3 http://127.0.0.1:18765/api/batches || echo 000)"
echo "local / -> $code_home"
echo "local /api/batches -> $code_api"
test "$code_home" = "200"
test "$code_api" = "200"
echo "[apply] OK"
