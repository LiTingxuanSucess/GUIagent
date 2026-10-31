# -*- coding: utf-8 -*-
"""Short-lived Volcengine AgentKit sandbox for the Doubao webpage."""
from __future__ import annotations

import datetime
import fcntl
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from cn_ai.agentbay_doubao import AgentBayDoubaoError

GEO_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LOCK_PATH = GEO_DIR / ".scrape_cache" / "volcengine_doubao.lock"
SITE_COOKIE_SUFFIXES = {
    "deepseek": ("deepseek.com",),
    "doubao": ("doubao.com",),
    "yiyan": ("baidu.com", "bdstatic.com"),
    "yuanbao": ("tencent.com", "qq.com"),
    "qianwen": ("qianwen.com", "aliyun.com", "tongyi.com"),
    "kimi": ("kimi.com", "moonshot.cn"),
}
DEFAULT_TOOL_ID = "t-yewie1bmkgmwrumgrm5n"
DEFAULT_REGION = "cn-beijing"
API_VERSION = "2025-10-30"
SERVICE = "agentkit"


class VolcengineDoubaoError(AgentBayDoubaoError):
    """Sandbox API failures are not a Doubao login failure."""

    def __init__(
        self,
        message: str,
        error_code: str = "site_request_failed",
        retryable: bool = True,
    ):
        super().__init__(message, error_code=error_code, retryable=retryable)


def doubao_browser_backend(mode: Optional[str] = None) -> str:
    """Select the browser used for a webpage job.

    ``volcengine`` opens a Volcengine sandbox for every site. ``local`` stays
    on the persistent Chrome for Doubao's first try. Every other value keeps
    Doubao on local Chrome first and uses AgentBay only after that fails.
    Yuanbao and the other sites stay on local Chrome.
    """
    backend = (
        mode if mode is not None else os.getenv("GEO_DOUBAO_BACKEND", "")
    ).strip().lower()
    if backend in {"volcengine", "volc", "huoshan", "agentkit"}:
        return "volcengine"
    if backend in {"off", "local-only", "disabled", "none"}:
        return "local"
    return "agentbay"


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def sign_headers(
    *,
    access_key: str,
    secret_key: str,
    host: str,
    method: str,
    path: str,
    query: Dict[str, str],
    body: bytes,
    now: datetime.datetime,
    region: str,
) -> Dict[str, str]:
    """Volcengine HMAC-SHA256 signature for one OpenAPI request."""
    xdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = {
        "content-type": "application/json",
        "host": host,
        "x-content-sha256": payload_hash,
        "x-date": xdate,
    }
    canonical_query = "&".join(
        f"{quote(key, safe='-_.~')}={quote(query[key], safe='-_.~')}"
        for key in sorted(query)
    )
    signed_names = ";".join(sorted(headers))
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
    canonical = "\n".join(
        [
            method,
            path or "/",
            canonical_query,
            canonical_headers,
            signed_names,
            payload_hash,
        ]
    )
    scope = f"{datestamp}/{region}/{SERVICE}/request"
    string_to_sign = "\n".join(
        [
            "HMAC-SHA256",
            xdate,
            scope,
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        ]
    )
    signing_key = _hmac_sha256(
        _hmac_sha256(
            _hmac_sha256(_hmac_sha256(secret_key.encode("utf-8"), datestamp), region),
            SERVICE,
        ),
        "request",
    )
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        f"HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_names}, Signature={signature}"
    )
    return headers


def _hosts(region: str) -> list[str]:
    explicit = os.getenv("GEO_VOLCENGINE_API_HOST", "").strip()
    # open.volcengineapi.com is the host that publishes DeleteSession.
    # The regional host accepts CreateSession but rejects DeleteSession.
    hosts = [
        explicit,
        "open.volcengineapi.com",
        f"agentkit.{region}.volcengineapi.com",
        f"agentkit.{region}.volces.com",
    ]
    seen = []
    for host in hosts:
        if host and host not in seen:
            seen.append(host)
    return seen


