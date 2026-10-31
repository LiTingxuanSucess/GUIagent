import asyncio
import hashlib
import json
import logging
import re
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional
from dataclasses import dataclass, asdict

# Windows 控制台 UTF-8，避免 emoji/中文乱码
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 避免代理导致 CDP 连接 localhost 超时/失败（* 最激进，确保所有库都不走代理）
os.environ["NO_PROXY"] = os.environ.get("NO_PROXY", "*") or "*"
os.environ["no_proxy"] = os.environ.get("no_proxy", "*") or "*"

# CDP WebSocket 必须显式 proxy=None，否则 websockets 库会走系统代理导致 localhost 连接卡住
import cdp_use.client as _cdp_client_mod
_orig_cdp_start = _cdp_client_mod.CDPClient.start

async def _patched_cdp_client_start(self):
    if self.ws is not None:
        raise RuntimeError("Client is already started")
    import websockets
    import logging as _log
    _log.getLogger("cdp_use.client").info(
        "Connecting to %s (max frame size: %.0fMB)", self.url, self.max_ws_frame_size / 1024 / 1024
    )
    connect_kwargs = {"max_size": self.max_ws_frame_size}
    if self.additional_headers:
        connect_kwargs["additional_headers"] = self.additional_headers
    # localhost CDP 连接不走代理，否则会卡在 on_BrowserStartEvent
    if "127.0.0.1" in self.url or "localhost" in self.url:
        connect_kwargs["proxy"] = None
    self.ws = await websockets.connect(self.url, **connect_kwargs)
    self._message_handler_task = asyncio.create_task(self._handle_messages())

_cdp_client_mod.CDPClient.start = _patched_cdp_client_start
# Chrome 带 URL 启动时加载页面需更长时间
os.environ.setdefault("TIMEOUT_BrowserLaunchEvent", "60")
os.environ.setdefault("TIMEOUT_BrowserStartEvent", "60")

from browser_use import Agent, Browser, BrowserProfile, ChatOpenAI
import browser_use.browser.profile as _profile_mod
import browser_use.browser.session as _session_mod
from urllib.parse import urlparse

# CDP 连接 patch：用原始 TCP 拉取 /json/version，完全绕过代理（httpx/aiohttp 都可能走代理）
_orig_session_connect = _session_mod.BrowserSession.connect

def _parse_cdp_json(raw: bytes) -> dict:
    """从 HTTP body 解析 CDP JSON"""
    if b"\r\n\r\n" not in raw:
        raise ValueError("Invalid HTTP response: no headers end")
    body = raw.split(b"\r\n\r\n", 1)[1]
    text = body.decode("utf-8", errors="replace")
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        return json.loads(text[start:end])
    raise ValueError("Invalid HTTP response: no JSON")

async def _fetch_cdp_via_raw_tcp(host: str, port: int) -> dict:
    """原始 TCP 请求，不走任何代理"""
    path = "/json/version"
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port),
        timeout=15,
    )
    try:
        req = f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
        writer.write(req.encode())
        await writer.drain()
        raw = b""
        while True:
            chunk = await asyncio.wait_for(reader.read(8192), timeout=10)
            if not chunk:
                break
            raw += chunk
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    return _parse_cdp_json(raw)

def _fetch_cdp_via_curl(host: str, port: int) -> dict:
    """用 curl.exe 拉取，完全绕过 Python 代理"""
    import subprocess
    url = f"http://{host}:{port}/json/version"
    r = subprocess.run(
        ["curl.exe", "-s", "--connect-timeout", "5", "--noproxy", "*", url],
        capture_output=True,
        timeout=10,
    )
    if r.returncode != 0:
        raise OSError(f"curl failed: {r.stderr.decode(errors='replace')}")
    return _parse_cdp_json(r.stdout)

async def _patched_session_connect(self, cdp_url=None):
    url = (cdp_url or getattr(self, "cdp_url", None) or getattr(self.browser_profile, "cdp_url", None) or "").strip()
    # 若为 http 且是 localhost，用多种方式拉取 ws URL（完全绕过代理）
    if url and not url.startswith("ws") and ("127.0.0.1" in url or "localhost" in url):
        parsed = urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 9222
        path = (parsed.path or "").rstrip("/")
        if not path.endswith("/json/version"):
            path = path + "/json/version"
        fetch_url = f"http://{host}:{port}{path}"
        data = None
        # 1) curl（绕过 Python 代理）
        try:
            data = await asyncio.get_event_loop().run_in_executor(None, lambda: _fetch_cdp_via_curl(host, port))
        except Exception:
            pass
        # 2) raw TCP
        if not data:
            try:
                data = await _fetch_cdp_via_raw_tcp(host, port)
            except Exception:
                pass
        # 3) httpx 显式 proxy=None + trust_env=False（完全绕过环境变量代理）
        if not data:
            try:
                import httpx
                async with httpx.AsyncClient(proxy=None, trust_env=False, timeout=15) as client:
                    r = await client.get(fetch_url)
                    r.raise_for_status()
                    data = r.json()
            except Exception:
                pass
        if data and data.get("webSocketDebuggerUrl"):
            cdp_url = data["webSocketDebuggerUrl"]
            self.browser_profile.cdp_url = cdp_url
    return await _orig_session_connect(self, cdp_url)

_session_mod.BrowserSession.connect = _patched_session_connect

# 专用 CDP Profile：Chrome 开启 CDP 必须用非默认目录，手动登录一次 Kimi 后复用
_CDP_USER_DATA = os.path.normpath(
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data_CDP")
)
_REAL_CHROME_USER_DATA = os.path.normpath(
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")
)
_orig_copy_profile = _profile_mod.BrowserProfile._copy_profile

