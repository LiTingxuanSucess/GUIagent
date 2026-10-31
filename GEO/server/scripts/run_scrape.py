#!/usr/bin/env python3
"""Thin CLI around the proven cn_ai.scraper.scrape_one."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

GEO_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GEO_DIR))

def configure_loopback_proxy_bypass() -> None:
    """Keep local CDP off the proxy without bypassing cloud API routing."""
    domains = []
    for name in ("NO_PROXY", "no_proxy"):
        for domain in os.getenv(name, "").split(","):
            domain = domain.strip()
            if domain and domain not in domains:
                domains.append(domain)
    for domain in ("localhost", "127.0.0.1", "::1"):
        if domain not in domains:
            domains.append(domain)
    value = ",".join(domains)
    os.environ["NO_PROXY"] = value
    os.environ["no_proxy"] = value


configure_loopback_proxy_bypass()

from cn_ai.scraper import scrape_one  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from cn_ai.budget import StageDeadline  # noqa: E402


def reject_local_kimi_quota(pw, cdp_url: str) -> None:
    """Stop before a sandbox when the local Kimi page already says the quota is gone."""
    if not cdp_url or cdp_url.startswith("http://127.0.0.1:9"):
        return
    from cn_ai.site_flows import PlatformFlowError, detect_platform_failure

    try:
        browser = pw.chromium.connect_over_cdp(cdp_url, timeout=8000)
    except Exception:
        return
    try:
        page = None
        for context in browser.contexts:
            for candidate in context.pages:
                url = candidate.url or ""
                if "kimi.com" in url or "moonshot.cn" in url:
                    page = candidate
                    break
            if page is not None:
                break
        if page is None:
            return
        # The quota line appears with the sidebar. A freshly opened tab can
        # still be blank for a few seconds; once the page has real text and
        # no quota line, the account can be tried in the sandbox.
        deadline = time.time() + 12
        while time.time() < deadline:
            detect_platform_failure(page, "kimi")
            length = page.evaluate(
                "() => ((document.body && document.body.innerText) || '').trim().length"
            )
            if int(length or 0) > 80:
                return
            time.sleep(0.4)
    except PlatformFlowError:
        raise
    except Exception:
        return


def _yuanbao_profile_dir(phone: str) -> Path | None:
    """Enabled callback account only. Disabled numbers stay unused."""
    digits = "".join(ch for ch in (phone or os.getenv("GEO_YUANBAO_PHONE") or "") if ch.isdigit())
    if len(digits) != 11:
        return None
    accounts_path = Path(os.getenv("GEO_DATA_DIR") or GEO_DIR / "server" / "data") / "accounts.json"
    try:
        accounts = json.loads(accounts_path.read_text(encoding="utf-8")).get("accounts") or []
    except (OSError, json.JSONDecodeError):
        return None
    for account in accounts:
        if not account.get("enabled"):
            continue
        if "".join(ch for ch in str(account.get("phone") or "") if ch.isdigit()) != digits:
            continue
        account_id = str(account.get("id") or "")
        if not account_id:
            continue
        if account_id == "default":
            return _profile_root() / ".chrome-cn-ai-profile"
        return _profile_root() / ".chrome-cn-ai-profiles" / account_id
    return None


def _profile_root() -> Path:
    return Path(os.getenv("GEO_AI_PROFILE_ROOT") or GEO_DIR)


def _profile_for_site(phone: str, site: str):
    """Use this phone's cookies, or another enabled account that is logged in."""
    from cn_ai.volcengine_doubao import profile_site_cookies, profile_site_storage

    accounts_path = Path(os.getenv("GEO_DATA_DIR") or GEO_DIR / "server" / "data") / "accounts.json"
    try:
        accounts = json.loads(accounts_path.read_text(encoding="utf-8")).get("accounts") or []
    except (OSError, json.JSONDecodeError):
        return None, []
    enabled = [a for a in accounts if a.get("enabled")]
    # One global phone is not the logged-in identity for all six platforms.
    # Prefer a confirmed login before a phone with leftover, rejected cookies.
    enabled.sort(key=lambda a: (
        ((a.get("sites") or {}).get(site) or {}).get("status") != "logged_in",
        str(a.get("phone") or "") != phone,
    ))
    for account in enabled:
        account_id = str(account.get("id") or "")
        if not account_id:
            continue
        candidate = (_profile_root() / ".chrome-cn-ai-profile") if account_id == "default" else _profile_root() / ".chrome-cn-ai-profiles" / account_id
        found = profile_site_cookies(candidate, site)
        storage = profile_site_storage(candidate, site)
        if site == "deepseek":
            from cn_ai.deepseek_session import rejected
            # ds_session_id and cached account statuses do not prove that
            # this profile has the frontend's actual authentication token.
            if _deepseek_token(storage) and not rejected(_deepseek_token(storage)):
                return candidate, found
            continue
        if site == "kimi" and not _kimi_login_material(storage):
            # Theme, sidebar and analytics cookies exist for guests as well.
            # They must not displace a profile with actual browser auth tokens.
            continue
        if found or storage:
            return candidate, found
    return None, []


