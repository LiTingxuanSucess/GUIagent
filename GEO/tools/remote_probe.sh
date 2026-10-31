#!/usr/bin/env bash
set -euo pipefail
GEO=/root/autodl-tmp/general-data-platform/projects/GEO
cd "$GEO"
echo "=== deploy_remote.sh ==="
sed -n '1,120p' deploy_remote.sh 2>/dev/null || true
echo "=== frp-geo-40216 ==="
ls -la frp-geo-40216/
echo "=== frpc binary ==="
ls -la frp-geo-40216/frpc 2>&1 || true
ls -la /usr/local/bin/frpc 2>&1 || true
echo "=== pids ==="
echo -n "viewer.pid="; cat viewer/viewer.pid 2>/dev/null || echo missing
echo -n "frpc.pid="; cat frp-geo-40216/frpc-40216.pid 2>/dev/null || echo missing
for f in viewer/viewer.pid frp-geo-40216/frpc-40216.pid; do
  if [ -f "$f" ]; then
    p=$(cat "$f")
    if kill -0 "$p" 2>/dev/null; then echo "alive $f -> $p"; else echo "dead $f -> $p"; fi
  fi
done
echo "=== listen ==="
ss -lntp 2>/dev/null | grep -E '18765|8765' || true
echo "=== curl 18765 ==="
curl -s -o /dev/null -w "%{http_code}\n" --connect-timeout 2 http://127.0.0.1:18765/ || echo fail
echo "=== frpc.40216.json ==="
cat frp-geo-40216/frpc.40216.json
echo
echo "=== viewer log ==="
tail -20 viewer/viewer.out.log 2>/dev/null || true
echo "=== frpc log ==="
tail -20 frp-geo-40216/frpc-40216.out.log 2>/dev/null || true
echo "=== viewer err ==="
tail -20 viewer/viewer.err.log 2>/dev/null || true