def _patched_copy_profile(self):
    if self.user_data_dir is None:
        return
    ud = os.path.normpath(str(self.user_data_dir))
    if ud in (_REAL_CHROME_USER_DATA, _CDP_USER_DATA):
        from browser_use.utils import logger
        logger.info("Using Chrome User Data (no copy): %s", ud)
        return
    _orig_copy_profile(self)

_profile_mod.BrowserProfile._copy_profile = _patched_copy_profile

# 启动时直接打开 kimi.com（Chrome 支持末尾传 URL）
_orig_create_subprocess = asyncio.create_subprocess_exec

async def _patched_create_subprocess_exec(*args, **kwargs):
    cmd = list(args)
    if len(cmd) >= 2 and "chrome" in str(cmd[0]).lower():
        cmd.append("https://kimi.moonshot.cn")
        log.info("[Chrome] Launching with Kimi URL...")
    return await _orig_create_subprocess(*cmd, **kwargs)

asyncio.create_subprocess_exec = _patched_create_subprocess_exec

# _wait_for_cdp_url 默认 30 秒，Chrome 带 URL 启动时需更久；加日志便于 debug
import browser_use.browser.watchdogs.local_browser_watchdog as _lbd
_orig_wait_cdp = _lbd.LocalBrowserWatchdog._wait_for_cdp_url

async def _patched_wait_for_cdp_url_impl(port: int, timeout: float = 90) -> str:
    import time
    import aiohttp
    start = time.time()
    last_log = 0
    while time.time() - start < timeout:
        try:
            async with aiohttp.ClientSession(trust_env=False) as session:
                async with session.get(f'http://127.0.0.1:{port}/json/version') as resp:
                    if resp.status == 200:
                        log.info("[CDP] Chrome ready after %.1fs", time.time() - start)
                        return f'http://127.0.0.1:{port}/'
        except Exception as e:
            if time.time() - last_log > 5:
                log.info("[CDP] Waiting for Chrome port %s... (%.0fs) last_err=%s", port, time.time() - start, type(e).__name__)
                last_log = time.time()
        await asyncio.sleep(0.2)
    raise TimeoutError(f'Browser did not start within {timeout} seconds')
_lbd.LocalBrowserWatchdog._wait_for_cdp_url = staticmethod(_patched_wait_for_cdp_url_impl)

# 配置日志：带时间戳，输出到控制台（避免 emoji 用 ASCII）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
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

    @classmethod
    def from_dict(cls, d: dict) -> "ChatGPTResult":
        return cls(**d)


# --- Site config and extractors ---
@dataclass
class SiteConfig:
    url: str
    extractor: str  # "kimi" | "generic"


SITE_CONFIGS: Dict[str, SiteConfig] = {
    "kimi": SiteConfig(url="https://kimi.moonshot.cn", extractor="kimi"),
    "chatgpt": SiteConfig(url="https://chat.openai.com", extractor="generic"),
    "perplexity": SiteConfig(url="https://www.perplexity.ai", extractor="generic"),
    "gemini": SiteConfig(url="https://gemini.google.com", extractor="generic"),
}


# 排除的引用域名（仅当 URL 的 host 属于这些时才排除，不含 query 中的）
_CITATION_EXCLUDE_DOMAINS = frozenset({
    "chatgpt.com", "chat.openai.com", "openai.com",
    "kimi.moonshot.cn", "kimi.com", "moonshot.cn",
    "perplexity.ai", "google.com", "gemini.google.com",
    "googleusercontent.com", "gstatic.com", "googleapis.com",
    "gravatar.com",
})


def _is_valid_citation_url(u: str, site: str = "generic") -> bool:
    """判断 URL 是否可作为 citation（排除内部链接、静态资源等）"""
    u = u.strip().rstrip(".,;:)")
    if not u or not u.startswith("http"):
        return False
    if any(x in u for x in [
        "kimi-img.moonshot.cn", "avatar.moonshot.cn",
        "sceneLightWebsites", "sceneLightDocs", ".svg", ".jpeg", ".png",
    ]):
        return False
    try:
        p = urlparse(u)
        netloc = (p.netloc or "").lower().lstrip("www.")
        if not netloc or "." not in netloc:
            return False
        # 排除内部站点
        for excl in _CITATION_EXCLUDE_DOMAINS:
            if netloc == excl or netloc.endswith("." + excl):
                return False
        return True
    except Exception:
        return False


def _extract_citations_from_markdown(md: str, site: str = "kimi") -> List[str]:
    """从 markdown 提取引用 URL。Kimi 有 icon-cache 格式；其他站点用通用 [text](url)"""
    urls = []
    seen = set()

    def _add(u: str) -> None:
        u = u.strip().rstrip(".,;:)")
        if not u or u in seen:
            return
        if "icon-cache-img/" in u:
            domain = u.split("icon-cache-img/")[-1].split("/")[0]
            if domain and "." in domain and not domain.startswith("www.kimi"):
                u = f"https://{domain}" if not domain.startswith("http") else domain
            else:
                return
        if not _is_valid_citation_url(u, site):
            return
        seen.add(u)
        urls.append(u)

    for m in re.finditer(r"\[([^\]]*)\]\((https?://[^\)]+)\)", md):
        _add(m.group(2))
    for m in re.finditer(r"!\[\]\((https?://[^\)]+)\)", md):
        _add(m.group(1))
    for m in re.finditer(r"\b(https?://[^\s\)\]\>\"]+)", md):
        u = m.group(1)
        if "kimi.moonshot" in u or "moonshot.cn" in u:
            if "icon-cache-img/" in u:
                domain = u.split("icon-cache-img/")[-1].split("/")[0]
                if domain and "." in domain:
                    _add(f"https://{domain}")
        else:
            _add(u)

    return list(dict.fromkeys(urls))