def _kimi_login_material(storage: dict) -> bool:
    for items in storage.values():
        if not isinstance(items, dict):
            continue
        for key in ("access_token", "refresh_token"):
            raw = items.get(key)
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                value = json.loads(raw)
            except ValueError:
                value = raw
            if isinstance(value, dict):
                value = value.get("value")
            if isinstance(value, str) and value.strip().lower() not in ("", "null", "undefined", "false"):
                return True
    return False


def _deepseek_token(storage: dict) -> str:
    raw = (storage.get("https://chat.deepseek.com") or {}).get("userToken", "")
    try:
        value = json.loads(raw).get("value")
    except (TypeError, ValueError, AttributeError):
        return ""
    return raw if isinstance(value, str) and value.strip() else ""


def _seed_storage(site: str, storage: dict) -> dict:
    if site != "deepseek":
        return storage
    return {origin: {key: value for key, value in items.items()
                     if not key.startswith(("smid", ".thumbcache", "HWWAF"))}
            for origin, items in storage.items()}


def _seed_cookies(site: str, cookies: list) -> list:
    """Drop device-bound WAF cookies. They block the cloud browser's own IP."""
    if site != "deepseek":
        return cookies
    return [
        cookie
        for cookie in cookies
        if not str(cookie.get("name") or "").startswith("HWWAF")
        and not str(cookie.get("name") or "").startswith("smid")
        and not str(cookie.get("name") or "").startswith(".thumbcache")
    ]


def _install_site_storage(context, storage: dict) -> None:
    payload = json.dumps(storage, ensure_ascii=False)
    context.add_init_script(
        "(() => {"
        f"const buckets = {payload};"
        "const items = buckets[location.origin];"
        "if (!items) return;"
        "const hasToken = raw => { try { const v = JSON.parse(raw); return typeof v?.value === 'string' && !!v.value.trim(); } catch (_) { return false; } };"
        "for (const [key, value] of Object.entries(items)) {"
        "try { const existing = localStorage.getItem(key);"
        "const emptyAuth = location.origin === 'https://chat.deepseek.com' && key === 'userToken' && hasToken(value) && !hasToken(existing);"
        "if (existing === null || emptyAuth) localStorage.setItem(key, value); } catch (e) {}"
        "}"
        "})();"
    )