def _api(
    action: str,
    body: Dict[str, Any],
    opener: Callable[..., Any] = urlopen,
    now: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    access_key = os.getenv("VOLCENGINE_ACCESS_KEY", "").strip()
    secret_key = os.getenv("VOLCENGINE_SECRET_KEY", "").strip()
    if not access_key or not secret_key:
        raise VolcengineDoubaoError("火山引擎访问密钥未配置")
    region = os.getenv("GEO_VOLCENGINE_REGION", DEFAULT_REGION).strip() or DEFAULT_REGION
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    query = {"Action": action, "Version": API_VERSION}
    stamped = now or datetime.datetime.utcnow()
    last_error = "火山引擎沙箱接口没有响应"
    for host in _hosts(region):
        headers = sign_headers(
            access_key=access_key,
            secret_key=secret_key,
            host=host,
            method="POST",
            path="/",
            query=query,
            body=payload,
            now=stamped,
            region=region,
        )
        url = "https://" + host + "/?" + "&".join(
            f"{quote(key, safe='-_.~')}={quote(query[key], safe='-_.~')}"
            for key in sorted(query)
        )
        request = Request(url, data=payload, method="POST", headers=headers)
        try:
            with opener(request, timeout=40) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            last_error = _api_error(raw) or f"HTTP {exc.code}"
            if exc.code in {404, 502, 503, 504} or _operation_missing(last_error):
                continue
            raise VolcengineDoubaoError(f"火山引擎沙箱{action}失败: {last_error}") from exc
        except URLError as exc:
            last_error = str(getattr(exc, "reason", exc))
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise VolcengineDoubaoError("火山引擎沙箱返回了无法解析的响应") from exc
        error = ((parsed.get("ResponseMetadata") or {}).get("Error") or {})
        if error:
            message = str(error.get("Message") or error.get("Code") or "unknown error")
            if _operation_missing(message):
                last_error = message
                continue
            raise VolcengineDoubaoError(f"火山引擎沙箱{action}失败: {message}")
        result = parsed.get("Result")
        if not isinstance(result, dict):
            raise VolcengineDoubaoError(f"火山引擎沙箱{action}没有返回结果")
        return result
    raise VolcengineDoubaoError(f"火山引擎沙箱{action}失败: {last_error}")


def _api_error(raw: str) -> str:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()[:180]
    error = ((parsed.get("ResponseMetadata") or {}).get("Error") or {})
    return str(error.get("Message") or error.get("Code") or "").strip()


def create_session(
    opener: Callable[..., Any] = urlopen,
    ttl_min: Optional[int] = None,
) -> Dict[str, str]:
    tool_id = os.getenv("GEO_VOLCENGINE_TOOL_ID", DEFAULT_TOOL_ID).strip() or DEFAULT_TOOL_ID
    if ttl_min is None:
        ttl = int(os.getenv("GEO_VOLCENGINE_SESSION_TTL_MIN", "10") or "10")
    else:
        ttl = ttl_min
    result = _api(
        "CreateSession",
        {"ToolId": tool_id, "Ttl": ttl, "TtlUnit": "minute"},
        opener=opener,
    )
    session_id = str(result.get("SessionId") or "")
    endpoint = str(result.get("Endpoint") or "")
    if not session_id or not endpoint:
        raise VolcengineDoubaoError("火山引擎沙箱会话缺少连接地址")
    return {"session_id": session_id, "endpoint": endpoint, "tool_id": tool_id}


def _operation_missing(message: str) -> bool:
    text = str(message or "").lower()
    return "could not find operation" in text or "invalidaction.notfound" in text


def delete_session(
    tool_id: str,
    session_id: str,
    opener: Callable[..., Any] = urlopen,
) -> None:
    """Release a sandbox. A cleanup failure must not fail the user's question."""
    if not tool_id or not session_id:
        return
    try:
        _api(
            "DeleteSession",
            {"ToolId": tool_id, "SessionId": session_id},
            opener=opener,
        )
    except Exception:
        return


def browser_cdp_url(endpoint: str, opener: Callable[..., Any] = urlopen) -> str:
    parts = urlsplit(endpoint.strip())
    if not parts.scheme or not parts.netloc:
        raise VolcengineDoubaoError("火山引擎沙箱连接地址无效")
    info_url = urlunsplit((parts.scheme, parts.netloc, "/v1/browser/info", parts.query, ""))
    deadline = time.monotonic() + 60
    last_error = "浏览器还没有就绪"
    while time.monotonic() < deadline:
        try:
            with opener(Request(info_url), timeout=15) as response:
                info = json.loads(response.read().decode("utf-8"))
            cdp = str(((info.get("data") or {}).get("cdp_url")) or "")
        except Exception as exc:
            last_error = str(exc)
            time.sleep(2)
            continue
        if not cdp or urlsplit(cdp).path.rstrip("/").endswith("/default"):
            # The sandbox returns this placeholder until Chromium is listening.
            time.sleep(2)
            continue
        if parts.query and "Authorization=" not in cdp:
            separator = "&" if "?" in cdp else "?"
            cdp = cdp + separator + parts.query
        return cdp
    raise VolcengineDoubaoError(f"火山引擎浏览器连接超时: {last_error}")


def cookie_matches(domain: str, suffixes: tuple) -> bool:
    host = str(domain or "").lstrip(".").lower()
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def normalize_store_cookie(cookie: dict) -> dict:
    """Convert a session-store cookie into the Playwright cookie shape."""
    item = {
        "name": cookie.get("name") or "",
        "value": cookie.get("value") or "",
        "domain": cookie.get("domain") or "",
        "path": cookie.get("path") or "/",
    }
    expires = cookie.get("expires")
    if isinstance(expires, (int, float)) and expires > 0:
        item["expires"] = expires
    if "httpOnly" in cookie:
        item["httpOnly"] = bool(cookie["httpOnly"])
    elif "http_only" in cookie:
        item["httpOnly"] = bool(cookie["http_only"])
    if "secure" in cookie:
        item["secure"] = bool(cookie["secure"])
    same_site = cookie.get("sameSite", cookie.get("same_site"))
    if same_site in {"Strict", "Lax", "None"}:
        item["sameSite"] = same_site
    return item


def profile_devtools_cdp(profile_dir: Path) -> str:
    """CDP address of a Chrome that is already open for this profile."""
    try:
        lines = (Path(profile_dir) / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    port = lines[0].strip() if lines else ""
    if not port.isdigit():
        return ""
    return "http://127.0.0.1:" + port


SITE_STORAGE_ORIGINS = {
    "deepseek": ("https://chat.deepseek.com",),
    "doubao": ("https://www.doubao.com",),
    "yiyan": ("https://yiyan.baidu.com", "https://wenxin.baidu.com"),
    "yuanbao": ("https://yuanbao.tencent.com",),
    "qianwen": ("https://www.qianwen.com", "https://tongyi.aliyun.com"),
    "kimi": ("https://www.kimi.com", "https://kimi.moonshot.cn"),
}


def _storage_pairs(items) -> dict:
    bag = {}
    for item in items if isinstance(items, list) else []:
        if (
            isinstance(item, (list, tuple))
            and len(item) >= 2
            and isinstance(item[0], str)
        ):
            bag[item[0]] = "" if item[1] is None else str(item[1])
    return bag


def _leveldb_origin_items(blob: bytes, origin: str) -> dict:
    """Read one origin out of a Chrome Local Storage LevelDB image.

    Keys are ``origin \\x00 \\x01 name`` followed by LevelDB's 8-byte tag.
    A leading 0x01 value is UTF-8; 0x00 is UTF-16LE. The bytes after that
    prefix are exactly what ``localStorage.getItem`` returns.
    """
    marker = origin.encode("utf-8") + b"\x00\x01"
    best = {}
    seqs = {}
    start = 0
    key_bytes = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")
    while True:
        found = blob.find(marker, start)
        if found < 0:
            break
        rest = blob[found + len(marker):]
        start = found + len(marker)
        name = bytearray()
        index = 0
        while index < len(rest) and index < 80 and rest[index] in key_bytes:
            name.append(rest[index])
            index += 1
        if not name or index >= len(rest):
            continue
        tail = rest[index:]
        seq = 0
        if len(tail) >= 8 and (int.from_bytes(tail[:8], "little") & 0xFF) == 1:
            seq = int.from_bytes(tail[:8], "little") >> 8
            tail = tail[8:]
        if not tail:
            continue
        kind = tail[0]
        raw = tail[1:8000]
        text = ""
        if kind == 1:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                text = raw[: exc.start].decode("utf-8", errors="ignore")
        elif kind == 0:
            data = raw[:4000]
            if len(data) % 2:
                data = data[:-1]
            try:
                text = data.decode("utf-16-le")
            except UnicodeDecodeError as exc:
                text = data[: exc.start].decode("utf-16-le", errors="ignore")
        if text.startswith("{") or text.startswith("["):
            try:
                _, end = json.JSONDecoder().raw_decode(text)
                text = text[:end]
            except json.JSONDecodeError:
                text = ""
        elif text.startswith('"'):
            try:
                parsed, _ = json.JSONDecoder().raw_decode(text)
                text = parsed if isinstance(parsed, str) else ""
            except json.JSONDecodeError:
                text = ""
        else:
            # Binary tails are not a storage string.
            text = text.split("\x00", 1)[0]
            if not text.isprintable():
                text = ""
        if not text:
            continue
        key = name.decode("ascii")
        if seq >= seqs.get(key, -1):
            seqs[key] = seq
            best[key] = text
    return best


def _profile_leveldb_storage(profile_dir: Path, origins: set) -> dict:
    directory = Path(profile_dir) / "Default" / "Local Storage" / "leveldb"
    if not directory.is_dir() or not origins:
        return {}
    blob = b""
    for path in directory.iterdir():
        if path.is_file() and path.suffix in {".ldb", ".log"}:
            try:
                blob += path.read_bytes()
            except OSError:
                continue
    selected = {}
    for origin in origins:
        items = _leveldb_origin_items(blob, origin)
        if items:
            selected[origin] = items
    return selected


def profile_site_storage(profile_dir: Path, site_id: str) -> dict:
    """Local storage for one site. Kimi keeps its login here, not in cookies."""
    path = Path(profile_dir) / ".session-store.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    raw = data.get("local_storage") if isinstance(data, dict) else None
    allowed = set(SITE_STORAGE_ORIGINS.get(site_id) or ())
    selected = {}
    for origin, items in (raw or {}).items() if isinstance(raw, dict) else []:
        if origin not in allowed:
            continue
        bag = _storage_pairs(items)
        if bag:
            selected[str(origin)] = bag
    for origin, items in _profile_leveldb_storage(profile_dir, allowed).items():
        bag = selected.setdefault(origin, {})
        for key, value in items.items():
            bag.setdefault(key, value)
    return selected


def profile_site_cookies(profile_dir: Path, site_id: str) -> list:
    """Read one account profile and keep that site's cookies."""
    path = Path(profile_dir) / ".session-store.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw = data.get("cookies") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    now = time.time()
    fresh = []
    for cookie in raw:
        if not isinstance(cookie, dict):
            continue
        expires = cookie.get("expires")
        if isinstance(expires, (int, float)) and 0 < expires < now:
            continue
        fresh.append(normalize_store_cookie(cookie))
    return cookies_for_site(fresh, site_id)


def cookies_for_site(cookies: list, site_id: str) -> list:
    """Keep the logged-in cookies that belong to one site."""
    suffixes = SITE_COOKIE_SUFFIXES.get(site_id)
    if not suffixes:
        return []
    selected = []
    for cookie in cookies:
        domain = str(cookie.get("domain") or "")
        name = str(cookie.get("name") or "")
        if not name or "value" not in cookie or not cookie_matches(domain, suffixes):
            continue
        item = {
            "name": name,
            "value": cookie.get("value") or "",
            "domain": domain,
            "path": cookie.get("path") or "/",
        }
        expires = cookie.get("expires")
        if isinstance(expires, (int, float)) and expires > 0:
            item["expires"] = expires
        if "httpOnly" in cookie:
            item["httpOnly"] = bool(cookie["httpOnly"])
        if "secure" in cookie:
            item["secure"] = bool(cookie["secure"])
        same_site = cookie.get("sameSite")
        if same_site in {"Strict", "Lax", "None"}:
            item["sameSite"] = same_site
        else:
            # The session snapshot omits SameSite. An omitted value is stored
            # as None, and Chrome drops None cookies that are not Secure.
            item["sameSite"] = "Lax"
        if item.get("sameSite") == "None" and not item.get("secure"):
            item["sameSite"] = "Lax"
        selected.append(item)
    return selected


def seed_origin_storage(playwright: Any, local_cdp: str, cloud_browser: Any, origin: str) -> int:
    """Copy one origin's localStorage into the sandbox. Do not close local Chrome."""
    if not local_cdp or local_cdp.startswith("http://127.0.0.1:9"):
        return 0
    local = playwright.chromium.connect_over_cdp(local_cdp)
    # Reuse the leased site's tab instead of navigating another renderer.
    source = next((page for page in local.contexts[0].pages
                   if urlsplit(page.url).netloc == urlsplit(origin).netloc), None)
    owns_source = source is None
    if owns_source:
        source = local.contexts[0].new_page()
    try:
        if owns_source:
            source.goto(origin, wait_until="domcontentloaded", timeout=20000)
        items = source.evaluate("() => Object.assign({}, localStorage)")
    finally:
        if owns_source:
            source.close()
    if not isinstance(items, dict) or not items:
        return 0
    context = cloud_browser.contexts[0] if cloud_browser.contexts else None
    if context is None:
        raise VolcengineDoubaoError("火山沙箱浏览器没有可用上下文")
    host = json.dumps(urlsplit(origin).netloc)
    payload = json.dumps({str(key): str(value) for key, value in items.items()}, ensure_ascii=False)
    # Run before the site's own scripts, including on the later scrape navigation.
    context.add_init_script(
        "(() => {"
        f"const items = {payload};"
        f"if (location.host !== {host}) return;"
        "for (const [key, value] of Object.entries(items)) {"
        "try { localStorage.setItem(key, value); } catch (e) {}"
        "}"
        "})();"
    )
    return len(items)


def seed_site_cookies(playwright: Any, local_cdp: str, cloud_browser: Any, site_id: str) -> int:
    """Copy the local account's site cookies into the fresh sandbox."""
    if not local_cdp or local_cdp.startswith("http://127.0.0.1:9"):
        raise VolcengineDoubaoError("火山沙箱缺少本地登录态来源")
    local = playwright.chromium.connect_over_cdp(local_cdp)
    try:
        cookies = []
        for context in local.contexts:
            cookies.extend(context.cookies())
    except Exception as exc:
        raise VolcengineDoubaoError("读取本地登录态失败") from exc
    selected = cookies_for_site(cookies, site_id)
    if not selected:
        raise VolcengineDoubaoError("本地浏览器里没有这个站点的登录态")
    context = cloud_browser.contexts[0] if cloud_browser.contexts else None
    if context is None:
        raise VolcengineDoubaoError("火山沙箱浏览器没有可用上下文")
    context.add_cookies(selected)
    return len(selected)


def warm_session_path() -> Path:
    explicit = os.getenv("GEO_VOLCENGINE_WARM", "").strip()
    if explicit:
        return Path(explicit)
    return GEO_DIR / ".scrape_cache" / "volcengine_doubao_warm.json"


def doubao_warm_ttl_min() -> int:
    """A reused Doubao sandbox has to outlive one batch of questions."""
    configured = int(os.getenv("GEO_VOLCENGINE_SESSION_TTL_MIN", "20") or "20")
    return max(20, min(configured, 60))


def _read_warm_record() -> Optional[Dict[str, Any]]:
    try:
        record = json.loads(warm_session_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


def _warm_record_usable(record: Dict[str, Any]) -> bool:
    created = float(record.get("created_at") or 0)
    ttl_min = int(record.get("ttl_min") or 0)
    if not record.get("session_id") or not record.get("cdp_url") or created <= 0 or ttl_min <= 0:
        return False
    # Leave enough time for the next answer before the sandbox expires.
    return time.time() < created + ttl_min * 60 - 90


def load_warm_session() -> Optional[Dict[str, Any]]:
    record = _read_warm_record()
    if record is None or not _warm_record_usable(record):
        return None
    return record


def release_expired_warm_session() -> None:
    """Delete a sandbox that can no longer serve another question."""
    record = _read_warm_record()
    if record is None or _warm_record_usable(record):
        return
    try:
        delete_session(str(record.get("tool_id") or ""), str(record.get("session_id") or ""))
    finally:
        clear_warm_session()


def save_warm_session(session_id: str, tool_id: str, cdp_url: str, ttl_min: int) -> None:
    path = warm_session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "tool_id": tool_id,
        "cdp_url": cdp_url,
        "created_at": time.time(),
        "ttl_min": ttl_min,
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


def clear_warm_session() -> None:
    try:
        warm_session_path().unlink()
    except OSError:
        pass


def merge_profile_cookies(profile_dir: Path, cookies: list) -> int:
    """Replace one profile's Doubao cookies with a session that just logged in."""
    path = Path(profile_dir) / ".session-store.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    existing = data.get("cookies") if isinstance(data.get("cookies"), list) else []
    kept = []
    for cookie in existing:
        if not isinstance(cookie, dict):
            continue
        if cookie_matches(str(cookie.get("domain") or ""), SITE_COOKIE_SUFFIXES["doubao"]):
            continue
        kept.append(cookie)
    for cookie in cookies:
        kept.append(
            {
                "name": cookie.get("name") or "",
                "value": cookie.get("value") or "",
                "domain": cookie.get("domain") or "",
                "path": cookie.get("path") or "/",
                "expires": cookie.get("expires") or 0,
                "http_only": bool(cookie.get("httpOnly")),
                "secure": bool(cookie.get("secure")),
                "same_site": cookie.get("sameSite") or "",
                "session": not cookie.get("expires"),
            }
        )
    data["cookies"] = kept
    data["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return len(cookies)


class VolcengineDoubaoLease:
    """Create one billed sandbox and delete it when the job finishes."""

    def __init__(self, opener: Callable[..., Any] = urlopen, ttl_min: Optional[int] = None) -> None:
        self.opener = opener
        self.ttl_min = ttl_min
        self.cdp_url = ""
        self.session_id = ""
        self.tool_id = ""
        self.keep = False
        self._lock = None

    def __enter__(self) -> "VolcengineDoubaoLease":
        path = Path(os.getenv("GEO_VOLCENGINE_LOCK", "").strip() or DEFAULT_LOCK_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._acquire_slot(path)
        try:
            if self.ttl_min is None:
                created = create_session(self.opener)
            else:
                created = create_session(self.opener, ttl_min=self.ttl_min)
            self.session_id = created["session_id"]
            self.tool_id = created["tool_id"]
            self.cdp_url = browser_cdp_url(created["endpoint"], self.opener)
            return self
        except Exception:
            self._delete()
            self._unlock()
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if not self.keep:
                self._delete()
        finally:
            self._unlock()

    def _acquire_slot(self, path: Path) -> None:
        # Independent sandboxes need bounded slots, not one lock held for
        # the whole answer. File locks keep the limit across processes.
        slots = max(1, min(6, int(os.getenv("GEO_VOLCENGINE_CONCURRENCY", "3"))))
        wait = max(0, min(300, float(os.getenv("GEO_VOLCENGINE_WAIT_SEC", "30"))))
        deadline = time.monotonic() + wait
        while True:
            for index in range(slots):
                candidate = Path(str(path) + f".slot-{index}").open("a+", encoding="utf-8")
                try:
                    fcntl.flock(candidate.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    candidate.close()
                    continue
                except Exception:
                    candidate.close()
                    raise
                self._lock = candidate
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise VolcengineDoubaoError("火山引擎沙箱并发槽位等待超时")
            time.sleep(min(0.1, remaining))

    def _delete(self) -> None:
        if not self.session_id:
            return
        try:
            delete_session(self.tool_id, self.session_id, self.opener)
        except Exception:
            pass
        self.session_id = ""

    def _unlock(self) -> None:
        if self._lock is None:
            return
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
        finally:
            self._lock.close()
            self._lock = None
