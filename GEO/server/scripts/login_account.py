#!/usr/bin/env python3
"""Drive one account through SMS login, one platform at a time."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import time
import urllib.error
import urllib.request


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", required=True)
    ap.add_argument("--account", required=True)
    ap.add_argument("--site", required=True)
    ap.add_argument("--sms-timeout", type=int, default=90)
    ap.add_argument("--send-only", action="store_true")
    ap.add_argument("--code", default="")
    args = ap.parse_args()

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def call(method: str, path: str, body: dict | None = None):
        raw = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            args.base.rstrip("/") + path,
            data=raw,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with opener.open(req, timeout=60) as resp:
                content = resp.read()
                return resp.status, json.loads(content.decode("utf-8")) if content else {}
        except urllib.error.HTTPError as exc:
            content = exc.read()
            return exc.code, json.loads(content.decode("utf-8")) if content else {}

    status, login = call("POST", "/api/admin/login", {"user": args.user, "password": args.password})
    if status != 200:
        raise SystemExit(json.dumps({"ok": False, "step": "admin_login", "response": login}, ensure_ascii=False))

    status, pool = call("GET", "/api/admin/accounts")
    account = next((a for a in pool.get("accounts", []) if a.get("id") == args.account), None)
    if status != 200 or not account:
        raise SystemExit(json.dumps({"ok": False, "step": "account", "account": args.account}, ensure_ascii=False))
    phone = "".join(c for c in str(account.get("phone", "")) if c.isdigit())[-11:]
    path = f"/api/admin/accounts/{args.account}/sites/{args.site}"

    status, state = call("POST", path + "/check", {})
    if status != 200:
        raise SystemExit(json.dumps({"ok": False, "step": "check", "response": state}, ensure_ascii=False))
    if state.get("status") == "logged_in":
        print(json.dumps({"ok": True, "site": args.site, "status": "logged_in", "skipped_send": True}, ensure_ascii=False))
        return 0

    if args.code:
        status, submitted = call("POST", path + "/submit-code", {"code": args.code})
        if status != 200:
            raise SystemExit(json.dumps({"ok": False, "step": "submit_code", "response": submitted}, ensure_ascii=False))
        status, checked = call("POST", path + "/check", {})
        ok = status == 200 and checked.get("status") == "logged_in"
        print(json.dumps({
            "ok": ok,
            "site": args.site,
            "status": checked.get("status"),
            "message": checked.get("message"),
        }, ensure_ascii=False))
        return 0 if ok else 3

    _, inbox = call("GET", "/api/admin/sms")
    seen = {
        (m.get("requestid"), m.get("received_at"), m.get("code"))
        for m in inbox.get("messages", [])
    }
    status, sent = call("POST", path + "/send-code", {})
    if status != 200:
        raise SystemExit(json.dumps({"ok": False, "step": "send_code", "response": sent}, ensure_ascii=False))
    if args.send_only:
        ok = sent.get("status") in {"waiting_sms", "need_captcha", "login_form"}
        print(json.dumps({
            "ok": ok,
            "site": args.site,
            "status": sent.get("status"),
            "message": sent.get("message"),
        }, ensure_ascii=False))
        return 0 if ok else 2

    deadline = time.monotonic() + args.sms_timeout
    message = None
    while time.monotonic() < deadline:
        _, inbox = call("GET", "/api/admin/sms")
        for item in inbox.get("messages", []):
            receiver = "".join(c for c in str(item.get("receiver", "")) if c.isdigit())[-11:]
            signature = (item.get("requestid"), item.get("received_at"), item.get("code"))
            if receiver == phone and item.get("code") and signature not in seen:
                message = item
                break
        if message:
            break
        time.sleep(2)

    if not message:
        print(json.dumps({
            "ok": False,
            "site": args.site,
            "step": "wait_sms",
            "status": sent.get("status"),
            "message": sent.get("message"),
        }, ensure_ascii=False))
        return 2

    status, submitted = call("POST", path + "/submit-code", {"code": message["code"]})
    if status != 200:
        raise SystemExit(json.dumps({"ok": False, "step": "submit_code", "response": submitted}, ensure_ascii=False))
    status, checked = call("POST", path + "/check", {})
    ok = status == 200 and checked.get("status") == "logged_in"
    print(json.dumps({
        "ok": ok,
        "site": args.site,
        "status": checked.get("status"),
        "message": checked.get("message"),
        "sms_received_at": message.get("received_at"),
    }, ensure_ascii=False))
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