def _apply_saved_login(context, profile, site: str, home: str) -> bool:
    """Cookies and localStorage only apply on the next real navigation."""
    from cn_ai.volcengine_doubao import profile_site_cookies, profile_site_storage

    cookies = _seed_cookies(site, profile_site_cookies(profile, site) if profile else [])
    storage = _seed_storage(site, profile_site_storage(profile, site) if profile else {})
    if cookies:
        context.add_cookies(cookies)
    if storage:
        _install_site_storage(context, storage)
    if not cookies and not storage:
        return False
    page = context.pages[0] if context.pages else None
    if page is None:
        return True
    # Repair a nonempty but expired cloud token once. The navigation script
    # above only repairs missing/null tokens, so it does not overwrite a
    # token subsequently refreshed by the website on every navigation.
    token = _deepseek_token(storage) if site == "deepseek" else ""
    repaired = False
    if token:
        try:
            repaired = bool(page.evaluate("""token => {
              if (location.origin !== 'https://chat.deepseek.com') return false;
              if (localStorage.getItem('userToken') === token) return false;
              localStorage.setItem('userToken', token);
              return true;
            }""", token))
        except Exception:
            pass
    from cn_ai.diagnostics import current
    diagnostic = current()
    if diagnostic:
        diagnostic.phase("login_seed", login_cookie_count=len(cookies),
                         login_storage_key_count=sum(len(items) for items in storage.values()),
                         login_has_deepseek_token=bool(token), login_token_repaired=repaired)
    try:
        page.goto("about:blank", timeout=10000)
    except Exception:
        pass
    page.goto(home, wait_until="domcontentloaded", timeout=30000)
    return True


def _ensure_deepseek_login(browser) -> None:
    from cn_ai.site_flows import PlatformFlowError, detect_logged_out, detect_platform_failure
    page = next((p for c in browser.contexts for p in c.pages if "chat.deepseek.com" in (p.url or "")), None)
    if page is None:
        raise PlatformFlowError("platform_auth_unavailable", "DeepSeek 缺少可用登录页面", True)
    deadline = time.monotonic() + 8
    while True:
        detect_logged_out(page, "deepseek")
        detect_platform_failure(page, "deepseek")
        editor = page.locator("textarea").first
        if editor.count() and editor.is_visible() and editor.is_enabled():
            return
        if time.monotonic() >= deadline:
            raise PlatformFlowError("platform_busy", "DeepSeek 登录后输入框尚未就绪", True)
        page.wait_for_timeout(250)


def browser_route(site: str, backend: str) -> str:
    """AgentBay is the webpage path for every site.

    ``volcengine`` keeps the sandbox backend. ``local`` stays on the
    persistent Chrome. AgentBay does not wait for that Chrome to fail.
    """
    if backend == "volcengine":
        return "volcengine"
    if backend == "agentbay":
        return "agentbay"
    return "local"


def run_doubao(local_call, agentbay_call, volcengine_call, backend: str):
    """Run Doubao on the selected browser. Volcengine does not touch local Chrome."""
    if backend == "volcengine":
        result = volcengine_call()
        if isinstance(result, dict):
            result["browser_backend"] = "volcengine"
        return result
    return run_doubao_with_fallback(local_call, agentbay_call, backend == "agentbay")


def run_doubao_with_fallback(local_call, agentbay_call, fallback_enabled: bool):
    """Try the persistent local browser, then AgentBay only after it fails."""
    local_error = None
    local_result = None
    try:
        local_result = local_call()
    except Exception as exc:
        local_error = exc
    else:
        if isinstance(local_result, dict) and local_result.get("ok"):
            local_result["browser_backend"] = "local"
            return local_result
    if not fallback_enabled:
        if local_error is not None:
            raise local_error
        return local_result

    try:
        cloud_result = agentbay_call()
    except Exception as exc:
        if isinstance(local_result, dict):
            failed = dict(local_result)
            failed["agentbay_error"] = str(exc)
            failed["browser_backend"] = "local"
            return failed
        if local_error is not None:
            raise exc from local_error
        raise
    if isinstance(cloud_result, dict):
        cloud_result["browser_backend"] = "agentbay"
        if local_error is not None:
            cloud_result["local_error"] = str(local_error)
        elif isinstance(local_result, dict):
            cloud_result["local_error"] = str(
                local_result.get("error")
                or local_result.get("error_code")
                or "local browser failed"
            )
    return cloud_result