def _extract_citations_from_html(html: str, site: str = "generic") -> List[str]:
    """从 HTML 提取所有 href 链接作为 citation 补充（抓取回答中的全部 links）"""
    urls = []
    seen = set()
    # href="..." 或 href='...'
    for m in re.finditer(r'href\s*=\s*["\'](https?://[^"\']+)["\']', html, re.I):
        u = m.group(1)
        if u in seen:
            continue
        if _is_valid_citation_url(u, site):
            seen.add(u)
            urls.append(u)
    return list(dict.fromkeys(urls))


def _parse_answer_kimi(md: str, prompt: str) -> tuple[str, List[str]]:
    """Kimi 专用：解析回答区域和 citations"""
    citations = _extract_citations_from_markdown(md, "kimi")
    md_lower = md.lower()

    start_idx = -1
    for m in ["based on the latest", "here are the", "here are some"]:
        i = md_lower.find(m)
        if i >= 0 and (start_idx < 0 or i < start_idx):
            start_idx = i
    if start_idx < 0 and len(prompt) >= 20 and prompt[:40] in md:
        start_idx = md.find(prompt[:40])
    if start_idx >= 0:
        hash_pos = md.rfind("\n## ", 0, start_idx + 1)
        if hash_pos >= 0:
            start_idx = hash_pos + 1
        md = md[start_idx:]

    if md.startswith("限时") or "网站](/websites)" in md[:500]:
        hash_pos = md.find("\n## ")
        if hash_pos > 0:
            md = md[hash_pos + 1:]

    for tail in ["\n\n[查看全部]", "\n[查看全部]", "\n引用\n", "\n尽管问", "\n内容由AI生成"]:
        if tail in md:
            md = md[: md.find(tail)]

    for ui in ["编辑\n", "复制\n", "分享\n", "搜索网页\n", "表格\n", "引用\n"]:
        md = md.replace(ui, "")
    md = re.sub(r"\n\d+ 个结果\n", "\n", md)
    md = re.sub(r"!\[\]\(https://kimi-web-img\.moonshot\.cn[^\)]+\)", "", md)

    answer = md.strip()
    if len(answer) > 50000:
        answer = answer[:50000] + "\n\n[...truncated]"
    return answer, citations


def _merge_citations(md_citations: List[str], html_citations: List[str]) -> List[str]:
    """合并 markdown 与 HTML 提取的 citations，去重并保持顺序（md 优先），过滤 null/空"""
    seen = set()
    out = []
    for u in md_citations:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    for u in html_citations:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _parse_answer_generic(md: str, prompt: str, current_url: str = "", html: str = "") -> tuple[str, List[str]]:
    """通用：ChatGPT/Perplexity/Gemini 等。html 可选，用于补充提取回答中的全部 links"""
    md_citations = _extract_citations_from_markdown(md, "generic")
    html_citations = _extract_citations_from_html(html, "generic") if html else []
    citations = _merge_citations(md_citations, html_citations)

    # 找回答主体：prompt 之后或典型 AI 开头
    start_idx = -1
    prompt_short = prompt[:50] if len(prompt) >= 20 else prompt
    if prompt_short in md:
        start_idx = md.find(prompt_short)
    for m in ["based on", "here are", "here is", "according to"]:
        i = md.lower().find(m)
        if i >= 0 and (start_idx < 0 or i < start_idx):
            start_idx = i
    if start_idx >= 0:
        md = md[start_idx:]

    # 去掉尾部常见 UI
    for tail in ["\n\nShare", "\n\nCopy", "Regenerate", "Thumbs up", "Thumbs down"]:
        if tail in md:
            md = md[: md.find(tail)]

    answer = md.strip()
    if len(answer) > 50000:
        answer = answer[:50000] + "\n\n[...truncated]"
    return answer, citations


CACHE_DIR = Path(__file__).parent / ".scrape_cache"
# 每个 site 单独目录：results=最终结果，runs=agent 步骤/LLM 历史，html=完整 HTML（调试用）
RESULTS_SUBDIR = "results"
RUNS_SUBDIR = "runs"
HTML_SUBDIR = "html"


def _cache_key(prompt: str, target_brand: str, site: str = "kimi") -> str:
    """prompt+target_brand 的 hash，site 在路径中体现"""
    h = hashlib.sha256(f"{prompt}|{target_brand}".encode()).hexdigest()[:16]
    return h


def _cache_key_legacy(prompt: str, target_brand: str, site: str = "kimi") -> str:
    """旧版 cache key（含 site），用于兼容迁移"""
    return hashlib.sha256(f"{prompt}|{target_brand}|{site}".encode()).hexdigest()[:16]


def _result_path(site: str, cache_key: str) -> Path:
    return CACHE_DIR / site / RESULTS_SUBDIR / f"{cache_key}.json"


def _run_path(site: str, cache_key: str) -> Path:
    return CACHE_DIR / site / RUNS_SUBDIR / f"{cache_key}.json"


def _html_path(site: str, cache_key: str) -> Path:
    return CACHE_DIR / site / HTML_SUBDIR / f"{cache_key}.html"


