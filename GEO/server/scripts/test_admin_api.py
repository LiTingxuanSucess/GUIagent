#!/usr/bin/env python3
"""Non-destructive admin/account-pool API regression test."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import time
import urllib.error
import urllib.request


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18080")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", required=True)
    ap.add_argument("--test-phone", default="")
    ap.add_argument("--check-sites", action="store_true")
    ap.add_argument("--send-code", choices=("deepseek", "doubao", "yiyan", "yuanbao", "qianwen", "kimi"))
    ap.add_argument("--check-isolation", choices=("deepseek", "doubao", "yiyan", "yuanbao", "qianwen", "kimi"))
    args = ap.parse_args()
    if not args.test_phone:
        args.test_phone = f"139{time.time_ns() % 100_000_000:08d}"

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def call(method: str, path: str, body: dict | None = None, auth: bool = True):
        raw = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            args.base.rstrip("/") + path,
            data=raw,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        client = opener if auth else urllib.request.build_opener()
        try:
            with client.open(req, timeout=60) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8") or "{}")
            return exc.code, payload

    checks: list[tuple[str, bool, object]] = []

    def expect(name: str, actual, want) -> None:
        shown = sorted(actual) if isinstance(actual, set) else actual
        checks.append((name, actual == want, shown))

    code, _ = call("GET", "/api/admin/accounts", auth=False)
    expect("unauthorized accounts", code, 401)
    code, _ = call("POST", "/api/admin/login", {"user": args.user, "password": "definitely-wrong"})
    expect("wrong password", code, 401)
    code, _ = call("POST", "/api/admin/login", {"user": args.user, "password": args.password})
    expect("admin login", code, 200)

    code, before = call("GET", "/api/admin/accounts")
    expect("list accounts", code, 200)
    before_ids = {a["id"] for a in before.get("accounts", [])}

    code, _ = call("POST", "/api/admin/accounts", {"phones": ["123"]})
    expect("reject invalid phone", code, 400)
    code, created = call("POST", "/api/admin/accounts", {"phones": [args.test_phone]})
    expect("create account", code, 201)
    account_id = ((created.get("accounts") or [{}])[0]).get("id", "")

    try:
        if not account_id:
            raise RuntimeError("test account was not created")
        code, _ = call("POST", "/api/admin/accounts", {"phones": [args.test_phone]})
        expect("reject duplicate phone", code, 400)
        code, disabled = call("PATCH", f"/api/admin/accounts/{account_id}", {"enabled": False})
        expect("disable account", (code, disabled.get("enabled")), (200, False))
        code, enabled = call("PATCH", f"/api/admin/accounts/{account_id}", {"enabled": True})
        expect("enable account", (code, enabled.get("enabled")), (200, True))
        if args.check_isolation:
            code, isolated = call(
                "POST",
                f"/api/admin/accounts/{account_id}/sites/{args.check_isolation}/check",
                {},
            )
            expect("check isolated profile", code, 200)
            expect("isolated profile has no inherited login", isolated.get("status") == "logged_in", False)
        code, _ = call(
            "POST",
            "/api/admin/accounts/batch-check",
            {"account_ids": [], "site_id": "deepseek"},
        )
        expect("reject empty batch", code, 400)
        # Default accounts are deletable. Keep this regression non-destructive
        # for real pools by testing an account ID that cannot be generated.
        code, _ = call("DELETE", "/api/admin/accounts/_missing_test_account")
        expect("reject missing account", code, 400)
    finally:
        if account_id:
            code, _ = call("DELETE", f"/api/admin/accounts/{account_id}")
            expect("delete test account", code, 200)

    code, after = call("GET", "/api/admin/accounts")
    expect("list accounts after cleanup", code, 200)
    after_ids = {a["id"] for a in after.get("accounts", [])}
    expect("pool restored after cleanup", after_ids, before_ids)

    code, _ = call("POST", "/api/sms/inbound?token=wrong", {"type": "receive"})
    expect("reject bad sms token", code, 401)

    site_states = {}
    if args.check_sites:
        for site_id in ("deepseek", "doubao", "yiyan", "yuanbao", "qianwen", "kimi"):
            code, state = call(
                "POST",
                f"/api/admin/accounts/default/sites/{site_id}/check",
                {},
            )
            expect(f"check {site_id}", code, 200)
            site_states[site_id] = {
                "status": state.get("status"),
                "message": state.get("message"),
                "evidence": state.get("evidence"),
            }
    login_state = {}
    if args.send_code:
        code, state = call(
            "POST",
            f"/api/admin/accounts/default/sites/{args.send_code}/send-code",
            {},
        )
        expect(f"send code {args.send_code}", code, 200)
        login_state = {
            "site_id": args.send_code,
            "status": state.get("status"),
            "message": state.get("message"),
        }

    failed = [row for row in checks if not row[1]]
    print(json.dumps(
        {
            "ok": not failed,
            "checks": [{"name": n, "ok": ok, "actual": actual} for n, ok, actual in checks],
            "site_states": site_states,
            "login_state": login_state,
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
