# -*- coding: utf-8 -*-
"""六站 Playwright 登录会话：探活、自动发短信、代填验证码、截图点击。"""
from __future__ import annotations

import io
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.sync_api import Page, sync_playwright

from chinese_ai_sites import CHINESE_AI_SITES, DEFAULT_SITE_ORDER
from cn_ai.site_flows import find_input

GEO_DIR = Path(__file__).resolve().parents[1]
PROFILE_DIR = Path(os.environ.get("CN_AI_PROFILE", str(GEO_DIR / ".chrome-cn-ai-profile")))
DEFAULT_PHONE = os.environ.get("CN_AI_PHONE", "")

SEND_TEXTS = ("发送验证码", "获取验证码", "获取短信验证码", "获取短信")
LOGIN_TEXTS = ("登录", "注册", "登录 / 注册", "登录/注册", "验证并登录")
OPEN_LOGIN_TEXTS = ("登录", "注册", "登录 / 注册", "手机号登录", "短信登录")
CAPTCHA_HINTS = ("点击图中", "请完成验证", "向右滑动", "请依次点击", "请点击")

SITE_LOGIN: Dict[str, Dict[str, Any]] = {
    "deepseek": {
        "home": "https://chat.deepseek.com/",
        "login": "https://chat.deepseek.com/sign_in",
        "open": [],
    },
    "doubao": {
        "home": "https://www.doubao.com/chat/",
        "login": "https://www.doubao.com/chat/",
        "open": ["登录", "手机号登录"],
    },
    "yiyan": {
        "home": "https://yiyan.baidu.com/",
        "login": "https://yiyan.baidu.com/",
        "open": ["登录", "短信登录", "手机号登录"],
    },
    "yuanbao": {
        "home": "https://yuanbao.tencent.com/chat",
        "login": "https://yuanbao.tencent.com/chat",
        "open": ["登录", "手机号登录"],
    },
    "qianwen": {
        "home": "https://www.qianwen.com/",
        "login": "https://www.qianwen.com/",
        "open": ["登录", "手机号登录"],
    },
    "kimi": {
        "home": "https://www.kimi.com/",
        "login": "https://www.kimi.com/",
        "open": ["登录", "手机号登录"],
    },
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _visible_text(page: Page) -> str:
    try:
        return page.evaluate("() => (document.body && document.body.innerText) || ''") or ""
    except Exception:
        return ""


def _first_visible(page: Page, selectors: List[str]):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def _click_text(page: Page, texts: tuple[str, ...]) -> Optional[str]:
    for text in texts:
        try:
            loc = page.get_by_text(text, exact=True).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=2500)
                return text
        except Exception:
            pass
        try:
            loc = page.locator(f"text={text}").first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=2500)
                return text
        except Exception:
            pass
    return None


def _has_captcha(page: Page, text: str) -> bool:
    if any(h in text for h in CAPTCHA_HINTS):
        return True
    try:
        loc = page.locator("#sm-captcha, .shumei_captcha, [class*='captcha']").first
        return loc.count() > 0 and loc.is_visible()
    except Exception:
        return False


def _has_login_form(page: Page) -> bool:
    loc = _first_visible(
        page,
        [
            "input[placeholder*='手机']",
            "input[type='tel']",
            "input[placeholder*='验证码']",
            "input[placeholder*='手机号']",
        ],
    )
    return loc is not None


def _waiting_sms(text: str) -> bool:
    if re.search(r"\d+\s*秒", text):
        return True
    return any(k in text for k in ("重新发送", "再次发送", "已发送"))


def _send_button_ready(page: Page) -> bool:
    for text in SEND_TEXTS:
        try:
            loc = page.get_by_text(text, exact=True).first
            if loc.count() == 0 or not loc.is_visible():
                continue
            cls = (loc.get_attribute("class") or "") + " " + (loc.evaluate("el => el.parentElement && el.parentElement.className || ''") or "")
            if "disabled" in cls:
                return False
            return True
        except Exception:
            continue
    return False


@dataclass
class SiteState:
    site_id: str
    name: str
    status: str = "starting"
    url: str = ""
    message: str = ""
    phone: str = DEFAULT_PHONE
    last_send_at: float = 0.0
    last_check_at: float = 0.0
    updated_at: str = field(default_factory=_now)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "site_id": self.site_id,
            "name": self.name,
            "status": self.status,
            "url": self.url,
            "message": self.message,
            "phone": self.phone,
            "last_send_at": self.last_send_at,
            "updated_at": self.updated_at,
        }