def _run(args: argparse.Namespace) -> dict:
    action = getattr(args, "recovery_action", "")
    if action == 'manual-share':
        from cn_ai.public_share_recovery import collect
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(args.cdp_url, timeout=20000)
            return collect(browser, args.site, args.shared_url, args.prompt, args.question_id)
    if action.startswith("manual-"):
        from cn_ai.manual_workflow import run
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(args.cdp_url, timeout=20000)
            return run(browser, args.site, args.prompt, args.question_id, action)
    if action:
        from cn_ai import budget
        from cn_ai.web_recovery import RecoveryJournal, prepare, probe
        recovery_file = getattr(args, "recovery_file", "") or os.getenv("GEO_WEB_RECOVERY_FILE", "")
        if not recovery_file:
            from cn_ai.site_flows import PlatformFlowError
            raise PlatformFlowError("recovery_unconfirmed", "恢复调用缺少私有记录路径", False)
        os.environ["GEO_WEB_RECOVERY_FILE"] = recovery_file
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(args.cdp_url, timeout=max(1, budget.timeout_ms(20000)))
            if action == "probe" and os.getenv("GEO_WEB_RECOVERY_PREPARE") == "1":
                journal = prepare(browser, args.site, args.prompt, args.question_id, path=recovery_file, bootstrap=True)
            else:
                journal = RecoveryJournal.from_env(args.site, args.prompt, args.question_id)
            checked = budget.call(lambda: probe(browser, journal), 25, code="recovery_unconfirmed")
            if action == "probe" or not checked["ready"]:
                return checked
            result = scrape_one(browser, args.site, args.prompt, question_id=args.question_id, recovery_action="resume")
            result["recovery_action"] = "resume"
            return result
    from cn_ai.diagnostics import current
    diagnostic = current()
    from cn_ai.agentbay_doubao import (
        AgentBayDoubaoLease,
        YUANBAO_HOME,
        ensure_doubao_login,
        ensure_yuanbao_login,
        is_agentbay_disconnect,
    )
    from cn_ai.volcengine_doubao import (
        VolcengineDoubaoError,
        VolcengineDoubaoLease,
        clear_warm_session,
        cookies_for_site,
        delete_session,
        doubao_browser_backend,
        doubao_warm_ttl_min,
        load_warm_session,
        release_expired_warm_session,
        merge_profile_cookies,
        profile_devtools_cdp,
        profile_site_cookies,
        profile_site_storage,
        save_warm_session,
        seed_origin_storage,
        seed_site_cookies,
    )

    def local_call():
        browser = pw.chromium.connect_over_cdp(args.cdp_url, timeout=20000)
        _prepare_recovery_browser(browser, args)
        if diagnostic:
            diagnostic.bind_browser(browser)
            diagnostic.phase("login_restore", browser_backend="local")
            diagnostic.checkpoint(force=True)
        if args.site == "doubao":
            ensure_doubao_login(browser, phone=args.phone)
        elif args.site == "yuanbao":
            ensure_yuanbao_login(browser, phone=args.phone)
        result = scrape_one(
            browser,
            args.site,
            args.prompt,
            question_id=args.question_id,
        )
        if isinstance(result, dict):
            result["browser_backend"] = "local"
        return result

    def agentbay_call():
        site = args.site
        homes = {
            "deepseek": "https://chat.deepseek.com/",
            "doubao": "https://www.doubao.com/chat/",
            "yiyan": "https://yiyan.baidu.com/",
            "yuanbao": YUANBAO_HOME,
            "qianwen": "https://www.qianwen.com/",
            "kimi": "https://www.kimi.com/",
        }
        if site == "doubao":
            state_path = None
            context_name = ""
        else:
            state_path = Path(os.getenv("GEO_AGENTBAY_STATE_DIR") or GEO_DIR / ".scrape_cache") / f"agentbay_{site}_session.json"
            context_name = (
                os.getenv(f"GEO_AGENTBAY_{site.upper()}_CONTEXT", f"geo-{site}").strip()
                or f"geo-{site}"
            )
        last_error = None
        for attempt in range(2):
            question_started = False
            try:
                with AgentBayDoubaoLease(
                    state_path=state_path,
                    context_name=context_name,
                    home_url=homes.get(site, "https://www.doubao.com/chat/"),
                    purpose=f"geo-{site}",
                    locale="zh-CN",
                    solve_captchas=False,
                ) as lease:
                    if diagnostic:
                        diagnostic.phase("browser_connect")
                    browser = pw.chromium.connect_over_cdp(lease.endpoint, timeout=20000)
                    _prepare_recovery_browser(browser, args)
                    if diagnostic:
                        diagnostic.bind_browser(browser)
                        diagnostic.phase("login_restore", browser_backend="agentbay", browser_reused=lease.reused)
                        diagnostic.checkpoint(force=True)
                    login_start = time.monotonic()
                    context = browser.contexts[0] if browser.contexts else None
                    if site == "deepseek":
                        from cn_ai.deepseek_session import DeepSeekAuthObserver
                        profile = deepseek_profile
                        if context is not None and profile is not None:
                            observer = DeepSeekAuthObserver(profile_site_storage(profile, site), diagnostic)
                            observer.bind(context)
                    else:
                        profile, _cookies = _profile_for_site(args.phone, site)
                    if context is not None and lease.needs_login_seed:
                        seeded = _apply_saved_login(
                            context,
                            profile,
                            site,
                            homes.get(site, "https://www.doubao.com/chat/"),
                        )
                        if seeded:
                            lease.needs_login_seed = False
                    if site == "doubao":
                        if diagnostic:
                            diagnostic.phase("login_doubao")
                        ensure_doubao_login(browser, phone=args.phone)
                        lease.needs_login_seed = False
                    elif site == "yuanbao":
                        if diagnostic:
                            diagnostic.phase("login_yuanbao")
                        ensure_yuanbao_login(browser, phone=args.phone)
                        lease.needs_login_seed = False
                    elif site == "deepseek":
                        if diagnostic:
                            diagnostic.phase("login_deepseek")
                        _ensure_deepseek_login(browser)
                        lease.needs_login_seed = False
                    login_ms = int((time.monotonic() - login_start) * 1000)
                    question_started = True
                    result = scrape_one(
                        browser,
                        site,
                        args.prompt,
                        question_id=args.question_id,
                    )
                    if isinstance(result, dict):
                        if result.get("ok"):
                            lease.needs_login_seed = False
                            lease.keep_session = True
                        elif result.get("error_code") == "platform_auth_unavailable":
                            lease.needs_login_seed = True
                        result["browser_backend"] = "agentbay"
                        result["browser_reused"] = lease.reused
                        result["browser_setup_ms"] = lease.setup_ms
                        result["login_ms"] = login_ms
                        if attempt:
                            result["browser_retries"] = attempt
                    return result
            except Exception as exc:
                last_error = exc
                if attempt == 0 and is_agentbay_disconnect(exc) and _may_reconnect(args, question_started):
                    continue
                raise
        raise last_error

    def yuanbao_agentbay_call():
        state_path = GEO_DIR / ".scrape_cache" / "agentbay_yuanbao_session.json"
        context_name = os.getenv("GEO_AGENTBAY_YUANBAO_CONTEXT", "geo-yuanbao-zh").strip() or "geo-yuanbao-zh"
        last_error = None
        for attempt in range(2):
            question_started = False
            try:
                with AgentBayDoubaoLease(
                    state_path=state_path,
                    context_name=context_name,
                    home_url=YUANBAO_HOME,
                    purpose="geo-yuanbao",
                    locale="zh-CN",
                    solve_captchas=False,
                ) as lease:
                    browser = pw.chromium.connect_over_cdp(lease.endpoint)
                    _prepare_recovery_browser(browser, args)
                    if diagnostic:
                        diagnostic.bind_browser(browser)
                        diagnostic.phase("login_restore", browser_backend="agentbay")
                        diagnostic.checkpoint(force=True)
                    context = browser.contexts[0] if browser.contexts else None
                    profile = _yuanbao_profile_dir(args.phone)
                    cookies = profile_site_cookies(profile, "yuanbao") if profile else []
                    if context is not None and cookies:
                        context.add_cookies(cookies)
                        page = next(
                            (
                                item
                                for item in context.pages
                                if "yuanbao.tencent.com" in (item.url or "")
                            ),
                            context.pages[0] if context.pages else context.new_page(),
                        )
                        page.goto(YUANBAO_HOME, wait_until="domcontentloaded", timeout=30000)
                    ensure_yuanbao_login(browser, phone=args.phone)
                    question_started = True
                    result = scrape_one(
                        browser,
                        args.site,
                        args.prompt,
                        question_id=args.question_id,
                    )
                    if isinstance(result, dict):
                        result["browser_backend"] = "agentbay"
                        if attempt:
                            result["browser_retries"] = attempt
                    return result
            except Exception as exc:
                last_error = exc
                if attempt == 0 and is_agentbay_disconnect(exc) and _may_reconnect(args, question_started):
                    continue
                raise
        raise last_error

    def _doubao_cookie_sources():
        profile = _yuanbao_profile_dir(args.phone)
        sources = []
        live = profile_devtools_cdp(profile) if profile else ""
        if live:
            sources.append(live)
        if args.cdp_url and args.cdp_url not in sources:
            sources.append(args.cdp_url)
        return profile, sources

    def _seed_doubao(browser):
        profile, sources = _doubao_cookie_sources()
        for source in sources:
            try:
                seed_site_cookies(pw, source, browser, "doubao")
                return
            except VolcengineDoubaoError:
                continue
        context = browser.contexts[0] if browser.contexts else None
        cookies = profile_site_cookies(profile, "doubao") if profile else []
        if context is not None and cookies:
            context.add_cookies(cookies)

    def _persist_doubao_login(browser):
        context = browser.contexts[0] if browser.contexts else None
        if context is None:
            return
        try:
            selected = cookies_for_site(context.cookies(), "doubao")
        except Exception:
            return
        if not selected:
            return
        profile, sources = _doubao_cookie_sources()
        for source in sources:
            try:
                local = pw.chromium.connect_over_cdp(source, timeout=8000)
                if local.contexts:
                    local.contexts[0].add_cookies(selected)
                break
            except Exception:
                continue
        if profile is not None:
            try:
                merge_profile_cookies(profile, selected)
            except Exception:
                pass

    def _scrape_doubao(browser):
        _prepare_recovery_browser(browser, args)
        ensure_doubao_login(browser, phone=args.phone)
        result = scrape_one(
            browser,
            args.site,
            args.prompt,
            question_id=args.question_id,
        )
        if isinstance(result, dict) and result.get("error_code") == "platform_auth_unavailable":
            # The seeded cookie was rejected. Log in on this same browser and
            # ask once more instead of opening another sandbox.
            ensure_doubao_login(browser, phone=args.phone)
            result = scrape_one(
                browser,
                args.site,
                args.prompt,
                question_id=args.question_id,
            )
            if isinstance(result, dict):
                result["login_retry"] = 1
        return result

    def volcengine_call():
        if args.site == "kimi":
            # Quota is already on the local logged-in page. Opening a sandbox
            # and waiting for /chat/ only repeats the same refusal.
            reject_local_kimi_quota(pw, args.cdp_url)
        if args.site != "doubao":
            with VolcengineDoubaoLease() as lease:
                browser = pw.chromium.connect_over_cdp(lease.cdp_url)
                _prepare_recovery_browser(browser, args)
                if diagnostic:
                    diagnostic.bind_browser(browser)
                    diagnostic.phase("login_restore", browser_backend="volcengine")
                    diagnostic.checkpoint(force=True)
                seed_site_cookies(pw, args.cdp_url, browser, args.site)
                if args.site == "deepseek":
                    seed_origin_storage(
                        pw, args.cdp_url, browser, "https://chat.deepseek.com/"
                    )
                if args.site == "kimi":
                    # Kimi keeps the login in localStorage access_token, not cookies.
                    seed_origin_storage(pw, args.cdp_url, browser, "https://www.kimi.com/")
                result = scrape_one(
                    browser,
                    args.site,
                    args.prompt,
                    question_id=args.question_id,
                )
                if isinstance(result, dict):
                    result["browser_backend"] = "volcengine"
                return result

        browser = None
        lease = None
        reused = False
        release_expired_warm_session()
        warm = load_warm_session()
        if warm:
            # Six sites open sandboxes together, so the warm CDP socket can
            # miss an 8s connect and used to be thrown away. Retry once
            # before creating a replacement session.
            for _attempt in range(2):
                try:
                    browser = pw.chromium.connect_over_cdp(warm["cdp_url"], timeout=15000)
                    reused = True
                    break
                except Exception:
                    browser = None
                    time.sleep(0.5)
            if browser is None:
                delete_session(str(warm.get("tool_id") or ""), str(warm.get("session_id") or ""))
                clear_warm_session()
        if browser is None:
            lease = VolcengineDoubaoLease(ttl_min=doubao_warm_ttl_min())
            lease.__enter__()
            try:
                browser = pw.chromium.connect_over_cdp(lease.cdp_url, timeout=20000)
            except Exception:
                lease.__exit__(None, None, None)
                raise
        try:
            if diagnostic:
                diagnostic.bind_browser(browser)
                diagnostic.phase("login_restore", browser_backend="volcengine", browser_reused=reused)
                diagnostic.checkpoint(force=True)
            if not reused:
                _seed_doubao(browser)
            result = _scrape_doubao(browser)
            if isinstance(result, dict):
                result["browser_backend"] = "volcengine"
                result["browser_reused"] = reused
            if isinstance(result, dict) and result.get("ok"):
                _persist_doubao_login(browser)
                if lease is not None:
                    save_warm_session(
                        lease.session_id,
                        lease.tool_id,
                        lease.cdp_url,
                        doubao_warm_ttl_min(),
                    )
                    lease.keep = True
            elif reused and isinstance(result, dict) and result.get("error_code") == "platform_auth_unavailable":
                delete_session(str(warm.get("tool_id") or ""), str(warm.get("session_id") or ""))
                clear_warm_session()
            return result
        finally:
            if lease is not None:
                lease.__exit__(None, None, None)

    from contextlib import nullcontext
    route = browser_route(args.site, doubao_browser_backend())
    deepseek_profile = None
    if route == "agentbay" and args.site == "deepseek":
        from cn_ai.site_flows import PlatformFlowError
        deepseek_profile, _ = _profile_for_site(args.phone, args.site)
        saved_path = Path(os.getenv("GEO_AGENTBAY_STATE_DIR") or GEO_DIR / ".scrape_cache") / "agentbay_deepseek_session.json"
        try:
            saved = json.loads(saved_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        if deepseek_profile is None and saved.get("login_seeded") != "true":
            if diagnostic:
                diagnostic.phase("login_precheck", browser_backend="agentbay", login_has_deepseek_token=False)
            raise PlatformFlowError("platform_auth_unavailable", "DeepSeek 缺少有效网页 Token 或已有 Token 已被平台拒绝，请重新登录", True)
    with sync_playwright() as pw, diagnostic.guard() if diagnostic else nullcontext():
        if route == "volcengine":
            return volcengine_call()
        if route == "agentbay":
            return agentbay_call()
        return local_call()


def _prepare_recovery_browser(browser, args):
    """Capture a target before login code can encounter a verification page."""
    from cn_ai.web_recovery import prepare, fail
    journal = prepare(browser, args.site, args.prompt, args.question_id)
    if journal and journal.data["send_state"] != "before_send":
        fail("recovery_unconfirmed", "问题可能已经发送，必须续读原页面，未重新提交")
    if journal and not journal.data.get("target_id"):
        from cn_ai.scraper import find_or_open_page
        from cn_ai import budget
        page = budget.call(lambda: find_or_open_page(browser, args.site), 30, code="session_setup_timeout")
        if page is not None:
            journal.bind(page)
    return journal


def _may_reconnect(args, question_started):
    if not question_started:
        return True
    from cn_ai.web_recovery import RecoveryJournal
    journal = RecoveryJournal.from_env(args.site, args.prompt, args.question_id)
    return bool(journal and journal.data["send_state"] == "before_send")


def wait_for_deployment(question_id: str) -> None:
    """Hold new attempts at the scraper boundary during a service handover.

    Jobs already executing are listed in the drain file and keep running.
    Held jobs never submit a question and can safely return to the queue.
    An expiring file prevents an abandoned deployment from blocking work.
    """
    path = GEO_DIR / "server" / "data" / ".deployment-drain.json"
    while path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if time.time() >= float(state.get("expires_at", 0)):
            return
        if question_id in state.get("allow", []):
            return
        marker = path.parent / (".deployment-held-" + question_id.replace("/", "_") + ".json")
        marker.write_text(json.dumps({"question_id": question_id}), encoding="utf-8")
        time.sleep(0.1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp-url", required=True)
    ap.add_argument("--site", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--question-id", default="")
    ap.add_argument("--phone", default="")
    ap.add_argument("--recovery-action", choices=("probe", "resume", "manual-auto", "manual-pause", "manual-continue", "manual-submitted", "manual-refresh", "manual-restore", "manual-confirm-current", "manual-reconcile", "manual-share"), default="")
    ap.add_argument('--shared-url', default='')
    ap.add_argument("--recovery-file", default="")
    args = ap.parse_args()
    main.invocation = args
    wait_for_deployment(args.question_id)
    if not args.recovery_action:
        from cn_ai.web_recovery import RecoveryJournal
        RecoveryJournal.from_env(args.site, args.prompt, args.question_id, create=True)
    started = time.monotonic()
    from cn_ai.diagnostics import DiagnosticRun
    with DiagnosticRun(args.site, args.question_id, args.prompt) as diagnostic:
        try:
            result = _run(args)
        except BaseException as error:
            from cn_ai.web_recovery import RecoveryJournal
            journal = RecoveryJournal.from_env(args.site, args.prompt, args.question_id)
            if journal:
                journal.failure(error)
            raise
        diagnostic.meta["outcome"] = "succeeded" if result.get("ok") else "failed"
        if not result.get("ok"):
            diagnostic.fail(result.get("error") or "No valid answer")
    result["browser_flow_ms"] = int((time.monotonic() - started) * 1000)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Exception, StageDeadline) as e:
        message = str(e).strip()
        if message in ("", "None", "null"):
            message = f"{type(e).__name__}: 平台提交没有返回原因"
        payload = {"ok": False, "error": message}
        error_code = getattr(e, "error_code", "")
        if error_code:
            payload["error_code"] = error_code
            payload["retryable"] = bool(getattr(e, "retryable", not error_code.startswith("recovery_")))
        retry_after = getattr(e, "retry_after_sec", None)
        if isinstance(retry_after, int) and not isinstance(retry_after, bool) and 0 <= retry_after <= 600:
            payload["retry_after_sec"] = retry_after
        # stdout 是父进程读取结构化结果的通道；stderr 仅保留非结构化诊断。
        invocation = getattr(main, "invocation", None)
        if getattr(invocation, "recovery_action", "") == "probe":
            payload["recovery_action"] = "probe"
            payload["ready"] = False
            payload.setdefault("context_valid", False)
            try:
                from cn_ai.web_recovery import RecoveryJournal
                journal = RecoveryJournal.from_env(invocation.site, invocation.prompt, invocation.question_id)
                if journal and journal.data.get("target_id"):
                    payload["target_id"] = journal.data["target_id"]
            except Exception:
                pass
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        raise SystemExit(1)