def _load_cache(prompt: str, target_brand: str, site: str = "kimi") -> Optional[ChatGPTResult]:
    path = _result_path(site, _cache_key(prompt, target_brand, site))
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            log.info("缓存命中: %s", path.relative_to(CACHE_DIR))
            return ChatGPTResult.from_dict(data)
        except Exception as e:
            log.debug("读取缓存失败: %s", e)
    # 兼容旧版扁平结构 .scrape_cache/{hash}.json
    legacy_path = CACHE_DIR / f"{_cache_key_legacy(prompt, target_brand, site)}.json"
    if legacy_path.exists():
        try:
            data = json.loads(legacy_path.read_text(encoding="utf-8"))
            log.info("缓存命中(旧): %s", legacy_path.name)
            res = ChatGPTResult.from_dict(data)
            _save_cache(res, target_brand, site)  # 迁移到新路径
            try:
                legacy_path.unlink()
            except Exception:
                pass
            return res
        except Exception as e:
            log.debug("读取旧缓存失败: %s", e)
    return None


def _save_cache(result: ChatGPTResult, target_brand: str, site: str = "kimi"):
    path = _result_path(site, _cache_key(result.prompt, target_brand, site))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("已缓存结果: %s", path.relative_to(CACHE_DIR))
    except Exception as e:
        log.debug("写入缓存失败: %s", e)


