#!/usr/bin/env python3
"""Drain accepted work, restart deployed systemd services, and check readiness."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import ssl
import subprocess
import sys
import time
import urllib.request
import uuid
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"succeeded", "partial", "failed", "cancelled"}


def systemctl(*args: str) -> str:
    return subprocess.check_output(["systemctl", *args], text=True, timeout=60).strip()


def pending_jobs(data: Path, *, running_only: bool = False) -> set[str]:
    pending = set()
    # Only job records in this directory; never traverse cooldowns or backups.
    for path in (data / "jobs").glob("*.json"):
        job = json.loads(path.read_text(encoding="utf-8"))
        if not job.get("job_id") or job.get("status") not in TERMINAL | {"queued", "running"}:
            raise RuntimeError(f"无法确认任务状态：{path.name}，取消重启")
        if job["status"] not in TERMINAL and (not running_only or job["status"] == "running"):
            pending.add(job["job_id"])
    return pending


@contextmanager
def drain(data: Path, wait_seconds: int, health_timeout: int):
    barrier = data / ".deployment-drain.json"
    owner = uuid.uuid4().hex
    state = {"owner": owner, "allow": [],
             "expires_at": time.time() + wait_seconds + health_timeout + 180}
    # An existing deployment owns this file even if its expiry has passed.
    # Refuse to overwrite it; an operator can inspect it separately.
    try:
        with barrier.open("x", encoding="utf-8") as stream:
            json.dump(state, stream)
    except FileExistsError:
        raise RuntimeError(f"已有部署屏障：{barrier}，取消重启") from None
    try:
        # Once new arrivals are held, let only these existing jobs finish.
        allowed = pending_jobs(data)
        state["allow"] = ["job_" + job_id for job_id in sorted(allowed)]
        temporary = barrier.with_name(".restart-drain-" + owner)
        try:
            temporary.write_text(json.dumps(state), encoding="utf-8")
            temporary.replace(barrier)
        finally:
            temporary.unlink(missing_ok=True)
        deadline = time.monotonic() + wait_seconds
        while True:
            # Also catches a request that began running just before the barrier.
            remaining = (pending_jobs(data) & allowed) | pending_jobs(data, running_only=True)
            if not remaining:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"仍有 {len(remaining)} 个已有任务未完成，服务未重启")
            print(f"等待 {len(remaining)} 个已有任务完成…", flush=True)
            time.sleep(min(5, max(0, deadline - time.monotonic())))
        yield
    finally:
        if barrier.exists():
            current = json.loads(barrier.read_text(encoding="utf-8"))
            if current.get("owner") == owner:
                barrier.unlink()


def probe(root: Path, http_url: str, https_url: str) -> None:
    import geo_ask

    # Explicitly bypass ambient proxy settings for local service checks.
    context = ssl.create_default_context(cafile=str(root / "certs/cert.pem"))
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
    for base in (http_url, https_url):
        with opener.open(base.rstrip("/") + "/admin/login", timeout=3) as response:
            if response.status != 200 or not response.read():
                raise RuntimeError("登录页未就绪")
    ak, sk = geo_ask.load_aksk(root / ".aksk.json")
    path = "/api/v1/sites"
    timestamp, nonce = str(int(time.time())), uuid.uuid4().hex
    request = urllib.request.Request(http_url.rstrip("/") + path, headers={
        "X-Geo-AK": ak, "X-Geo-Timestamp": timestamp, "X-Geo-Nonce": nonce,
        "X-Geo-Signature": geo_ask.sign(sk, "GET", path, timestamp, nonce, b""),
    })
    with opener.open(request, timeout=3) as response:
        payload = json.load(response)
        if response.status != 200 or not payload.get("sites"):
            raise RuntimeError("签名只读接口未就绪")


def wait_ready(root: Path, units: tuple[str, str], args: argparse.Namespace, started_at: float) -> None:
    deadline = time.monotonic() + args.health_timeout
    last_error = "服务尚未就绪"
    while time.monotonic() < deadline:
        try:
            for unit in units:
                if systemctl("is-active", unit) != "active":
                    raise RuntimeError(f"{unit} 未运行")
            probe(root, args.http_url, args.https_url)
            dashboard = root / "data/monitor-dashboard.json"
            snapshot = json.loads(dashboard.read_text(encoding="utf-8"))
            if dashboard.stat().st_mtime < started_at or time.time() - dashboard.stat().st_mtime > 45 or not snapshot.get("service_healthy"):
                raise RuntimeError("监控快照尚未更新为健康状态")
            return
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            # Avoid printing exception text from HTTP handlers or secret files.
            last_error = type(error).__name__
        time.sleep(2)
    raise RuntimeError(f"启动检查超时（{last_error}），请检查 systemctl status 和服务日志")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit", choices=("geo-server", "geo-server-test"), default="geo-server")
    parser.add_argument("--wait-seconds", type=int, default=300, help="已有任务完成等待上限，默认300秒")
    parser.add_argument("--health-timeout", type=int, default=180, help="启动检查等待上限，默认180秒")
    parser.add_argument("--http-url", default="http://127.0.0.1:8080")
    parser.add_argument("--https-url", default="https://127.0.0.1:8443")
    args = parser.parse_args()
    if args.wait_seconds < 0 or args.health_timeout <= 0:
        parser.error("等待秒数必须非负，启动检查秒数必须大于0")
    if os.geteuid() != 0:
        raise RuntimeError("请使用 root 或 sudo 执行 restart.sh")
    monitor = "geo-monitor-test" if args.unit.endswith("-test") else "geo-monitor"
    units = (args.unit, monitor)
    for unit in units:
        directory = systemctl("show", "--property=WorkingDirectory", "--value", unit)
        if not directory or Path(directory).resolve() != ROOT:
            raise RuntimeError(f"{unit} 的工作目录与脚本不匹配，取消重启")
    data = ROOT / "data"
    if not (data / "jobs").is_dir():
        raise RuntimeError("任务目录不存在，取消重启")
    with (data / ".restart.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("已有 restart 正在执行") from None
        with drain(data, args.wait_seconds, args.health_timeout):
            print("已有任务已完成，重启主服务与监控（使用已部署版本）…", flush=True)
            started_at = time.time()
            systemctl("restart", *units)
            wait_ready(ROOT, units, args, started_at)
            pid = systemctl("show", "--property=MainPID", "--value", args.unit)
            (ROOT / "geo-server.pid").write_text(pid + "\n", encoding="utf-8")
    print("重启成功：主服务、监控、HTTP、HTTPS、签名只读接口检查通过。", flush=True)
    return 0


def interrupted(signum, frame):
    raise RuntimeError("重启流程收到终止信号，释放本次部署屏障")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    try:
        sys.exit(main())
    except (Exception, KeyboardInterrupt) as error:
        print(f"restart 失败：{error}", file=sys.stderr)
        sys.exit(1)