class SessionManager:
    def __init__(self) -> None:
        self.phone = DEFAULT_PHONE
        self.lock = threading.RLock()
        self.states: Dict[str, SiteState] = {
            sid: SiteState(site_id=sid, name=CHINESE_AI_SITES[sid].name)
            for sid in DEFAULT_SITE_ORDER
        }
        self._pw = None
        self._context = None
        self.pages: Dict[str, Page] = {}
        self._ready = threading.Event()

    def start(self) -> None:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--ignore-certificate-errors"],
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
            ignore_https_errors=True,
        )
        for page in list(self._context.pages):
            try:
                page.close()
            except Exception:
                pass
        for sid in DEFAULT_SITE_ORDER:
            page = self._context.new_page()
            self.pages[sid] = page
            self._set(sid, status="starting", message="打开站点")
            try:
                page.goto(SITE_LOGIN[sid]["home"], wait_until="domcontentloaded", timeout=60000)
                time.sleep(1.2)
                self.refresh(sid)
            except Exception as exc:
                self._set(sid, status="error", message=f"打开失败: {type(exc).__name__}: {exc}")
        self._ready.set()

    def stop(self) -> None:
        with self.lock:
            try:
                if self._context:
                    self._context.close()
            except Exception:
                pass
            try:
                if self._pw:
                    self._pw.stop()
            except Exception:
                pass

    def watch(self, stop: threading.Event) -> None:
        self._ready.wait(timeout=180)
        while not stop.is_set():
            for sid in DEFAULT_SITE_ORDER:
                if stop.is_set():
                    break
                try:
                    self.ensure_login(sid)
                except Exception as exc:
                    self._set(sid, message=f"探活异常: {type(exc).__name__}: {exc}")
            stop.wait(12)

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "phone": self.phone,
                "ready": self._ready.is_set(),
                "sites": [self.states[sid].as_dict() for sid in DEFAULT_SITE_ORDER],
            }

    def set_phone(self, phone: str) -> None:
        phone = re.sub(r"\D", "", phone or "")
        if len(phone) != 11:
            raise ValueError("手机号需为 11 位")
        self.phone = phone
        with self.lock:
            for st in self.states.values():
                st.phone = phone

    def screenshot(self, site_id: str) -> bytes:
        page = self._page(site_id)
        with self.lock:
            raw = page.screenshot(type="png")
        return raw

    def click_norm(self, site_id: str, nx: float, ny: float) -> Dict[str, Any]:
        page = self._page(site_id)
        nx = min(max(float(nx), 0.0), 1.0)
        ny = min(max(float(ny), 0.0), 1.0)
        with self.lock:
            box = page.viewport_size or {"width": 1280, "height": 800}
            x = int(box["width"] * nx)
            y = int(box["height"] * ny)
            page.mouse.click(x, y)
            time.sleep(0.8)
        self.refresh(site_id)
        return {"ok": True, "x": x, "y": y, **self.states[site_id].as_dict()}

    def send_code(self, site_id: str, force: bool = False) -> Dict[str, Any]:
        page = self._page(site_id)
        with self.lock:
            st = self.states[site_id]
            if st.status == "logged_in":
                return st.as_dict()
            if not force and st.last_send_at and time.time() - st.last_send_at < 55:
                st.message = "发码冷却中，请稍候或直接填短信"
                st.updated_at = _now()
                return st.as_dict()
            self._set(site_id, status="sending", message="准备登录页并发送验证码")
            self._ensure_login_form(page, site_id)
            self._fill_phone(page)
            if _has_captcha(page, _visible_text(page)):
                self._set(site_id, status="need_captcha", message="请在截图上点击验证码")
                return self.states[site_id].as_dict()
            if not _send_button_ready(page):
                text = _visible_text(page)
                if _waiting_sms(text) or _has_captcha(page, text):
                    self.refresh(site_id)
                    return self.states[site_id].as_dict()
            clicked = _click_text(page, SEND_TEXTS)
            time.sleep(1.6)
            st.last_send_at = time.time()
            text = _visible_text(page)
            if _has_captcha(page, text):
                self._set(site_id, status="need_captcha", message="已点发送，请在截图上完成点选验证")
            elif _waiting_sms(text):
                self._set(site_id, status="waiting_sms", message="验证码已发送，请在上方填写短信")
            elif clicked:
                self._set(site_id, status="waiting_sms", message=f"已点击「{clicked}」，等待短信")
            else:
                self._set(site_id, status="login_form", message="没找到发码按钮，请点截图操作")
        return self.states[site_id].as_dict()

    def submit_code(self, site_id: str, code: str) -> Dict[str, Any]:
        code = re.sub(r"\D", "", code or "")
        if len(code) < 4:
            raise ValueError("验证码太短")
        page = self._page(site_id)
        with self.lock:
            self._set(site_id, status="submitting", message="正在提交验证码")
            self._ensure_login_form(page, site_id)
            box = _first_visible(
                page,
                [
                    "input[placeholder*='验证码']",
                    "input[placeholder*='短信']",
                    "input[type='tel']",
                ],
            )
            # 优先填第二个 tel / 明确写着验证码的框
            code_box = _first_visible(page, ["input[placeholder*='验证码']", "input[placeholder*='短信']"])
            if code_box is None:
                tels = page.locator("input[type='tel']")
                try:
                    if tels.count() >= 2:
                        code_box = tels.nth(1)
                    elif box is not None:
                        code_box = box
                except Exception:
                    code_box = box
            if code_box is None:
                self._set(site_id, status="login_form", message="找不到验证码输入框")
                return self.states[site_id].as_dict()
            code_box.click(timeout=3000)
            try:
                code_box.fill(code, timeout=3000)
            except Exception:
                page.keyboard.press("Control+A")
                page.keyboard.type(code, delay=40)
            time.sleep(0.3)
            clicked = _click_text(page, LOGIN_TEXTS)
            if not clicked:
                page.keyboard.press("Enter")
            time.sleep(2.2)
        self.refresh(site_id)
        st = self.states[site_id]
        if st.status != "logged_in":
            self._set(site_id, message=st.message or "已提交，若未登录请看截图")
        return self.states[site_id].as_dict()

    def refresh(self, site_id: str) -> SiteState:
        page = self._page(site_id)
        with self.lock:
            try:
                url = page.url
            except Exception:
                url = ""
            text = _visible_text(page)
            captcha = _has_captcha(page, text)
            form = _has_login_form(page)
            logged = False
            if not captcha and not form and "sign_in" not in url and "passport" not in url:
                try:
                    logged = find_input(page) is not None
                except Exception:
                    logged = False
                if not logged and any(
                    k in text for k in ("新对话", "开启新对话", "新建会话", "深度思考", "联网搜索")
                ):
                    logged = True
            if captcha:
                status, message = "need_captcha", "请在截图上点击验证码"
            elif logged:
                status, message = "logged_in", "登录有效"
            elif _waiting_sms(text):
                status, message = "waiting_sms", "等待你填写短信验证码"
            elif form:
                status, message = "login_form", "登录页已打开，将自动发送验证码"
            else:
                status, message = "unknown", "未识别到输入框，可点截图操作"
            self._set(site_id, status=status, message=message, url=url)
            self.states[site_id].last_check_at = time.time()
            return self.states[site_id]

    def ensure_login(self, site_id: str) -> None:
        st = self.refresh(site_id)
        if st.status == "logged_in":
            return
        if st.status == "need_captcha":
            return
        if st.status == "waiting_sms":
            return
        if st.status in ("sending", "submitting", "starting"):
            return
        self.send_code(site_id, force=False)

    def _ensure_login_form(self, page: Page, site_id: str) -> None:
        if _has_login_form(page):
            return
        cfg = SITE_LOGIN[site_id]
        for text in cfg.get("open") or []:
            if _click_text(page, (text,)):
                time.sleep(1.0)
                if _has_login_form(page):
                    return
        try:
            page.goto(cfg["login"], wait_until="domcontentloaded", timeout=45000)
            time.sleep(1.2)
        except Exception:
            pass
        for text in tuple(cfg.get("open") or ()) + OPEN_LOGIN_TEXTS:
            if _has_login_form(page):
                return
            if _click_text(page, (text,)):
                time.sleep(0.8)

    def _fill_phone(self, page: Page) -> None:
        box = _first_visible(
            page,
            [
                "input[placeholder*='手机号']",
                "input[placeholder*='手机']",
                "input[type='tel']",
            ],
        )
        if box is None:
            return
        try:
            current = box.input_value()
        except Exception:
            current = ""
        if re.sub(r"\D", "", current) == self.phone:
            return
        box.click(timeout=3000)
        try:
            box.fill(self.phone, timeout=3000)
        except Exception:
            page.keyboard.press("Control+A")
            page.keyboard.type(self.phone, delay=30)

    def _page(self, site_id: str) -> Page:
        if site_id not in self.pages:
            raise KeyError(site_id)
        return self.pages[site_id]

    def _set(self, site_id: str, **kwargs: Any) -> None:
        st = self.states[site_id]
        for key, value in kwargs.items():
            setattr(st, key, value)
        st.phone = self.phone
        try:
            st.url = self.pages[site_id].url
        except Exception:
            pass
        st.updated_at = _now()


manager = SessionManager()
