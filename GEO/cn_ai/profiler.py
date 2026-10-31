# -*- coding: utf-8 -*-
"""
CDP 研究工具：每步 profiling 耗时 + DOM 快照 + 网络请求捕获。

输出目录: .scrape_cache/research/{site}/{run_id}/
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

GEO_DIR = Path(__file__).resolve().parent.parent
RESEARCH_ROOT = GEO_DIR / ".scrape_cache" / "research"


@dataclass
class StepRecord:
    name: str
    started_at: float
    elapsed_ms: float
    ok: bool
    detail: str = ""
    dom_path: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class NetworkRecord:
    ts: float
    method: str
    url: str
    resource_type: str
    status: Optional[int] = None
    mime: Optional[str] = None
    body_preview: Optional[str] = None
    body_path: Optional[str] = None
    elapsed_ms: Optional[float] = None


class SiteProfiler:
    def __init__(self, site_id: str, out_dir: Path, capture_bodies: bool = True):
        self.site_id = site_id
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.steps: List[StepRecord] = []
        self.network: List[NetworkRecord] = []
        self._pending: Dict[str, float] = {}
        self._run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.capture_bodies = capture_bodies

    def step(self, name: str, fn: Callable[[], Any], save_dom: bool = True) -> Any:
        from cn_ai.diagnostics import current
        diagnostic = current()
        if diagnostic:
            diagnostic.phase(name)
            if hasattr(self, "_page"):
                diagnostic.checkpoint(self._page)
        t0 = time.perf_counter()
        dom_path = None
        ok = True
        detail = ""
        result = None
        try:
            result = fn()
            detail = str(result)[:500] if result is not None else "ok"
        except Exception as e:
            ok = False
            detail = f"{type(e).__name__}: {e}"
            raise
        finally:
            elapsed = (time.perf_counter() - t0) * 1000
            if save_dom and hasattr(self, "_page") and self._page:
                dom_path = self._save_dom(name)
            rec = StepRecord(
                name=name,
                started_at=t0,
                elapsed_ms=elapsed,
                ok=ok,
                detail=detail,
                dom_path=dom_path,
            )
            self.steps.append(rec)
            if diagnostic and hasattr(self, "_page"):
                diagnostic.checkpoint(self._page)
            print(f"  [{self.site_id}] {name}: {elapsed:.0f}ms {'OK' if ok else 'FAIL'}")
        return result

    def attach_page(self, page) -> None:
        self._page = page
        net_dir = self.out_dir / "network_bodies"
        net_dir.mkdir(exist_ok=True)

        def on_request(req):
            self._pending[req.url] = time.perf_counter()

        def on_response(res):
            req = res.request
            start = self._pending.pop(req.url, None)
            elapsed_ms = (time.perf_counter() - start) * 1000 if start else None
            url = req.url
            if req.resource_type in ("image", "font", "media"):
                return
            if any(x in url for x in [".png", ".jpg", ".woff", ".css", "favicon"]):
                return
            mime = res.headers.get("content-type", "")
            status = res.status
            body_preview = None
            body_path = None
            interesting = any(
                k in url.lower()
                for k in [
                    "api", "chat", "completion", "message", "stream",
                    "conversation", "query", "sse", "bot", "agent",
                    "wenxin", "yiyan", "doubao", "deepseek", "moonshot",
                    "kimi", "qianwen", "tongyi", "yuanbao", "hunyuan",
                ]
            ) or "json" in mime or "event-stream" in mime
            # 研究阶段才读 body；抓取阶段由 NetworkCapture 负责，避免 sync body() 竞态 CancelledError
            if (
                getattr(self, "capture_bodies", True)
                and interesting
                and status
                and status < 400
            ):
                try:
                    body = res.body()
                    if len(body) > 500000:
                        body_preview = body[:2000].decode("utf-8", errors="replace") + "..."
                        fname = re.sub(r"[^\w.-]", "_", url[-80:])[:60]
                        body_path = str(net_dir / f"{int(time.time()*1000)}_{fname}.bin")
                        Path(body_path).write_bytes(body)
                    else:
                        body_preview = body.decode("utf-8", errors="replace")
                except Exception:
                    pass
            self.network.append(
                NetworkRecord(
                    ts=time.time(),
                    method=req.method,
                    url=url,
                    resource_type=req.resource_type,
                    status=status,
                    mime=mime,
                    body_preview=body_preview[:8000] if body_preview else None,
                    body_path=body_path,
                    elapsed_ms=elapsed_ms,
                )
            )

        page.on("request", on_request)
        page.on("response", on_response)

    def _save_dom(self, step_name: str) -> str:
        safe = re.sub(r"[^\w.-]", "_", step_name)[:40]
        path = self.out_dir / f"dom_{safe}.html"
        try:
            html = self._page.content()
            path.write_text(html, encoding="utf-8")
            return str(path.relative_to(GEO_DIR))
        except Exception as e:
            path.write_text(f"<!-- dom capture failed: {e} -->", encoding="utf-8")
            return str(path.relative_to(GEO_DIR))

    def save_summary(self) -> Path:
        summary = {
            "site_id": self.site_id,
            "run_id": self._run_id,
            "timestamp": datetime.now().isoformat(),
            "steps": [asdict(s) for s in self.steps],
            "network_count": len(self.network),
            "network": [asdict(n) for n in self.network],
            "api_endpoints": self._extract_api_endpoints(),
        }
        path = self.out_dir / "summary.json"
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _extract_api_endpoints(self) -> List[Dict[str, Any]]:
        seen = set()
        endpoints = []
        for n in self.network:
            if n.status and n.status >= 400:
                continue
            key = f"{n.method} {urlparse(n.url).path}"
            if key in seen:
                continue
            if not any(
                k in n.url.lower()
                for k in ["api", "chat", "completion", "message", "stream", "conversation", "bot"]
            ):
                continue
            seen.add(key)
            endpoints.append({
                "method": n.method,
                "url": n.url,
                "status": n.status,
                "mime": n.mime,
                "has_body": bool(n.body_preview or n.body_path),
            })
        return endpoints
