"""
单独测试：从已打开的 Kimi 页面提取完整回答 + citations，并正确保存。

用法：浏览器已打开 Kimi 且有回答时，运行：
  cd GEO
  set CDP_URL=http://127.0.0.1:9222
  py -3.11 test_extract_save.py
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import List, Optional
from urllib.parse import urlparse

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

os.environ["NO_PROXY"] = os.environ.get("NO_PROXY", "*") or "*"
os.environ["no_proxy"] = os.environ.get("no_proxy", "*") or "*"

# 复用 test.py 的 CDP patch
import cdp_use.client as _cdp_client_mod
_orig_cdp_start = _cdp_client_mod.CDPClient.start

async def _patched_cdp_client_start(self):
    if self.ws is not None:
        raise RuntimeError("Client is already started")
    import websockets
    connect_kwargs = {"max_size": self.max_ws_frame_size}
    if self.additional_headers:
        connect_kwargs["additional_headers"] = self.additional_headers
    if "127.0.0.1" in self.url or "localhost" in self.url:
        connect_kwargs["proxy"] = None
    self.ws = await websockets.connect(self.url, **connect_kwargs)
    self._message_handler_task = asyncio.create_task(self._handle_messages())

_cdp_client_mod.CDPClient.start = _patched_cdp_client_start

from browser_use import Browser, BrowserProfile
import browser_use.browser.profile as _profile_mod
import browser_use.browser.session as _session_mod
from urllib.parse import urlparse as _urlparse

# CDP connect patch (curl fallback)
def _parse_cdp_json(raw: bytes) -> dict:
    if b"\r\n\r\n" not in raw:
        raise ValueError("Invalid HTTP response")
    body = raw.split(b"\r\n\r\n", 1)[1]
    text = body.decode("utf-8", errors="replace")
    start, end = text.find("{"), text.rfind("}") + 1
    if start >= 0 and end > start:
        return json.loads(text[start:end])
    raise ValueError("No JSON in response")

def _fetch_cdp_via_curl(host: str, port: int) -> dict:
    import subprocess
    r = subprocess.run(
        ["curl.exe", "-s", "--connect-timeout", "5", "--noproxy", "*", f"http://{host}:{port}/json/version"],
        capture_output=True, timeout=10,
    )
    if r.returncode != 0:
        raise OSError(f"curl failed: {r.stderr.decode(errors='replace')}")
    return _parse_cdp_json(r.stdout)

_orig_session_connect = _session_mod.BrowserSession.connect

async def _patched_session_connect_impl(self, cdp_url=None):
    url = (cdp_url or getattr(self, "cdp_url", None) or getattr(self.browser_profile, "cdp_url", None) or "").strip()
    if url and not url.startswith("ws") and ("127.0.0.1" in url or "localhost" in url):
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host, port = parsed.hostname or "127.0.0.1", parsed.port or 9222
        try:
            data = await asyncio.get_event_loop().run_in_executor(None, lambda: _fetch_cdp_via_curl(host, port))
            if data.get("webSocketDebuggerUrl"):
                cdp_url = data["webSocketDebuggerUrl"]
                self.browser_profile.cdp_url = cdp_url
        except Exception:
            pass
    return await _orig_session_connect(self, cdp_url)

_session_mod.BrowserSession.connect = _patched_session_connect_impl

# skip profile copy for CDP
_CDP_USER_DATA = os.path.normpath(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data_CDP"))
_orig_copy = _profile_mod.BrowserProfile._copy_profile
def _skip_copy(self):
    if self.user_data_dir and os.path.normpath(str(self.user_data_dir)) == _CDP_USER_DATA:
        return
    _orig_copy(self)
_profile_mod.BrowserProfile._copy_profile = _skip_copy

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


@dataclass
class ChatGPTResult:
    prompt: str
    answer: str
    brand_mentioned: bool
    brand_rank: Optional[int]
    citations: List[str]
    citation_domains: List[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _extract_citations_from_markdown(md: str) -> List[str]:
    """从 markdown 提取引用 URL（排除 Kimi 内部资源，保留真实引用来源）"""
    urls = []
    seen = set()

    def _add(u: str) -> None:
        u = u.strip().rstrip('.,;:)')
        if not u or u in seen:
            return
        # 排除 Kimi 内部资源
        if any(x in u for x in [
            'kimi-img.moonshot.cn', 'avatar.moonshot.cn',
            'sceneLightWebsites', 'sceneLightDocs', 'sceneLightPPT',
            'sceneLightExcel', 'sceneLightDeepresearch', '.svg', '.jpeg', '.png'
        ]):
            return
        # icon-cache 格式: .../icon-cache-img/www.soundguys.com -> https://www.soundguys.com
        if 'icon-cache-img/' in u:
            domain = u.split('icon-cache-img/')[-1].split('/')[0]
            if domain and '.' in domain and not domain.startswith('www.kimi'):
                u = f"https://{domain}" if not domain.startswith('http') else domain
            else:
                return
        seen.add(u)
        urls.append(u)

    # [xxx](url)
    for m in re.finditer(r'\[([^\]]*)\]\((https?://[^\)]+)\)', md):
        _add(m.group(2))
    # ![](url) 图片链接中的 icon-cache
    for m in re.finditer(r'!\[\]\((https?://[^\)]+)\)', md):
        _add(m.group(1))
    # 裸 URL（外部来源）
    for m in re.finditer(r'\b(https?://[^\s\)\]\>\"]+)', md):
        u = m.group(1)
        if 'kimi.moonshot' in u or 'moonshot.cn' in u:
            if 'icon-cache-img/' in u:
                domain = u.split('icon-cache-img/')[-1].split('/')[0]
                if domain and '.' in domain:
                    _add(f"https://{domain}")
        elif 'kimi.com' not in u:
            _add(u)

    return list(dict.fromkeys(urls))


def _parse_answer_and_citations(md: str, prompt: str) -> tuple[str, List[str]]:
    """
    从 extract 得到的 markdown 中解析 Kimi 回答区域和 citations。
    定位 AI 回答：通常包含 prompt 或 "Based on" 等开头，到 citation 图标结束。
    """
    citations = _extract_citations_from_markdown(md)

    # 尝试定位回答主体：从 prompt 首次出现或 "Based on" 等典型开头到 "查看全部" 或类似结尾
    prompt_lower = prompt.lower()
    md_lower = md.lower()

    # 找回答起始：标题行 "## Best AI..." 或 "Based on"、"Here are" 等
    start_idx = -1
    for m in ["based on the latest", "here are the", "here are some"]:
        i = md_lower.find(m)
        if i >= 0 and (start_idx < 0 or i < start_idx):
            start_idx = i
    if start_idx < 0 and prompt[:40] in md:
        start_idx = md.find(prompt[:40])
    if start_idx >= 0:
        # 往前找 "##" 标题
        hash_pos = md.rfind("\n## ", 0, start_idx + 1)
        if hash_pos >= 0:
            start_idx = hash_pos + 1
        md = md[start_idx:]

    # 去掉开头侧栏（限时活动、网站、文档等，到 ## 标题前）
    if md.startswith("限时") or "网站](/websites)" in md[:500]:
        hash_pos = md.find("\n## ")
        if hash_pos > 0:
            md = md[hash_pos + 1:]

    # 去掉尾部 "查看全部"、底部 UI 等
    for tail in ["\n\n[查看全部]", "\n[查看全部]", "\n引用\n", "\n尽管问", "\n内容由AI生成"]:
        if tail in md:
            md = md[: md.find(tail)]

    # 去掉明显 UI 碎片（单行按钮/标签）
    for ui in ["编辑\n", "复制\n", "分享\n", "搜索网页\n", "表格\n", "引用\n"]:
        md = md.replace(ui, "")
    md = re.sub(r"\n\d+ 个结果\n", "\n", md)
    # 去掉 citation 图标 ![](kimi-web-img...)（已在 citations 列表中）
    md = re.sub(r'!\[\]\(https://kimi-web-img\.moonshot\.cn[^\)]+\)', '', md)

    answer = md.strip()
    if len(answer) > 50000:
        answer = answer[:50000] + "\n\n[...truncated]"
    return answer, citations


async def main():
    cdp_url = os.environ.get("CDP_URL", "http://127.0.0.1:9222").strip()
    prompt = "best AI translation earbuds for travel 2026"
    target_brand = "Timekettle"

    log.info("连接 CDP: %s", cdp_url)
    # Browser 是 BrowserSession 的别名
    session = Browser(browser_profile=BrowserProfile(cdp_url=cdp_url), is_local=False)
    await session.start()

    try:
        log.info("提取页面内容 (extract_links=True)...")
        from browser_use.dom.markdown_extractor import extract_clean_markdown

        if session._dom_watchdog:
            session._dom_watchdog.clear_cache()
        await asyncio.sleep(1)

        content, stats = await extract_clean_markdown(browser_session=session, extract_links=True)
        log.info("提取完成: %d 字符, url=%s", len(content), stats.get("url", "?"))

        answer, citations = _parse_answer_and_citations(content, prompt)
        log.info("解析: answer 长度=%d, citations 数量=%d", len(answer), len(citations))
        for i, c in enumerate(citations[:10], 1):
            log.info("  [%d] %s", i, c[:80] + ("..." if len(c) > 80 else ""))
        if len(citations) > 10:
            log.info("  ... 共 %d 个", len(citations))

        domains = [_urlparse(u).netloc for u in citations if u]
        brand_mentioned = target_brand.lower() in answer.lower()
        rank = None
        if brand_mentioned:
            words = answer.lower().split()
            for i, w in enumerate(words):
                if target_brand.lower() in w:
                    rank = i + 1
                    break

        res = ChatGPTResult(
            prompt=prompt,
            answer=answer,
            brand_mentioned=brand_mentioned,
            brand_rank=rank,
            citations=citations,
            citation_domains=domains,
        )

        # 保存
        cache_dir = Path(__file__).parent / ".scrape_cache"
        cache_dir.mkdir(exist_ok=True)
        key = hashlib.sha256(f"{prompt}|{target_brand}".encode()).hexdigest()[:16]
        path = cache_dir / f"{key}.json"
        path.write_text(json.dumps(res.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("已保存: %s", path)
        log.info("answer 前 300 字符: %s", answer[:300].replace("\n", " "))
    finally:
        await session.stop()


if __name__ == "__main__":
    asyncio.run(main())
