#!/usr/bin/env python3
"""Run the admin Cursor CLI page assist on each platform and check the verdict.

For every site one account page is held through the admin cursor-assist API,
the CLI runs, and the page is then read independently with cn_ai.recovery_cdp.
A site passes when the CLI verdict matches the page:
  recovery-candidate        -> no challenge and not logged out
  stopped: sms_required     -> page is logged out (correct hand-off)
  stopped: platform_quota   -> page shows a mute/quota notice
Usage: cli_assist_e2e.py [--sites deepseek,doubao] [--rounds 3]
"""
import argparse
import http.cookiejar
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
GEO = SERVER.parent
API = "http://127.0.0.1:8080"
SITES = ["deepseek", "doubao", "kimi", "qianwen", "yiyan", "yuanbao"]
HOSTS = {"deepseek": "deepseek.com", "doubao": "doubao.com", "kimi": "kimi.com",
         "qianwen": "qianwen.com", "yiyan": "baidu.com", "yuanbao": "yuanbao.tencent.com"}


def env():
    values = {}
    for name in (".env", "optimization.env"):
        path = SERVER / name
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                found = re.match(r"\s*([A-Z0-9_]+)\s*=\s*(.*)", line)
                if found:
                    values[found[1]] = found[2].strip().strip("\"'")
    return values


def opener():
    values = env()
    jar = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    body = json.dumps({"user": values.get("GEO_ADMIN_USER", ""), "password": values.get("GEO_ADMIN_PASSWORD", "")}).encode()
    jar.open(urllib.request.Request(API + "/api/admin/login", body, {"Content-Type": "application/json"}), timeout=10)
    return jar


def call(jar, method, path):
    request = urllib.request.Request(API + path, b"{}" if method == "POST" else None, {"Content-Type": "application/json"}, method=method)
    try:
        with jar.open(request, timeout=300 if method == "POST" else 30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")
    except OSError as error:
        return 0, {"detail": f"{type(error).__name__}: {error}"}


def candidates(jar, site):
    _, data = call(jar, "GET", "/api/admin/accounts")
    rank = {"need_captcha": 0, "logged_in": 1, "unknown": 2}
    found = [(rank[a["sites"][site]["status"]], a["id"]) for a in data["accounts"]
             if a.get("enabled", True) and (a.get("sites") or {}).get(site, {}).get("status") in rank]
    return [account for _, account in sorted(found)]


def page_target(account, site):
    port = (GEO / ".chrome-cn-ai-profiles" / account / "DevToolsActivePort").read_text().split()[0]
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
        pages = [t for t in json.load(response) if t.get("type") == "page" and HOSTS[site] in t.get("url", "")]
    return f"http://127.0.0.1:{port}", pages[0]["id"] if pages else ""


def state(cdp, target, site):
    raw = subprocess.run([sys.executable, "-m", "cn_ai.recovery_cdp", "--cdp", cdp, "--target", target, "--site", site, "state"],
                         cwd=GEO, capture_output=True, text=True, timeout=60).stdout
    try:
        return json.loads(raw)
    except ValueError:
        return {"ok": False, "error": "tool_failed"}


def verdict(call_id):
    log = SERVER / "data" / "cursor-logs" / f"{call_id}.log"
    result, tools = "", 0
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "result":
            result = event.get("result", "")
        if event.get("type") == "tool_call" and event.get("subtype") == "started":
            tools += 1
    marker = re.findall(r"recovery-candidate|recovery-stopped:\s*[a-z_]+", result)
    return (marker[-1] if marker else "none"), tools


def judge(marker, page):
    if not page.get("ok"):
        return False, "page unreadable: " + str(page.get("error"))
    text = page.get("text", "")
    if marker == "recovery-candidate":
        ok = not page.get("challenge_visible") and not page.get("logged_out")
        return ok, "page clear" if ok else "CLI said candidate but page still blocked"
    if marker.endswith("sms_required"):
        # Wenxin shows its login prompt as plain text, which the probe's
        # button-based logged_out check does not count.
        return bool(page.get("logged_out") or page.get("waiting_sms") or re.search(r"请登录|登录后", text)), "login hand-off"
    if marker.endswith("platform_quota"):
        return bool(re.search(r"禁言|额度|用量", text)), "quota/mute notice"
    return False, "verdict " + marker


def run_site(jar, site, account=""):
    for account in [account] if account else candidates(jar, site):
        status, body = call(jar, "POST", f"/api/admin/accounts/{account}/sites/{site}/cursor-assist")
        if status != 202:
            print(f"  {site} {account}: start refused {status} {body.get('detail')}", flush=True)
            continue
        call_id, started = body["call_id"], time.time()
        print(f"  {site} {account}: call {call_id}", flush=True)
        while True:
            time.sleep(5)
            _, current = call(jar, "GET", f"/api/admin/accounts/{account}/sites/{site}/cursor-assist?call_id={call_id}")
            if current.get("status") not in ("running", None) or current.get("phase") == "candidate":
                break
        elapsed = time.time() - started
        marker, tools = verdict(call_id)
        try:
            cdp, target = page_target(account, site)
            page = state(cdp, target, site) if target else {"ok": False, "error": "target_missing"}
        except (OSError, subprocess.TimeoutExpired) as error:
            page = {"ok": False, "error": f"{type(error).__name__}: {error}"}
        ok, why = judge(marker, page)
        print(f"  {site} {account}: {'PASS' if ok else 'FAIL'} {marker} tools={tools} {elapsed:.0f}s "
              f"status={current.get('status')}/{current.get('error_code')} challenge={page.get('challenge_visible')} "
              f"logged_out={page.get('logged_out')} ({why})", flush=True)
        return ok, {"site": site, "account": account, "call_id": call_id, "marker": marker, "tools": tools,
                    "seconds": round(elapsed), "pass": ok, "why": why}
    print(f"  {site}: no account could be held", flush=True)
    return False, {"site": site, "pass": False, "why": "no account could be held"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sites", default=",".join(SITES))
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--account", default="", help="hold this account instead of picking one")
    args = parser.parse_args()
    pending = [s for s in args.sites.split(",") if s]
    results = []
    for round_no in range(1, args.rounds + 1):
        print(f"round {round_no}: {', '.join(pending)}", flush=True)
        jar = opener()
        for site in list(pending):
            ok, record = run_site(jar, site, args.account)
            record["round"] = round_no
            results.append(record)
            if ok:
                pending.remove(site)
        if not pending:
            break
    out = SERVER / "data" / f"cli-assist-e2e-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print("passed all" if not pending else "still failing: " + ", ".join(pending), "->", out, flush=True)
    return 0 if not pending else 1


if __name__ == "__main__":
    sys.exit(main())
