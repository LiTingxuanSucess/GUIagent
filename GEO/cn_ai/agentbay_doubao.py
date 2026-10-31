# -*- coding: utf-8 -*-
"""AgentBay-backed Doubao browser session and automatic SMS login."""
from __future__ import annotations

import fcntl
import http.cookiejar
import json
import hashlib
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set
from urllib.request import HTTPCookieProcessor, Request, build_opener

from playwright.sync_api import Browser, Page

from cn_ai.site_flows import close_site_targets, detect_platform_failure, page_answers, wait_doubao_auth
from cn_ai.diagnostics import current as current_diagnostics

GEO_DIR = Path(__file__).resolve().parent.parent
DEFAULT_STATE_PATH = GEO_DIR / ".scrape_cache" / "agentbay_doubao_session.json"
DEFAULT_CONTEXT_NAME = "geo-doubao-aio"
DEFAULT_IMAGE_ID = "aio-ubuntu-2404"
DOUBAO_HOME = "https://www.doubao.com/chat/"


def _bounded_number(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
        return value if minimum <= value <= maximum else default
    except (ValueError, TypeError):
        return default


class AgentBayDoubaoError(RuntimeError):
    def __init__(
        self,
        message: str,
        error_code: str = "platform_auth_unavailable",
        retryable: bool = True,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


def use_agentbay_for_doubao(site_id: str, mode: Optional[str] = None) -> bool:
    """Doubao tries local Chrome first; AgentBay is the failure fallback.

    ``off``, ``local-only``, ``disabled``, and ``none`` keep the local browser
    as the only backend. ``volcengine`` skips AgentBay. Every other value,
    including the default and ``local``, allows the fallback.
    """
    if site_id != "doubao":
        return False
    from cn_ai.volcengine_doubao import doubao_browser_backend

    return doubao_browser_backend(mode) == "agentbay"


def _state_path() -> Path:
    raw = os.getenv("GEO_AGENTBAY_DOUBAO_STATE", "").strip()
    return Path(raw) if raw else DEFAULT_STATE_PATH


def _read_state(path: Path) -> Dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items() if v}
    except Exception:
        return {}


def _write_state(path: Path, data: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        os.chmod(temp, 0o600)
        json.dump(data, handle, ensure_ascii=False)
    temp.replace(path)


class AgentBayDoubaoLease:
    """Serialize access to one persistent AgentBay browser profile."""

    def __init__(
        self,
        *,
        state_path: Optional[Path] = None,
        context_name: str = "",
        home_url: str = "",
        purpose: str = "",
        locale: str = "",
        solve_captchas: bool = False,
        auto_upload: bool = True,
    ) -> None:
        self.path = state_path or _state_path()
        self._context_name = context_name.strip()
        self._home_url = home_url.strip() or DOUBAO_HOME
        self._purpose = purpose.strip() or "geo-doubao-fallback"
        self._locale = locale.strip()
        self._solve_captchas = bool(solve_captchas)
        self._auto_upload = bool(auto_upload)
        self._start_blank = os.getenv("GEO_AGENTBAY_START_BLANK", "0") == "1"
        self._lock = None
        self._slot = None
        self._startup = None
        self._stop = threading.Event()
        self._keepalive: Optional[threading.Thread] = None
        self.client: Any = None
        self.session: Any = None
        self.endpoint = ""
        self.context_id = ""
        self.context_name = ""
        self.needs_login_seed = True
        self.keep_session = False
        self.reused = False
        self.setup_ms = 0

    def __enter__(self) -> "AgentBayDoubaoLease":
        diagnostic = current_diagnostics()
        if diagnostic:
            diagnostic.phase("cloud_profile_wait", browser_backend="agentbay")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = self.path.with_suffix(".lock").open("a+", encoding="utf-8")
        try:
            deadline = time.monotonic() + _bounded_number("GEO_AGENTBAY_WAIT_SEC", 30, 0, 300)
            while True:
                try:
                    fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AgentBayDoubaoError("AgentBay 档案正在使用", error_code="platform_busy")
                    time.sleep(min(0.2, remaining))
            if diagnostic:
                diagnostic.phase("cloud_slot_wait")
            self._acquire_slot()
            setup_start = time.monotonic()
            self._connect()
            self.setup_ms = int((time.monotonic() - setup_start) * 1000)
            return self
        except Exception as exc:
            if diagnostic:
                diagnostic.fail(exc)
            self.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        diagnostic = current_diagnostics()
        if exc is not None and diagnostic:
            diagnostic.fail(exc)  # Preserve the page before deleting the cloud session.
        if getattr(exc, "error_code", "") == "platform_auth_unavailable":
            self.needs_login_seed = True
        self._stop.set()
        if self._keepalive:
            self._keepalive.join(timeout=2)
        try:
            warm_seconds = _bounded_number("GEO_AGENTBAY_WARM_SEC", 0, 0, 150)
            if self.keep_session and exc is None and self.session is not None and self.endpoint and warm_seconds > 0:
                try:
                    kept = self.session.keep_alive()
                except Exception:
                    kept = None
                if bool(getattr(kept, "success", False)):
                    _write_state(self.path, {
                        "session_id": str(self.session.session_id),
                        "context_id": self.context_id,
                        "context_name": self.context_name,
                        "login_seeded": "true" if not self.needs_login_seed else "false",
                        "warm_until": str(time.time() + warm_seconds),
                    })
                    return
            deleted = self.session is None
            if self.client is not None and self.session is not None:
                try:
                    result = self.client.delete(self.session, sync_context=self._auto_upload)
                    deleted = bool(getattr(result, "success", False))
                except Exception:
                    pass
            if self.context_id:
                state = {
                    "context_id": self.context_id,
                    "context_name": self.context_name,
                    "login_seeded": "true" if not self.needs_login_seed else "false",
                }
                # Keep a failed deletion discoverable on the next lease.
                if not deleted and self.session is not None:
                    state["session_id"] = str(self.session.session_id)
                _write_state(self.path, state)
        finally:
            self._unlock()

    def _acquire_slot(self) -> None:
        slots = int(_bounded_number("GEO_AGENTBAY_CONCURRENCY", 6, 1, 6))
        self._slot = self._acquire_file_slot("agentbay", slots)

    def _acquire_startup_slot(self) -> None:
        # Only cold creation/init needs throttling. Once a browser is ready,
        # all six sites can generate answers at the same time.
        slots = int(_bounded_number("GEO_AGENTBAY_STARTUP_CONCURRENCY", 2, 1, 6))
        self._startup = self._acquire_file_slot("agentbay.startup", slots)

    def _acquire_file_slot(self, prefix: str, slots: int):
        wait = _bounded_number("GEO_AGENTBAY_WAIT_SEC", 30, 0, 300)
        directory = GEO_DIR / ".scrape_cache"
        directory.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + wait
        while True:
            for index in range(slots):
                candidate = (directory / f"{prefix}.slot-{index}").open("a+", encoding="utf-8")
                try:
                    fcntl.flock(candidate.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    candidate.close()
                    continue
                except Exception:
                    candidate.close()
                    raise
                return candidate
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AgentBayDoubaoError(
                    "AgentBay 浏览器并发槽位等待超时",
                    error_code="platform_busy",
                )
            time.sleep(min(0.2, remaining))

    def _unlock(self) -> None:
        for handle in (self._startup, self._slot, self._lock):
            if handle is None:
                continue
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                handle.close()
            except Exception:
                pass
        self._slot = None
        self._startup = None
        self._lock = None

    def _open_context(self, name: str) -> str:
        result = self.client.context.get(name=name, create=True)
        context = getattr(result, "context", None)
        context_id = str(getattr(context, "id", "") or "")
        if not context_id:
            detail = str(getattr(result, "error_message", "") or "")
            raise AgentBayDoubaoError(
                f"AgentBay 浏览器档案创建失败: {detail or 'unknown error'}",
                error_code="site_request_failed",
            )
        self.context_id = context_id
        self.context_name = name
        return context_id

    def _start_browser(self, option: Any) -> str:
        """Start Chrome and return an empty string, or the platform error."""
        from agentbay._common.config import _BROWSER_DATA_PATH
        from agentbay.api.models import InitBrowserRequest

        browser = self.session.browser
        if browser.is_initialized():
            return ""
        browser_option = option._to_map()
        if getattr(self.session, "enableBrowserReplay", None) is not None:
            browser_option["enableRecord"] = self.session.enableBrowserReplay
        request = InitBrowserRequest(
            authorization=f"Bearer {self.session._get_api_key()}",
            session_id=self.session._get_session_id(),
            persistent_path=_BROWSER_DATA_PATH,
            browser_option=browser_option,
        )
        try:
            response = self.session._get_client().init_browser(request)
            body = response.to_map().get("body") or {}
        except Exception as exc:
            return str(exc)[:240]
        data = body.get("Data") or {}
        port = data.get("Port")
        if port is None:
            message = (
                body.get("Message")
                or body.get("Code")
                or "InitBrowser 没有返回调试端口"
            )
            return str(message)[:240]
        browser.endpoint_router_port = port
        browser._initialized = True
        browser._option = option
        return ""

    def _create_session(self, context_id: str) -> None:
        diagnostic = current_diagnostics()
        if diagnostic:
            diagnostic.phase("cloud_create_session")
        from agentbay import BrowserContext, CreateSessionParams

        image_id = os.getenv(
            "GEO_AGENTBAY_DOUBAO_IMAGE", DEFAULT_IMAGE_ID
        ).strip() or DEFAULT_IMAGE_ID
        result = self.client.create(
            CreateSessionParams(
                image_id=image_id,
                idle_release_timeout=int(_bounded_number("GEO_AGENTBAY_IDLE_RELEASE_SEC", 180, 60, 1800)),
                labels={"purpose": self._purpose},
                browser_context=BrowserContext(
                    context_id=context_id,
                    auto_upload=self._auto_upload,
                ),
            )
        )
        self.session = getattr(result, "session", None)
        if self.session is None:
            detail = str(getattr(result, "error_message", "") or "")
            unavailable = (
                "INVALID_ACCOUNT_STATUS" in detail
                or "account status is invalid" in detail.lower()
                or "CEASED" in detail
            )
            raise AgentBayDoubaoError(
                f"AgentBay 会话创建失败: {detail or 'unknown error'}",
                error_code=(
                    "platform_auth_unavailable"
                    if unavailable
                    else "site_request_failed"
                ),
            )
        if diagnostic:
            diagnostic.phase("cloud_session_created", browser_image=image_id,
                             session_fingerprint=hashlib.sha256(str(self.session.session_id).encode()).hexdigest()[:16],
                             context_fingerprint=hashlib.sha256(context_id.encode()).hexdigest()[:16])
        # Persist immediately: if the Go deadline kills this Python process
        # during browser initialization, the next lease can recover/release
        # the session. The server-side idle limit also bounds orphan billing.
        _write_state(self.path, {
            "session_id": str(self.session.session_id),
            "context_id": self.context_id,
            "context_name": self.context_name,
            "login_seeded": "true" if not self.needs_login_seed else "false",
        })

    def _connect(self) -> None:
        diagnostic = current_diagnostics()
        if diagnostic:
            diagnostic.phase("cloud_connect")
        api_key = os.getenv("AGENTBAY_API_KEY", "").strip()
        if not api_key:
            raise AgentBayDoubaoError("AgentBay API Key 未配置")
        try:
            from agentbay import (
                AgentBay,
                BrowserContext,
                BrowserFingerprint,
                BrowserOption,
                CreateSessionParams,
            )
            from agentbay._common.logger import AgentBayLogger
        except ImportError as exc:
            raise AgentBayDoubaoError(
                "AgentBay SDK 未安装",
                error_code="site_request_failed",
            ) from exc
        AgentBayLogger.setup(
            level="WARNING",
            enable_console=False,
            enable_file=False,
        )

        self.client = AgentBay(api_key=api_key)
        state = _read_state(self.path)
        self.needs_login_seed = state.get("login_seeded") != "true"
        session_id = state.get("session_id", "")
        if session_id:
            if diagnostic:
                diagnostic.phase("cloud_get_session")
            try:
                self.session = self.client.get(session_id).session
            except Exception:
                self.session = None

        context_name = state.get("context_name") or self._context_name or os.getenv(
            "GEO_AGENTBAY_DOUBAO_CONTEXT", DEFAULT_CONTEXT_NAME
        ).strip() or DEFAULT_CONTEXT_NAME
        context_id = state.get("context_id", "")
        if context_id:
            self.context_id = context_id
            self.context_name = context_name
        else:
            if diagnostic:
                diagnostic.phase("cloud_open_context")
            context_id = self._open_context(context_name)

        if self.session is not None:
            # get() does not restore the SDK Browser's initialized flag. A
            # cached successful session is already running Chrome: obtain a
            # fresh signed CDP URL without another InitBrowser/navigation.
            try:
                warm_until = float(state.get("warm_until", "0"))
            except ValueError:
                warm_until = 0
            if warm_until > time.time():
                try:
                    kept = self.session.keep_alive()
                    if bool(getattr(kept, "success", False)):
                        self.session.browser._initialized = True
                        self.endpoint = str(self.session.browser.get_endpoint_url() or "")
                        if self.endpoint:
                            self.reused = True
                            self._start_keepalive()
                            return
                except Exception:
                    pass
            deleted = self.client.delete(self.session, sync_context=self._auto_upload)
            if not bool(getattr(deleted, "success", False)):
                raise AgentBayDoubaoError("AgentBay 过期会话释放失败", error_code="site_request_failed")
            self.session = None

        if diagnostic:
            diagnostic.phase("cloud_startup_wait")
        self._acquire_startup_slot()
        if self.session is None:
            self._create_session(context_id)

        if diagnostic:
            diagnostic.phase("cloud_keepalive")
        keep = self.session.keep_alive()
        if not bool(getattr(keep, "success", False)):
            raise AgentBayDoubaoError(
                "AgentBay 会话续期失败",
                error_code="site_request_failed",
            )
        fingerprint = None
        cmd_args = None
        if self._locale:
            fingerprint = BrowserFingerprint(
                devices=["desktop"],
                operating_systems=["windows"],
                locales=[self._locale],
            )
            cmd_args = [f"--lang={self._locale}"]
        option = BrowserOption(
            call_for_user=True,
            use_stealth=True,
            default_navigate_url="about:blank" if self._start_blank else self._home_url,
            fingerprint=fingerprint,
            cmd_args=cmd_args,
            solve_captchas=self._solve_captchas,
        )
        if diagnostic:
            diagnostic.phase("browser_initialize")
        detail = self._start_browser(option)
        if detail and "startChromeByCdp" not in detail:
            # Retry initialization on this session before recreating it. An
            # infrastructure error must never discard a working login profile.
            time.sleep(1)
            detail = self._start_browser(option)
        if detail:
            try:
                deleted = self.client.delete(self.session, sync_context=self._auto_upload)
                if not bool(getattr(deleted, "success", False)):
                    raise AgentBayDoubaoError("AgentBay 旧会话未释放", error_code="site_request_failed")
            except Exception:
                raise AgentBayDoubaoError("AgentBay 会话释放失败", error_code="site_request_failed")
            self.session = None
            try:
                self._create_session(context_id)
                kept = self.session.keep_alive()
                if not bool(getattr(kept, "success", False)):
                    detail = "AgentBay 会话续期失败"
                else:
                    fresh = self._start_browser(option)
                    detail = fresh or ""
            except AgentBayDoubaoError as exc:
                detail = str(exc)
            if detail:
                raise AgentBayDoubaoError(
                    f"AgentBay 浏览器启动失败: {detail}",
                    error_code="site_request_failed",
                )
        if diagnostic:
            diagnostic.phase("browser_endpoint")
        self.endpoint = str(self.session.browser.get_endpoint_url() or "")
        if not self.endpoint:
            raise AgentBayDoubaoError(
                "AgentBay 浏览器连接地址为空",
                error_code="site_request_failed",
            )
        _write_state(
            self.path,
            {
                "session_id": str(self.session.session_id),
                "context_id": self.context_id,
                "context_name": self.context_name,
                "login_seeded": "true" if not self.needs_login_seed else "false",
            },
        )
        # Release just the cold-start slot; retain the profile and active-flow
        # slots through the prompt, so another platform can start immediately.
        if self._startup is not None:
            fcntl.flock(self._startup.fileno(), fcntl.LOCK_UN)
            self._startup.close()
            self._startup = None
        self._start_keepalive()

    def _start_keepalive(self) -> None:
        self._keepalive = threading.Thread(
            target=self._keepalive_loop,
            name="agentbay-doubao-keepalive",
            daemon=True,
        )
        self._keepalive.start()

    def _keepalive_loop(self) -> None:
        while not self._stop.wait(30):
            try:
                result = self.session.keep_alive()
                if not bool(getattr(result, "success", False)):
                    return
            except Exception:
                return


def is_agentbay_disconnect(exc: BaseException) -> bool:
    text = str(exc or "").lower()
    return any(
        marker in text
        for marker in (
            "failed to open a new tab",
            "target page, context or browser has been closed",
            "target closed",
            "browser has been closed",
            "browser closed",
            "websocket is not open",
            "connection closed",
            "session not found",
        )
    )


def _visible_exact(page: Page, text: str):
    locator = page.get_by_text(text, exact=True)
    for i in range(locator.count()):
        item = locator.nth(i)
        try:
            if item.is_visible():
                return item
        except Exception:
            continue
    return None


def _visible_role_button(page: Page, name: str):
    locator = page.get_by_role("button", name=name)
    for i in range(locator.count()):
        item = locator.nth(i)
        try:
            if item.is_visible():
                return item
        except Exception:
            continue
    return None


def doubao_text_logged_in(body: str) -> bool:
    """The logged-out homepage also contains 发消息. Only the account chip counts."""
    return bool(re.search(r"用户[A-Za-z0-9]{4,}", body or ""))


def _doubao_logged_in(page: Page) -> bool:
    # body.inner_text waits until the node stops changing. The signed-in
    # sidebar keeps updating, so that call times out and a live session
    # looks logged out. Read innerText directly and wait out the login shell.
    return wait_doubao_auth(page, 0) == "logged_in"


class _SMSInboxClient:
    def __init__(self) -> None:
        self.base = os.getenv(
            "GEO_SMS_ADMIN_URL", "http://127.0.0.1:8080"
        ).rstrip("/")
        user = os.getenv("GEO_ADMIN_USER", "").strip()
        password = os.getenv("GEO_ADMIN_PASSWORD", "").strip()
        if not user or not password:
            raise AgentBayDoubaoError("自动登录缺少 GEO 管理员凭据")
        jar = http.cookiejar.CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(jar))
        payload = json.dumps(
            {"user": user, "password": password},
            ensure_ascii=False,
        ).encode("utf-8")
        req = Request(
            self.base + "/api/admin/login",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.opener.open(req, timeout=10) as response:
            if response.status != 200:
                raise AgentBayDoubaoError("GEO 短信收件箱登录失败")

    def recent(self) -> List[Dict[str, Any]]:
        with self.opener.open(self.base + "/api/admin/sms", timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return list(payload.get("messages") or [])


def latest_doubao_sms_code(
    messages: Iterable[Dict[str, Any]],
    receiver: str,
    seen_request_ids: Optional[Set[str]] = None,
) -> str:
    seen = seen_request_ids or set()
    for message in messages:
        request_id = str(message.get("requestid") or "")
        if request_id and request_id in seen:
            continue
        if receiver not in str(message.get("receiver") or ""):
            continue
        result = str(message.get("result") or "")
        if "豆包" not in result:
            continue
        code = str(message.get("code") or "")
        if not re.fullmatch(r"\d{6}", code):
            match = re.search(r"(?<!\d)(\d{6})(?!\d)", result)
            code = match.group(1) if match else ""
        if code:
            return code
    return ""


def _doubao_phone_input(page: Page, timeout: float = 8):
    """Wait for the asynchronously rendered login choice before opening phone input."""
    deadline = time.monotonic() + timeout
    clicked_mobile = False
    while time.monotonic() < deadline:
        dialog = page.locator('[role="dialog"]').last
        phone_input = dialog.locator('[data-testid="login_phone_number_input"]')
        if phone_input.count() and phone_input.is_visible():
            return phone_input
        detect_platform_failure(page, "doubao")
        if not clicked_mobile:
            mobile = _visible_role_button(page, "手机号登录") or _visible_exact(page, "手机号登录")
            if mobile is not None:
                mobile.click(timeout=3000)
                clicked_mobile = True
        page.wait_for_timeout(200)
    raise AgentBayDoubaoError("豆包手机号登录表单未打开，请重新登录", error_code="platform_auth_unavailable")


def ensure_doubao_login(browser: Browser, phone: str | None = None) -> Page:
    context = browser.contexts[0] if browser.contexts else None
    if context is None:
        raise AgentBayDoubaoError("豆包浏览器没有可用上下文")
    page = next(
        (item for item in context.pages if "doubao.com" in (item.url or "")),
        context.pages[0] if context.pages else context.new_page(),
    )
    # A reused sandbox is already on the logged-in chat. Reloading it only
    # paints the login shell again and costs another page load.
    if (
        "doubao.com" in (page.url or "")
        and page_answers(page)
        and wait_doubao_auth(page, 1) == "logged_in"
    ):
        return page
    # Cookies copied into a fresh sandbox are not on a document that already
    # loaded. Reload, then wait out the sidebar that paints before the account chip.
    page.goto(DOUBAO_HOME, wait_until="domcontentloaded", timeout=30000)
    if wait_doubao_auth(page, 8) == "logged_in":
        return page
    detect_platform_failure(page, "doubao")

    if os.getenv("GEO_BROWSER_AUTO_LOGIN", "1") == "0":
        raise AgentBayDoubaoError("豆包网页未登录，请在后台完成登录", error_code="platform_auth_unavailable")

    phone = (phone or os.getenv("GEO_DOUBAO_PHONE", "")).strip()
    if not re.fullmatch(r"1\d{10}", phone):
        raise AgentBayDoubaoError("豆包自动登录手机号未配置")
    inbox = _SMSInboxClient()
    baseline = {
        str(item.get("requestid") or "")
        for item in inbox.recent()
        if item.get("requestid")
    }

    try:
        promo = page.locator('[role="dialog"]').filter(
            has_text="下载豆包电脑版"
        ).first
        if promo.count() and promo.is_visible():
            promo.get_by_role("button", name="关闭").click(timeout=3000)
    except Exception:
        pass

    dialog = page.locator('[role="dialog"]').last
    phone_ready = dialog.locator('[data-testid="login_phone_number_input"]')
    try:
        already_open = phone_ready.count() > 0 and phone_ready.is_visible()
    except Exception:
        already_open = False
    if not already_open:
        login = _visible_role_button(page, "登录") or _visible_exact(page, "登录")
        if login is None:
            page.goto(DOUBAO_HOME, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
            if _doubao_logged_in(page):
                return page
            login = _visible_role_button(page, "登录") or _visible_exact(page, "登录")
        if login is None:
            raise AgentBayDoubaoError("豆包登录入口不可用")
        login.click(timeout=5000)
    phone_input = _doubao_phone_input(page)
    dialog = page.locator('[role="dialog"]').last
    phone_input.fill(phone)
    agreement = dialog.locator('[data-testid="login_agreement_check"]')
    if agreement.get_attribute("aria-checked") != "true":
        agreement.click(timeout=3000)
    dialog.locator('[data-testid="login_next_button"]').click(timeout=5000)

    deadline = time.monotonic() + 90
    code = ""
    while time.monotonic() < deadline:
        detect_platform_failure(page, "doubao")
        try:
            code = latest_doubao_sms_code(inbox.recent(), phone, baseline)
        except Exception:
            code = ""
        if code:
            break
        time.sleep(2)
    if not code:
        raise AgentBayDoubaoError("豆包登录验证码回调超时", error_code="sms_timeout")

    code_input = dialog.locator("input").last
    code_input.fill(code)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        detect_platform_failure(page, "doubao")
        if _doubao_logged_in(page):
            return page
        time.sleep(1)
    raise AgentBayDoubaoError("豆包验证码已回填，但登录未完成")


YUANBAO_HOME = "https://yuanbao.tencent.com/chat"
YUANBAO_PHONE = ""


def _latest_sms_code(messages, receiver: str, signature: str, seen_request_ids=None) -> str:
    seen = seen_request_ids or set()
    for message in messages:
        request_id = str(message.get("requestid") or "")
        if request_id and request_id in seen:
            continue
        if receiver not in str(message.get("receiver") or ""):
            continue
        result = str(message.get("result") or "")
        if signature not in result:
            continue
        code = str(message.get("code") or "")
        if not re.fullmatch(r"\d{6}", code):
            match = re.search(r"(?<!\d)(\d{6})(?!\d)", result)
            code = match.group(1) if match else ""
        if code:
            return code
    return ""


def _recent_request_ids(messages, receiver: str, signature: str, seconds: int = 180) -> set:
    """Request ids whose SMS is still inside the platform resend countdown."""
    now = time.time()
    found = set()
    for message in messages:
        if receiver not in str(message.get("receiver") or ""):
            continue
        if signature not in str(message.get("result") or ""):
            continue
        try:
            stamp = datetime.strptime(str(message.get("time") or ""), "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        if now - stamp <= seconds:
            request_id = str(message.get("requestid") or "")
            if request_id:
                found.add(request_id)
    return found


def _yuanbao_dialog(page: Page):
    locator = page.locator(".t-dialog, .t-dialog__position, [role='dialog']")
    for i in range(locator.count() - 1, -1, -1):
        item = locator.nth(i)
        try:
            if item.is_visible():
                return item
        except Exception:
            continue
    return None


_YUANBAO_CAPTCHA_MARKERS = (
    "安全验证",
    "依次点击",
    "选择最符合描述",
    "换一组",
    "请完成验证",
)


def yuanbao_entry_failure(text: str, has_login_button: bool) -> Optional[tuple]:
    """A blank frozen page is not a logged-out account."""
    kind = yuanbao_page_kind(text, has_login_button)
    if kind != "unknown" or has_login_button:
        return None
    if len((text or "").strip()) <= 20:
        return ("platform_busy", "元宝页面没有就绪，请稍后重试")
    return ("platform_auth_unavailable", "元宝登录入口不可用")


def yuanbao_page_kind(text: str, has_login_button: bool = False) -> str:
    """Classify a Yuanbao page. The logged-out home also says 我是元宝."""
    body = text or ""
    if any(marker in body for marker in _YUANBAO_CAPTCHA_MARKERS):
        return "captcha"
    if any(
        marker in body
        for marker in ("未登录", "请登录后", "手机验证码登录", "获取验证码", "请输入手机号")
    ):
        return "logged_out"
    if has_login_button:
        return "logged_out"
    if re.search(r"1\d{2}\*{2,}\d{2,4}", body) or re.search(r"用户[A-Za-z0-9]{4,}", body):
        return "logged_in"
    if "发消息" in body and "请登录" not in body:
        return "logged_in"
    return "unknown"


def _yuanbao_body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000) or ""
    except Exception:
        return ""


def _yuanbao_has_login_button(page: Page) -> bool:
    return bool(_visible_role_button(page, "登录") or _visible_exact(page, "登录"))


def _yuanbao_captcha_visible(page: Page) -> bool:
    # The picture challenge is a cross-origin frame, so the main page text
    # does not contain it. A hidden preload of that frame is not a challenge.
    if yuanbao_page_kind(_yuanbao_body_text(page)) == "captcha":
        return True
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            text = frame.locator("body").inner_text(timeout=500) or ""
            element = frame.frame_element()
            box = element.bounding_box()
            shown = element.is_visible() and box and box["width"] > 80 and box["height"] > 80
        except Exception:
            continue
        if shown and yuanbao_page_kind(text) == "captcha":
            return True
    return False


def _clear_yuanbao_captcha(page: Page, wait_sec: float = 55) -> None:
    """Stop at a picture check and leave the page open for a person to click."""
    del wait_sec
    if _yuanbao_captcha_visible(page):
        _raise_yuanbao_captcha(page)


def _raise_yuanbao_captcha(page: Page) -> None:
    try:
        os.makedirs("/tmp/geo_login_shots", exist_ok=True)
        page.screenshot(path="/tmp/geo_login_shots/yuanbao_login_stuck.png")
    except Exception:
        pass
    raise AgentBayDoubaoError(
        "元宝触发图片安全验证，请人工完成后重试",
        error_code="platform_risk_control",
        retryable=False,
    )


def _yuanbao_logged_in(page: Page) -> bool:
    return (
        yuanbao_page_kind(_yuanbao_body_text(page), _yuanbao_has_login_button(page))
        == "logged_in"
    )


def _yuanbao_phone_filled(value: str, phone: str) -> bool:
    return phone in re.sub(r"\D", "", value or "")


def _fill_yuanbao_phone(page: Page, phone: str) -> bool:
    """Type the callback number into the phone field, including iframe dialogs."""
    script = """(phone) => {
      const visible = el => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 40 && r.height > 16 && s.visibility !== 'hidden' && s.display !== 'none';
      };
      const meta = el => [
        el.getAttribute('placeholder') || '',
        el.getAttribute('name') || '',
        el.getAttribute('aria-label') || '',
        el.getAttribute('inputmode') || '',
        el.getAttribute('autocomplete') || '',
        el.type || '',
      ].join(' ');
      const inputs = [...document.querySelectorAll('input')].filter(visible);
      const phoneInput = inputs.find(el => /手机|tel|phone/i.test(meta(el)) && !/验证码|code/i.test(meta(el)))
        || inputs.find(el => !/验证码|code|搜索|search/i.test(meta(el)));
      if (!phoneInput) return false;
      phoneInput.focus();
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
      if (setter) setter.call(phoneInput, phone);
      phoneInput.dispatchEvent(new InputEvent('input', {bubbles:true, data:phone, inputType:'insertText'}));
      phoneInput.dispatchEvent(new Event('change', {bubbles:true}));
      return (phoneInput.value || '').replace(/\\D/g, '').includes(phone);
    }"""
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        frames = [page, *[frame for frame in page.frames if frame != page.main_frame]]
        for target in frames:
            try:
                if target.evaluate(script, phone):
                    return True
            except Exception:
                continue
        page.wait_for_timeout(400)
    return False


def _type_yuanbao_code(page: Page, code: str) -> None:
    box = page.locator("input[placeholder*='验证码']").last
    try:
        box.click(timeout=3000)
        box.fill("")
        page.keyboard.type(code, delay=30)
        value = re.sub(r"\D", "", box.input_value(timeout=1000) or "")
        if value == code:
            return
    except Exception:
        pass
    typed = page.evaluate(
        """(code) => {
          const visible = el => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 2 && r.height > 2 && s.visibility !== "hidden";
          };
          const inputs = [...document.querySelectorAll("input")].filter(
            el => visible(el) && (el.placeholder || "").includes("验证码")
          );
          const el = inputs[inputs.length - 1];
          if (!el) return false;
          el.focus();
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
          if (setter) setter.call(el, "");
          document.execCommand("insertText", false, code);
          if ((el.value || "").replace(/\\D/g, "") !== code && setter) {
            setter.call(el, code);
            el.dispatchEvent(new InputEvent("input", {bubbles:true, data:code, inputType:"insertText"}));
          }
          return (el.value || "").replace(/\\D/g, "") === code;
        }""",
        code,
    )
    if not typed:
        raise AgentBayDoubaoError("元宝验证码没有写进输入框")


def ensure_yuanbao_login(browser: Browser, phone: str | None = None) -> Page:
    """Log Yuanbao in with the SMS callback number, then return its page."""
    context = browser.contexts[0] if browser.contexts else None
    if context is None:
        raise AgentBayDoubaoError("元宝浏览器没有可用上下文")
    page = next(
        (item for item in context.pages if "yuanbao.tencent.com" in (item.url or "")),
        context.pages[0] if context.pages else None,
    )
    if page is None:
        try:
            page = context.new_page()
        except Exception as exc:
            raise AgentBayDoubaoError(
                "元宝页面没有就绪，请稍后重试",
                error_code="platform_busy",
            ) from exc
    if "yuanbao.tencent.com" not in (page.url or ""):
        page.goto(YUANBAO_HOME, wait_until="domcontentloaded", timeout=30000)
    if not page_answers(page):
        # Reuse this tab. AgentBay Chrome rejects Target.createTarget.
        try:
            page.goto(YUANBAO_HOME, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
    # The hero text "我是元宝" paints before the login button. Deciding from
    # that first paint skips login and the scrape then reports logged_out.
    settle_until = time.monotonic() + 6
    while time.monotonic() < settle_until:
        # A reusable login wins over a leftover captcha frame. A real picture
        # check only stops the job when the account is still logged out.
        if _yuanbao_logged_in(page):
            return page
        if _yuanbao_captcha_visible(page):
            _clear_yuanbao_captcha(page)
            continue
        body = _yuanbao_body_text(page)
        if "未登录" in body or "请登录后" in body or _yuanbao_has_login_button(page):
            break
        page.wait_for_timeout(500)
    if _yuanbao_logged_in(page):
        return page

    if os.getenv("GEO_BROWSER_AUTO_LOGIN", "1") == "0":
        raise AgentBayDoubaoError("元宝网页未登录，请在后台完成登录", error_code="platform_auth_unavailable")

    phone = (phone or os.getenv("GEO_YUANBAO_PHONE") or YUANBAO_PHONE).strip()
    if not re.fullmatch(r"1\d{10}", phone):
        raise AgentBayDoubaoError(
            "Set GEO_YUANBAO_PHONE before starting Yuanbao login",
            error_code="platform_auth_unavailable",
        )
    inbox = _SMSInboxClient()
    baseline = {
        str(item.get("requestid") or "")
        for item in inbox.recent()
        if item.get("requestid")
    }
    dialog = _yuanbao_dialog(page)
    if dialog is None:
        login = _visible_role_button(page, "登录") or _visible_exact(page, "登录")
        if login is None:
            failure = yuanbao_entry_failure(
                _yuanbao_body_text(page), _yuanbao_has_login_button(page)
            )
            if failure is not None:
                code, message = failure
                raise AgentBayDoubaoError(message, error_code=code)
            raise AgentBayDoubaoError("元宝登录入口不可用")
        try:
            login.click(timeout=5000)
        except Exception:
            # The phone dialog often opens under the pointer and cancels the click.
            pass
        page.wait_for_timeout(800)
        dialog = _yuanbao_dialog(page)
        if dialog is None:
            raise AgentBayDoubaoError("元宝登录弹层没有打开")
    root = dialog if dialog is not None else page
    page.evaluate(
        """() => {
          const visible = el => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 2 && r.height > 2 && s.visibility !== 'hidden';
          };
          const labels = ['手机号登录', '手机', 'Phone', 'Mobile'];
          const nodes = [...document.querySelectorAll('button, a, span, div')]
            .filter(el => visible(el) && labels.includes((el.innerText || '').trim()));
          const target = nodes.sort((a, b) => a.innerText.length - b.innerText.length).pop();
          if (target) target.click();
        }"""
    )
    filled = _fill_yuanbao_phone(page, phone)
    page.evaluate(
        """() => {
          const visible = el => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 2 && r.height > 2 && s.visibility !== 'hidden';
          };
          const agree = [...document.querySelectorAll('span, div, label, p')]
            .filter(el => visible(el) && (el.innerText || '').trim().startsWith('我已阅读'))
            .sort((a, b) => a.innerText.length - b.innerText.length)[0];
          if (!agree) return;
          const box = agree.querySelector('input, [role=checkbox], [class*=check]') || agree.previousElementSibling;
          (box || agree).click();
        }"""
    )
    if not filled:
        try:
            os.makedirs("/tmp/geo_login_shots", exist_ok=True)
            page.screenshot(path="/tmp/geo_login_shots/yuanbao_phone_missing.png")
        except Exception:
            pass
        raise AgentBayDoubaoError(
            "元宝手机号没有写进输入框",
            error_code="sms_timeout",
            retryable=True,
        )
    try:
        os.makedirs("/tmp/geo_login_shots", exist_ok=True)
        page.screenshot(path="/tmp/geo_login_shots/yuanbao_login.png")
    except Exception:
        pass
    sent = page.evaluate(
        """() => {
          const visible = el => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 2 && r.height > 2 && s.visibility !== 'hidden';
          };
          const labelOf = el => (el.innerText || '').trim();
          const nodes = [...document.querySelectorAll('button, a, span, div')]
            .filter(el => {
              const text = labelOf(el);
              return visible(el) && (
                ['获取验证码', '发送验证码', '重新发送'].includes(text) ||
                /^[0-9]+秒后重发$/.test(text)
              );
            });
          const target = nodes.sort((a, b) => labelOf(a).length - labelOf(b).length).pop();
          if (!target) return '';
          const text = labelOf(target);
          if (!/^[0-9]+秒后重发$/.test(text)) target.click();
          return text;
        }"""
    )
    if not sent:
        raise AgentBayDoubaoError(
            "元宝没有点到获取验证码",
            error_code="sms_timeout",
            retryable=True,
        )
    if "秒后重发" in sent:
        try:
            baseline -= _recent_request_ids(inbox.recent(), phone, "元宝")
        except Exception:
            pass
    page.wait_for_timeout(1500)
    if _yuanbao_captcha_visible(page):
        _clear_yuanbao_captcha(page)
    deadline = time.monotonic() + 90
    code = ""
    while time.monotonic() < deadline:
        try:
            code = _latest_sms_code(inbox.recent(), phone, "元宝", baseline)
        except Exception:
            code = ""
        if code:
            break
        time.sleep(2)
    if not code:
        try:
            page.screenshot(path="/tmp/geo_login_shots/yuanbao_sms_timeout.png")
        except Exception:
            pass
        raise AgentBayDoubaoError(
            "元宝验证码没有送达，请稍后重试",
            error_code="sms_timeout",
            retryable=True,
        )
    _type_yuanbao_code(page, code)
    page.evaluate(
        """() => {
          const label = document.querySelector('.t-checkbox__label');
          const box = label && label.parentElement;
          const input = box && box.querySelector('input[type="checkbox"]');
          if (input && !input.checked) input.click();
          else if (box && !box.classList.contains('t-is-checked')) box.click();
        }"""
    )
    if _yuanbao_captcha_visible(page):
        _clear_yuanbao_captcha(page)
    dialog = _yuanbao_dialog(page) or page
    confirm = dialog.locator("button:not(.agent-dialogue__tool__login)").filter(
        has_text=re.compile(r"^\s*登录\s*$")
    )
    if confirm.count():
        confirm.last.click(timeout=5000)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if _yuanbao_captcha_visible(page):
            _clear_yuanbao_captcha(page)
            continue
        if _yuanbao_logged_in(page):
            return page
        time.sleep(1)
    try:
        page.screenshot(path="/tmp/geo_login_shots/yuanbao_login_stuck.png")
    except Exception:
        pass
    raise AgentBayDoubaoError(
        "元宝验证码已回填，但登录未完成",
        error_code="sms_timeout",
        retryable=True,
    )
