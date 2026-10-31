#!/usr/bin/env python3
"""Destructive restart + start profiling for geo-server (systemd)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

import geo_ask

ROOT = Path("/root/workspace/GUIAgent/GEO/server")
LOG = ROOT / "geo-server.err.log"
OUT = Path("/tmp/geo-restart-profile.jsonl")


def sh(cmd: str, timeout: int = 60) -> str:
    return subprocess.check_output(cmd, shell=True, text=True, timeout=timeout, stderr=subprocess.STDOUT)


def cgroup_mb() -> int:
    raw = sh("systemctl show -p MemoryCurrent --value geo-server").strip()
    try:
        n = int(raw)
    except ValueError:
        return -1
    if n < 0:
        return -1
    return n // 1024 // 1024


def avail_mb() -> int:
    raw = sh("awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo").strip()
    return int(raw)


def chrome_procs() -> int:
    try:
        return int(sh("pgrep -c -f 'chrome-linux64/chrome' || true").strip() or "0")
    except subprocess.CalledProcessError:
        return 0


def snapshot_sites() -> dict:
    ak, sk = geo_ask.load_aksk(ROOT / ".aksk.json")
    return geo_ask.call("https://127.0.0.1", "GET", "/api/v1/sites", ak, sk)


def wait_ready(since_text: str, timeout: int = 180) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = LOG.read_text(encoding="utf-8", errors="replace")
        chunk = t[len(since_text) :]
        if "browser ready, 6 tabs" in chunk or "profile ready total_ms=" in chunk:
            return {"ok": True, "chunk": chunk, "wait_ms": int((timeout - (deadline - time.time())) * 1000)}
        time.sleep(1)
    t = LOG.read_text(encoding="utf-8", errors="replace")
    return {"ok": False, "chunk": t[len(since_text) :][-2000:], "wait_ms": timeout * 1000}


def parse_profile(chunk: str) -> dict:
    out: dict = {"sites": [], "restore_ms": None, "ready_ms": None, "dump_n": None}
    m = re.search(r"profile restore_cookies (\d+)ms", chunk)
    if m:
        out["restore_ms"] = int(m.group(1))
    m = re.search(r"profile ready total_ms=(\d+)", chunk)
    if m:
        out["ready_ms"] = int(m.group(1))
    m = re.search(r"profile dump_cookies n=(\d+)", chunk)
    if m:
        out["dump_n"] = int(m.group(1))
    for m in re.finditer(
        r"profile site (\w+) open_ms=(\d+) refresh_ms=(\d+) freeze_ms=(\d+) status=(\w+)",
        chunk,
    ):
        out["sites"].append(
            {
                "site": m.group(1),
                "open_ms": int(m.group(2)),
                "refresh_ms": int(m.group(3)),
                "freeze_ms": int(m.group(4)),
                "status": m.group(5),
            }
        )
    for m in re.finditer(r"profile site (\w+) open_ms=(\d+) status=error", chunk):
        out["sites"].append({"site": m.group(1), "open_ms": int(m.group(2)), "status": "error"})
    out["dump_fail"] = "dump cookies:" in chunk or "stop cookie flush" in chunk
    return out


def record(kind: str, extra: dict) -> dict:
    rec = {
        "kind": kind,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "cgroup_mb": cgroup_mb(),
        "avail_mb": avail_mb(),
        "chrome_procs": chrome_procs(),
        **extra,
    }
    with OUT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps(rec, ensure_ascii=False), flush=True)
    return rec


def restart_term() -> dict:
    before = LOG.read_text(encoding="utf-8", errors="replace")
    t0 = time.perf_counter()
    sh("systemctl restart geo-server")
    waited = wait_ready(before)
    wall = int((time.perf_counter() - t0) * 1000)
    time.sleep(2)
    sites = snapshot_sites()
    return record(
        "sigterm_restart",
        {
            "wall_ms": wall,
            "ready_ok": waited["ok"],
            "profile": parse_profile(waited.get("chunk") or ""),
            "api_ready": sites.get("ready"),
            "site_status": {s["site_id"]: s["status"] for s in (sites.get("sites") or [])},
        },
    )


def restart_kill() -> dict:
    before = LOG.read_text(encoding="utf-8", errors="replace")
    pid = sh("systemctl show -p MainPID --value geo-server").strip()
    t0 = time.perf_counter()
    if pid and pid != "0":
        os.kill(int(pid), 9)
    time.sleep(1)
    sh("systemctl reset-failed geo-server || true")
    sh("systemctl start geo-server")
    waited = wait_ready(before)
    wall = int((time.perf_counter() - t0) * 1000)
    time.sleep(2)
    sites = snapshot_sites()
    return record(
        "sigkill_restart",
        {
            "wall_ms": wall,
            "ready_ok": waited["ok"],
            "profile": parse_profile(waited.get("chunk") or ""),
            "api_ready": sites.get("ready"),
            "site_status": {s["site_id"]: s["status"] for s in (sites.get("sites") or [])},
        },
    )


def maybe_ask() -> dict | None:
    sites = snapshot_sites()
    logged = [s["site_id"] for s in (sites.get("sites") or []) if s.get("status") == "logged_in"]
    if not logged:
        record("ask_skip", {"reason": "no logged_in", "site_status": {s["site_id"]: s["status"] for s in (sites.get("sites") or [])}})
        return None
    target = logged[0]
    ak, sk = geo_ask.load_aksk(ROOT / ".aksk.json")
    t0 = time.perf_counter()
    submitted = geo_ask.call(
        "https://127.0.0.1",
        "POST",
        "/api/v1/ask",
        ak,
        sk,
        {"sites": [target], "prompt": geo_ask.DEFAULT_PROMPT},
    )
    job_id = submitted.get("job_id")
    job = submitted
    deadline = time.time() + 180
    while job_id and time.time() < deadline:
        time.sleep(3)
        job = geo_ask.call("https://127.0.0.1", "GET", "/api/v1/jobs/" + job_id, ak, sk)
        if job.get("status") in ("succeeded", "failed", "partial"):
            break
    return record(
        "ask_after_restart",
        {
            "site": target,
            "wall_ms": int((time.perf_counter() - t0) * 1000),
            "job_status": job.get("status"),
            "elapsed_ms": job.get("elapsed_ms"),
            "answer": ((job.get("results") or [{}])[0].get("answer") if job.get("results") else None),
            "steps": ((job.get("results") or [{}])[0].get("steps") if job.get("results") else None),
        },
    )


def main() -> int:
    os.chdir(ROOT)
    if OUT.exists():
        OUT.unlink()
    record("baseline", {"site_status": {s["site_id"]: s["status"] for s in (snapshot_sites().get("sites") or [])}})
    restart_term()
    restart_term()
    restart_kill()
    restart_term()
    maybe_ask()
    print("wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