def _save_run_cache(site: str, prompt: str, target_brand: str, run_data: dict):
    """保存 agent 步骤/LLM 历史，与结果分开，供调试用"""
    path = _run_path(site, _cache_key(prompt, target_brand, site))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(run_data, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("已缓存 run: %s", path.relative_to(CACHE_DIR))
    except Exception as e:
        log.debug("写入 run 缓存失败: %s", e)


async def _get_page_html(session) -> str:
    """通过 CDP 获取当前页面完整 HTML"""
    try:
        cdp_session = await session.get_or_create_cdp_session(target_id=None, focus=False)
        result = await cdp_session.cdp_client.send.Runtime.evaluate(
            params={"expression": "document.documentElement.outerHTML", "returnByValue": True},
            session_id=cdp_session.session_id,
        )
        if result.get("exceptionDetails"):
            return ""
        val = result.get("result", {}).get("value")
        return val if isinstance(val, str) else ""
    except Exception as e:
        log.debug("获取页面 HTML 失败: %s", e)
        return ""


async def _get_page_html_via_direct_cdp(
    host: str = "127.0.0.1", port: int = 9222, prefer_url: str = ""
) -> str:
    """通过直接 CDP 连接获取页面 HTML（session 失效时的 fallback）"""
    try:
        import httpx
        async with httpx.AsyncClient(proxy=None, trust_env=False, timeout=10) as client:
            r = await client.get(f"http://{host}:{port}/json/list")
            r.raise_for_status()
            targets = r.json()
        # 只取 type=page 的 target，browser target 无法执行 Runtime.evaluate
        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            pages = targets  # fallback
        # 优先匹配当前站点 URL
        ws_url = None
        if prefer_url:
            for p in pages:
                if prefer_url in (p.get("url") or ""):
                    ws_url = p.get("webSocketDebuggerUrl")
                    break
        if not ws_url:
            ws_url = pages[0].get("webSocketDebuggerUrl") if pages else None
        if not ws_url:
            log.debug("直接 CDP: 无可用 page target")
            return ""
        import websockets
        async with websockets.connect(ws_url, max_size=2**20, close_timeout=2) as ws:
            msg_id = 1
            await ws.send(json.dumps({
                "id": msg_id,
                "method": "Runtime.evaluate",
                "params": {"expression": "document.documentElement.outerHTML", "returnByValue": True},
            }))
            for _ in range(50):
                resp = await asyncio.wait_for(ws.recv(), timeout=10)
                data = json.loads(resp)
                if data.get("id") == msg_id:
                    if "error" in data:
                        log.debug("直接 CDP Runtime.evaluate 错误: %s", data.get("error"))
                        return ""
                    val = data.get("result", {}).get("result", {}).get("value")
                    return val if isinstance(val, str) else ""
            return ""
    except Exception as e:
        log.info("直接 CDP 获取 HTML 失败: %s", e)
        return ""


def _save_html(site: str, cache_key: str, html: str):
    """保存完整 HTML 到 html 子目录"""
    if not html:
        return
    path = _html_path(site, cache_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(html, encoding="utf-8", errors="replace")
        log.info("已保存 HTML: %s (%d 字符)", path.relative_to(CACHE_DIR), len(html))
    except Exception as e:
        log.debug("写入 HTML 失败: %s", e)


def _kill_chrome_before_run():
    """启动前自动结束 Chrome，避免 user_data_dir 被占用"""
    try:
        import subprocess
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/IM", "chrome.exe", "/T"],
                capture_output=True,
                timeout=10,
            )
        else:
            subprocess.run(
                ["pkill", "-9", "-f", "chrome"],
                capture_output=True,
                timeout=10,
            )
        import time
        time.sleep(2)
        log.info("已尝试结束 Chrome 进程")
    except Exception as e:
        log.debug("结束 Chrome 时: %s", e)


# Chrome 用户数据目录（专用 CDP Profile，支持 remote-debugging-port）
if sys.platform == "win32":
    USER_DATA_DIR = os.path.normpath(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data_CDP"))
    CHROME_PATH = r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
    if not os.path.exists(CHROME_PATH):
        CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
elif sys.platform == "darwin":
    USER_DATA_DIR = os.path.expanduser("~/Library/Application Support/Google/Chrome")
    CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
else:
    USER_DATA_DIR = os.path.expanduser("~/.config/google-chrome")
    CHROME_PATH = "/usr/bin/google-chrome"


class ChatGPTScraper:
    def __init__(
        self,
        api_key: str,
        model: str = "qwen-plus",
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        user_data_dir: Optional[str] = None,
        executable_path: Optional[str] = None,
    ):
        # DashScope OpenAI 兼容接口 (chat/completions)
        log.info("初始化 LLM: model=%s, base_url=%s", model, base_url)
        self.llm = ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=0.1
        )
        self._user_data_dir = user_data_dir or USER_DATA_DIR
        self._executable_path = executable_path or CHROME_PATH
        self._browser: Optional[Browser] = None
        log.info("ChatGPTScraper 初始化完成 (Browser 延迟创建)")

    @property
    def browser(self) -> Browser:
        if self._browser is None:
            cdp_url = os.environ.get("CDP_URL", "").strip()
            if cdp_url:
                log.info("连接已有 Chrome: cdp_url=%s", cdp_url)
                self._browser = Browser(
                    browser_profile=BrowserProfile(cdp_url=cdp_url),
                    is_local=False,
                )
            else:
                log.info("延迟创建 Browser: user_data_dir=%s", self._user_data_dir)
                self._browser = Browser(
                    headless=False,
                    user_data_dir=self._user_data_dir,
                    executable_path=self._executable_path,
                    profile_directory="Default",
                    args=["--disable-extensions", "--disable-sync"],
                )
        return self._browser

    async def _fetch_via_llm(self, prompt: str, target_brand: str) -> ChatGPTResult:
        """直接用 LLM 回答，不打开浏览器"""
        from browser_use.llm.messages import UserMessage
        from urllib.parse import urlparse

        user_msg = f'问题: {prompt}\n检查回答中是否提到品牌"{target_brand}"，若提到记录是第几个推荐。返回严格 JSON：{{"answer":"完整回答","citations":["url1"],"brand_mentioned":true/false,"brand_rank":数字或null}}'
        msgs = [UserMessage(content=user_msg)]
        resp = await self.llm.ainvoke(msgs)
        content = resp.completion if hasattr(resp, "completion") else str(resp)
        json_match = re.search(r"\{.*\}", content, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
        else:
            data = {"answer": content, "citations": [], "brand_mentioned": target_brand.lower() in content.lower(), "brand_rank": None}
        citations = [u for u in data.get("citations", []) if u]  # 过滤 null/空
        domains = [urlparse(u).netloc for u in citations if u]
        return ChatGPTResult(
            prompt=prompt,
            answer=data.get("answer", ""),
            brand_mentioned=data.get("brand_mentioned", False),
            brand_rank=data.get("brand_rank"),
            citations=citations,
            citation_domains=domains,
        )

    async def scrape(
        self,
        prompt: str,
        target_brand: str,
        max_wait: int = 60,
        use_browser: bool = False,
        site: str = "kimi",
    ) -> ChatGPTResult:
        """
        抓取回答。优先缓存，其次 LLM 直接回答，最后才用浏览器。
        site: kimi | chatgpt | perplexity | gemini
        """
        site = (site or "kimi").lower().strip()
        if site not in SITE_CONFIGS:
            site = "kimi"
        log.info("=" * 50)
        log.info("开始 scrape: prompt=%s, target_brand=%s, site=%s", prompt[:50] + "..." if len(prompt) > 50 else prompt, target_brand, site)

        # 1. 缓存命中且不需要浏览器时直接返回
        if not use_browser:
            cached = _load_cache(prompt, target_brand, site)
            if cached:
                return cached
            log.info("缓存未命中，先用 LLM 直接回答...")
            result = await self._fetch_via_llm(prompt, target_brand)
            _save_cache(result, target_brand, site)
            return result

        # 2. use_browser=True：启动浏览器抓取
        if os.environ.get("CDP_URL") or os.environ.get("SKIP_KILL_CHROME"):
            log.info("[Browser] CDP_URL/SKIP_KILL_CHROME set, skip killing Chrome")
        else:
            log.info("[Browser] Killing Chrome, then launching...")
            _kill_chrome_before_run()
        log.info("[Browser] Creating agent for site=%s...", site)
        return await self._scrape_via_browser(prompt, target_brand, max_wait, site)

    async def _scrape_via_browser(
        self, prompt: str, target_brand: str, max_wait: int, site: str = "kimi"
    ) -> ChatGPTResult:
        # CDP 预检：若 CDP_URL 已设，先确认可连通，避免 agent 启动后卡在 connect
        cdp_env = os.environ.get("CDP_URL", "").strip()
        if cdp_env and ("127.0.0.1" in cdp_env or "localhost" in cdp_env):
            parsed = urlparse(cdp_env)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or 9222
            ok = False
            try:
                data = await asyncio.get_event_loop().run_in_executor(None, lambda: _fetch_cdp_via_curl(host, port))
                ok = bool(data and data.get("webSocketDebuggerUrl"))
            except Exception:
                pass
            if not ok:
                try:
                    data = await _fetch_cdp_via_raw_tcp(host, port)
                    ok = bool(data and data.get("webSocketDebuggerUrl"))
                except Exception:
                    pass
            if not ok:
                try:
                    import httpx
                    async with httpx.AsyncClient(proxy=None, trust_env=False, timeout=10) as client:
                        r = await client.get(f"http://{host}:{port}/json/version")
                        ok = r.status_code == 200 and bool(r.json().get("webSocketDebuggerUrl"))
                except Exception:
                    pass
            if not ok:
                log.error(
                    "CDP 预检失败: 无法连接 %s:%s。请先运行 run_with_cdp.bat 或 start_chrome_cdp.bat 启动 Chrome。",
                    host, port,
                )
                raise RuntimeError(f"CDP unreachable at {host}:{port}. Run run_with_cdp.bat to start Chrome first.")

        cfg = SITE_CONFIGS.get(site, SITE_CONFIGS["kimi"])

        task_templates = {
            "kimi": """
            The page is open. Your FIRST action must be to input text. Do not wait or observe.

            Step 1: Use the 'input' action - find the input/textarea and type: {prompt}
            Step 2: Click the Send button to submit
            Step 3: Kimi streams its answer. Wait at least 30-45 seconds after clicking Send.
                    Use wait(30) or wait(45). The answer is complete when you see citation links at the bottom.
            Step 4: Use extract or read(source='page', extract_links=True) to get full content.
            Step 5: Call done() with JSON: {{"answer":"...","citations":[],"brand_mentioned":true/false,"brand_rank":number or null}}

            CRITICAL: Do extract at most 2 times. If after 2 attempts you still cannot find citation URLs, call done()
            immediately with citations=[] and the answer text you have. Do NOT retry extraction indefinitely.
            """,
            "chatgpt": """
            The page is open. You MUST enable Web search BEFORE typing, or there will be no citations.

            Step 0 (enable Web search - SKIP if already enabled):
            - FIRST check: Look for a "Search" pill/button in the composer footer (class __composer-pill, aria-label "Search, click to remove").
            - If the Search pill is ALREADY visible, do NOT click the plus button or Web search. Skip Step 0 entirely and go to Step 1.
            - If Search pill is NOT visible: Click the "+" (id=composer-plus-btn), then click "Web search" in the dropdown. Wait for the Search pill to appear.

            Step 1: Find the input/textarea at the bottom and type: {prompt}
            Step 2: Click the Send button to submit
            Step 3: Wait 30-45 seconds for the response. Use wait(30) or wait(45).
            Step 4: Use extract or read(source='page', extract_links=True) to get the full answer and citations.
            Step 5: Call done() with JSON: {{"answer":"...","citations":[],"brand_mentioned":true/false,"brand_rank":number or null}}

            CRITICAL: Do extract at most 2 times. If citations are not found after 2 attempts, call done() with citations=[].
            """,
            "perplexity": """
            The page is open. Your FIRST action must be to input the query.

            Step 1: Find the search/input box and type: {prompt}
            Step 2: Submit (click Ask or press Enter)
            Step 3: Wait 30-45 seconds for the answer and sources. Use wait(30) or wait(45).
            Step 4: Use extract or read(source='page', extract_links=True) to get the full answer and citation URLs.
            Step 5: Call done() with JSON: {{"answer":"...","citations":[],"brand_mentioned":true/false,"brand_rank":number or null}}

            CRITICAL: Do extract at most 2 times. If citations are not found after 2 attempts, call done() with citations=[].
            """,
            "gemini": """
            The page is open. Your FIRST action must be to input text.

            Step 1: Find the input area and type: {prompt}
            Step 2: Click Submit or Send
            Step 3: Wait 30-45 seconds for the response. Use wait(30) or wait(45).
            Step 4: Use extract or read(source='page', extract_links=True) to get the full answer and citations.
            Step 5: Call done() with JSON: {{"answer":"...","citations":[],"brand_mentioned":true/false,"brand_rank":number or null}}

            CRITICAL: Do extract at most 2 times. If citations are not found after 2 attempts, call done() with citations=[].
            """,
        }
        # ChatGPT / Gemini：亲测有效，提示词加 "give me all the links you searched" 能拿到更多 citations
        prompt_for_input = prompt + " Give me all the links you searched." if site in ("chatgpt", "gemini") else prompt
        task_desc = (task_templates.get(site, task_templates["kimi"])).format(
            prompt=prompt_for_input, target_brand=target_brand or ""
        )
        log.info("创建 Agent, site=%s, task 长度=%d 字符", site, len(task_desc))

        # initial_actions: CDP 模式下 run_with_cdp.bat 已按 SITE 打开对应 URL，无需 navigate
        cdp_url = os.environ.get("CDP_URL", "").strip()
        if cdp_url:
            initial = []
        else:
            initial = [
                {"navigate": {"url": cfg.url, "new_tab": False}},
                {"wait": {"seconds": 2}},
            ]

        agent = Agent(
            task=task_desc,
            llm=self.llm,
            browser=self.browser,
            initial_actions=initial,
        )

        max_steps = 15  # 限制步数，避免 extract 无限重试（task 已要求最多 2 次 extract）
        log.info("[Agent] Starting agent.run(max_steps=%d) for site=%s...", max_steps, site)
        t0 = datetime.now()
        result = await agent.run(max_steps=max_steps)
        log.info("[Agent] agent.run() finished in %.1fs", (datetime.now() - t0).total_seconds())

        # 保存 agent 步骤/LLM 历史到 runs 目录（与结果分开，供调试）
        cache_key = _cache_key(prompt, target_brand, site)
        run_data = {
            "prompt": prompt,
            "target_brand": target_brand,
            "site": site,
            "timestamp": datetime.now().isoformat(),
            "content": str(result),
            "final_result": result.final_result() if hasattr(result, "final_result") and result.final_result() else None,
        }
        _save_run_cache(site, prompt, target_brand, run_data)

        # Prefer final_result() from AgentHistoryList (done() text)
        content = None
        if hasattr(result, "final_result") and result.final_result():
            content = result.final_result()
            log.info("使用 result.final_result(), 长度=%d", len(content or ""))
        if not content:
            content = str(result)
        log.info("解析 result, content 前 200 字符: %s", (content or "")[:200].replace("\n", " "))

        res = None
        try:
            raw = content or ""
            data = None
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                json_match = re.search(r"\{.*\"answer\"\s*:.*\}", raw, re.DOTALL)
                if json_match:
                    try:
                        data = json.loads(json_match.group())
                    except json.JSONDecodeError:
                        pass
            if not data:
                # Agent 可能返回 Python dict（单引号），用 regex 提取 answer 和 citations
                try:
                    cites = []
                    for m in re.finditer(r"'citations'\s*:\s*\[(.*?)\]", raw, re.DOTALL):
                        cites.extend(re.findall(r"(https?://[^\s'\"\]>,]+)", m.group(1)))
                    cites = list(dict.fromkeys(c for c in cites if len(c) > 10))
                    if cites:
                        ans_match = re.search(r"'answer'\s*:\s*'((?:[^'\\]|\\.)*)'\s*,\s*'citations'", raw)
                        ans = ans_match.group(1).replace("\\n", "\n").replace("\\'", "'")[:50000] if ans_match else ""
                        if not ans:
                            ans_match = re.search(r"'answer'\s*:\s*'((?:[^'\\]|\\.)*)'", raw)
                            ans = ans_match.group(1).replace("\\n", "\n").replace("\\'", "'")[:50000] if ans_match else raw[:8000]
                        if len(ans) > 50:
                            data = {
                                "answer": ans,
                                "citations": cites,
                                "brand_mentioned": bool(re.search(r"'brand_mentioned'\s*:\s*True", raw)),
                                "brand_rank": 1 if re.search(r"'brand_rank'\s*:\s*1\b", raw) else None,
                            }
                except Exception:
                    pass
            if data and data.get("answer") and len(str(data.get("answer", ""))) > 50:
                citations = [u for u in data.get("citations", []) if u]  # 过滤 null/空
                domains = [urlparse(u).netloc for u in citations if u]
                res = ChatGPTResult(
                    prompt=prompt,
                    answer=data.get("answer", ""),
                    brand_mentioned=data.get("brand_mentioned", False),
                    brand_rank=data.get("brand_rank"),
                    citations=citations,
                    citation_domains=domains,
                )
                log.info("成功解析 Agent JSON: keys=%s", list(data.keys()))
        except Exception as e:
            log.debug("JSON 解析失败: %s", e)

        # Fallback: page extraction when agent JSON fails or empty
        if res is None or not res.answer or len(res.answer) < 50:
            log.info("[Fallback] 使用页面提取 (extract_clean_markdown)")
            try:
                session = self.browser  # already started by agent
                if session._dom_watchdog:
                    session._dom_watchdog.clear_cache()
                await asyncio.sleep(1)
                from browser_use.dom.markdown_extractor import extract_clean_markdown

                # 获取并保存完整 HTML（session 可能已 reset，失败时用直接 CDP fallback）
                page_html = await _get_page_html(session)
                if not page_html:
                    host, port = "127.0.0.1", 9222
                    cdp_env = os.environ.get("CDP_URL", "").strip()
                    if cdp_env and ("127.0.0.1" in cdp_env or "localhost" in cdp_env):
                        parsed = urlparse(cdp_env)
                        host, port = parsed.hostname or host, parsed.port or port
                    log.info("[Fallback] session HTML 为空，尝试直接 CDP 获取")
                    page_html = await _get_page_html_via_direct_cdp(
                        host=host, port=port, prefer_url=getattr(cfg, "url", "") or "",
                    )
                cache_key = _cache_key(prompt, target_brand, site)
                _save_html(site, cache_key, page_html)

                md_content, _ = await extract_clean_markdown(browser_session=session, extract_links=True)
                current_url = getattr(session, "cdp_url", "") or ""
                if cfg.extractor == "kimi":
                    answer, citations = _parse_answer_kimi(md_content, prompt)
                else:
                    answer, citations = _parse_answer_generic(md_content, prompt, current_url, html=page_html)
                domains = [urlparse(u).netloc for u in citations if u]
                brand_mentioned = (target_brand or "").lower() in (answer or "").lower()
                rank = None
                if brand_mentioned and target_brand:
                    for i, w in enumerate((answer or "").lower().split()):
                        if target_brand.lower() in w:
                            rank = i + 1
                            break
                res = ChatGPTResult(
                    prompt=prompt,
                    answer=answer or "",
                    brand_mentioned=brand_mentioned,
                    brand_rank=rank,
                    citations=citations,
                    citation_domains=domains,
                )
                log.info("Fallback 提取完成: answer=%d 字符, citations=%d", len(answer or ""), len(citations))
            except Exception as e:
                log.exception("Fallback 提取失败: %s", e)
                if res is None:
                    res = ChatGPTResult(
                        prompt=prompt,
                        answer=content or "",
                        brand_mentioned=(target_brand or "").lower() in (content or "").lower(),
                        brand_rank=None,
                        citations=[],
                        citation_domains=[],
                    )

        # 保存 HTML 并从 HTML 补充 citations（回答中可能有多处 links，Agent 或 fallback 可能只抓到部分）
        if res:
            page_html = ""
            try:
                if self._browser:
                    session = self.browser
                    page_html = await _get_page_html(session)
                if not page_html:
                    # session 可能已 reset，用直接 CDP 连接（run_with_cdp 固定 127.0.0.1:9222）
                    host, port = "127.0.0.1", 9222
                    cdp_env = os.environ.get("CDP_URL", "").strip()
                    if cdp_env:
                        parsed = urlparse(cdp_env)
                        host = parsed.hostname or host
                        port = parsed.port or port
                    log.info("尝试直接 CDP 获取 HTML (%s:%s)", host, port)
                    page_html = await _get_page_html_via_direct_cdp(
                        host=host, port=port,
                        prefer_url=getattr(cfg, "url", "") or "",
                    )
                if page_html:
                    cache_key = _cache_key(res.prompt, target_brand, site)
                    _save_html(site, cache_key, page_html)
                    # 从 HTML 补充 citations，合并更多 links
                    html_cites = _extract_citations_from_html(page_html, cfg.extractor)
                    merged = _merge_citations(res.citations, html_cites)
                    if len(merged) > len(res.citations):
                        res = ChatGPTResult(
                            prompt=res.prompt,
                            answer=res.answer,
                            brand_mentioned=res.brand_mentioned,
                            brand_rank=res.brand_rank,
                            citations=merged,
                            citation_domains=[urlparse(u).netloc for u in merged if u],
                        )
                        log.info("从 HTML 补充 citations: %d -> %d", len(res.citations), len(merged))
            except Exception as e:
                log.debug("保存 HTML / 补充 citations 失败: %s", e)

        _save_cache(res, target_brand, site)
        return res

    async def close(self):
        if self._browser:
            await self._browser.stop()


# 使用示例
async def main():
    log.info(">>> 启动 main()")
    site = os.environ.get("SITE", "kimi").strip().lower() or "kimi"
    if site not in SITE_CONFIGS:
        site = "kimi"

    LLM_CONFIG = {
        "api_key": os.environ.get("DASHSCOPE_API_KEY", ""),
        "model": "qwen-plus",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    }
    if not LLM_CONFIG["api_key"]:
        raise RuntimeError("Set the DASHSCOPE_API_KEY environment variable")

    scraper = ChatGPTScraper(
        api_key=LLM_CONFIG["api_key"],
        model=LLM_CONFIG["model"],
        base_url=LLM_CONFIG["base_url"],
    )

    prompt = "best GaN charger for MacBook Pro and iPhone travel"
    target_brand = "Anker"  # for brand_mentioned test

    try:
        log.info(">>> 调用 scraper.scrape(site=%s)", site)
        result = await scraper.scrape(
            prompt=prompt,
            target_brand=target_brand,
            use_browser=True,
            site=site,
        )

        log.info(">>> scrape 完成，输出结果")
        print("=" * 50)
        print(f"Site: {site}")
        print(f"Prompt: {result.prompt}")
        print(f"Brand mentioned: {result.brand_mentioned}")
        print(f"Brand rank: {result.brand_rank}")
        print(f"\nAnswer preview: {result.answer[:500]}...")
        print(f"\nCitations ({len(result.citations)}):")
        for url in result.citations:
            print(f"  - {url}")
        print(f"\nDomains: {result.citation_domains}")
        log.info(">>> 全部完成")

    finally:
        log.info("关闭 browser")
        await scraper.close()


async def run_test_extract(site: str = "kimi"):
    """单独测试 extract 步骤：连接 CDP，从当前页面提取 answer+citations，保存到 results"""
    log.info(">>> 测试 extract 步骤 (site=%s)", site)
    cfg = SITE_CONFIGS.get(site, SITE_CONFIGS["kimi"])
    cdp_url = os.environ.get("CDP_URL", "").strip()
    if not cdp_url:
        log.error("请设置 CDP_URL，例如: set CDP_URL=http://127.0.0.1:9222")
        return

    from browser_use import Browser, BrowserProfile
    browser = Browser(
        browser_profile=BrowserProfile(cdp_url=cdp_url),
        is_local=False,
    )
    try:
        await browser.start()
        session = browser  # Browser 是 BrowserSession 的别名
        if session._dom_watchdog:
            session._dom_watchdog.clear_cache()
        await asyncio.sleep(1)
        from browser_use.dom.markdown_extractor import extract_clean_markdown

        # 获取并保存完整 HTML
        page_html = await _get_page_html(session)
        _save_html(site, "test-extract", page_html)

        md_content, _ = await extract_clean_markdown(browser_session=session, extract_links=True)
        current_url = getattr(session, "cdp_url", "") or ""
        if cfg.extractor == "kimi":
            answer, citations = _parse_answer_kimi(md_content, "test prompt")
        else:
            answer, citations = _parse_answer_generic(md_content, "test prompt", current_url, html=page_html)
        domains = [urlparse(u).netloc for u in citations if u]
        res = ChatGPTResult(
            prompt="[test-extract]",
            answer=answer or "",
            brand_mentioned=False,
            brand_rank=None,
            citations=citations,
            citation_domains=domains,
        )
        path = CACHE_DIR / site / RESULTS_SUBDIR / "test-extract.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(res.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("已保存: %s", path)
        print(f"Answer 长度: {len(answer or '')}, Citations: {len(citations)}")
        for u in citations[:10]:
            print(f"  - {u}")
    finally:
        await browser.stop()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", default=None, help="kimi|chatgpt|perplexity|gemini (or set SITE env)")
    parser.add_argument("--test-extract", action="store_true", help="单独测试 extract 步骤（需 CDP_URL，页面已打开）")
    args = parser.parse_args()
    if args.site:
        os.environ["SITE"] = args.site
    if args.test_extract:
        site = os.environ.get("SITE", "kimi").strip().lower() or "kimi"
        asyncio.run(run_test_extract(site))
    else:
        asyncio.run(main())
