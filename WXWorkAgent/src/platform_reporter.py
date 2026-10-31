"""
轻量级平台上报客户端。

只依赖标准库 (urllib / threading)，在 WXWorkAgent 和 DouyinCSAgent 中均可直接使用。
所有网络调用都在后台线程执行，绝不阻塞 GUI 自动化主流程。
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any
from urllib import request as _req
from urllib.error import URLError

log = logging.getLogger("platform_reporter")


class PlatformReporter:
    """向 Agent Console 后端上报事件。"""

    def __init__(self, base_url: str, task_id: int | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.task_id = task_id

    # ── 底层 HTTP ─────────────────────────────────────────────────────────

    def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any] | None:
        url = self.base_url + path
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        req = _req.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with _req.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (URLError, OSError, json.JSONDecodeError) as e:
            log.debug("platform_reporter POST %s failed: %s", path, e)
            return None

    def _post_bg(self, path: str, data: dict[str, Any]) -> None:
        """非阻塞：在后台线程发送，不影响主流程。"""
        threading.Thread(target=self._post, args=(path, data), daemon=True).start()

    # ── 线索 API ──────────────────────────────────────────────────────────

    def report_lead(
        self,
        phone: str,
        source: str = "douyin",
        source_user: str | None = None,
        source_context: str | None = None,
    ) -> int | None:
        """
        上报新线索（幂等：同一手机号同一天只创建一次）。
        返回线索 ID，同时更新 self.task_id 供后续 report_event 使用。
        """
        r = self._post(
            "/api/leads",
            {
                "phone": phone,
                "source": source,
                "source_user": source_user,
                "source_context": source_context,
            },
        )
        if r and "id" in r:
            self.task_id = int(r["id"])
            log.info("platform: lead created/found id=%s phone=%s", self.task_id, phone)
            return self.task_id
        return None

    # ── 步骤事件 API ──────────────────────────────────────────────────────

    def report_event(
        self,
        step: str,
        ok: bool,
        detail: str | None = None,
        lead_id: int | None = None,
    ) -> None:
        lid = lead_id if lead_id is not None else self.task_id
        if not lid:
            return
        self._post(
            f"/api/leads/{lid}/event",
            {"step": step, "ok": ok, "detail": detail},
        )

    def report_event_bg(
        self,
        step: str,
        ok: bool,
        detail: str | None = None,
        lead_id: int | None = None,
    ) -> None:
        """非阻塞版本——用于 GUI 自动化中间步骤，避免网络延迟影响点击时序。"""
        lid = lead_id if lead_id is not None else self.task_id
        if not lid:
            return
        self._post_bg(
            f"/api/leads/{lid}/event",
            {"step": step, "ok": ok, "detail": detail},
        )

    # ── 对话消息 API ──────────────────────────────────────────────────────

    def report_conversation(
        self,
        platform: str,
        direction: str,
        content: str,
        contact_id: str | None = None,
        contact_name: str | None = None,
    ) -> None:
        """上报单条消息（非阻塞）。direction = 'in' | 'out'。"""
        self._post_bg(
            "/api/conversations",
            {
                "platform": platform,
                "direction": direction,
                "content": content,
                "contact_id": contact_id,
                "contact_name": contact_name,
            },
        )
