# -*- coding: utf-8 -*-
"""Build stable citation metadata for public API results."""
from __future__ import annotations

import html
import ipaddress
import re
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from cn_ai.network_parser import _maybe_fix_mojibake


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_title = False
        self.title_parts: List[str] = []
        self.og_title = ""
        self.site_name = ""
        self.icon = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {str(k).lower(): str(v or "") for k, v in attrs}
        if tag.lower() == "title":
            self.in_title = True
        elif tag.lower() == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content", "").strip()
            if key in ("og:title", "twitter:title") and content and not self.og_title:
                self.og_title = content
            elif key in ("og:site_name", "application-name") and content and not self.site_name:
                self.site_name = content
        elif tag.lower() == "link":
            rel = values.get("rel", "").lower()
            href = values.get("href", "").strip()
            if href and "icon" in rel and not self.icon:
                self.icon = href

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title and data.strip():
            self.title_parts.append(data.strip())

    def metadata(self, base_url: str) -> Dict[str, str]:
        title = self.og_title or " ".join(self.title_parts)
        return {
            "title": _maybe_fix_mojibake(_clean_text(title)),
            "icon": urljoin(base_url, self.icon) if self.icon else "",
            "site_name": _maybe_fix_mojibake(_clean_text(self.site_name)),
        }


def _clean_text(value: str, limit: int = 300) -> str:
    return " ".join(html.unescape(value or "").split())[:limit]


def _clean_title(value: str) -> str:
    title = _clean_text(value)
    return re.sub(r"^(?:undefined|null)\s*[-|–—:]\s*", "", title, flags=re.IGNORECASE)


def _still_garbled(value: str) -> bool:
    """修复失败后仍是 Latin-1 乱码：高位字符很多，且没有汉字。"""
    chars = [ch for ch in (value or "") if not ch.isspace()]
    if len(chars) < 4:
        return False
    high = sum(1 for ch in chars if ord(ch) >= 0x80)
    cjk = sum(1 for ch in chars if "\u4e00" <= ch <= "\u9fff")
    return high >= 4 and cjk == 0 and high * 2 >= len(chars)


def _usable_title(value: str) -> bool:
    title = _clean_title(_maybe_fix_mojibake(value))
    if not title or _still_garbled(title):
        return False
    # 平台引用卡常把序号（如 “- 1” 或 “[reference:2]”）误当成标题。
    compact = re.sub(r"[\s\-–—_|:：.,，。()\[\]]", "", title, flags=re.IGNORECASE)
    if re.fullmatch(r"(?:reference|ref)?\d+", compact, flags=re.IGNORECASE):
        return False
    return True


def _public_host(host: str) -> bool:
    if not host or host.lower() == "localhost":
        return False
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
    return bool(infos)


def _safe_public_url(raw: str) -> bool:
    try:
        parsed = urlparse(raw)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and not parsed.username and _public_host(parsed.hostname or "")


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _safe_public_url(newurl):
            raise ValueError("unsafe redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_LATIN_DEFAULT_CHARSETS = {
    "iso-8859-1",
    "latin1",
    "latin-1",
    "ascii",
    "us-ascii",
    "ansi_x3.4-1968",
}
_CHARSET_ALIASES = {
    "utf8": "utf-8",
    "gb2312": "gb18030",
    "gb_2312": "gb18030",
    "gbk": "gb18030",
    "gb18030": "gb18030",
    "x-gbk": "gb18030",
    "cp936": "gb18030",
    "chinese": "gb18030",
}


def _normalize_charset(value: str) -> str:
    name = (value or "").strip().strip("\"'").lower()
    name = name.split(";", 1)[0].strip()
    return _CHARSET_ALIASES.get(name, name)


def _meta_charset(raw: bytes) -> str:
    head = raw[:8192]
    for match in re.finditer(br"<meta\b[^>]*>", head, flags=re.IGNORECASE):
        found = re.search(
            br"charset\s*=\s*[\"']?\s*([A-Za-z0-9._-]+)",
            match.group(0),
            flags=re.IGNORECASE,
        )
        if found:
            return _normalize_charset(found.group(1).decode("ascii", "ignore"))
    return ""


def _decode_html(raw: bytes, header_charset: Optional[str]) -> str:
    """按 meta、响应头、UTF-8、GB18030 的顺序解码 HTML。

    很多中文站点不带 charset，或带一个不可靠的 ISO-8859-1。
    直接按该默认值解码会把标题变成 å/æ/ç 这类乱码。
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    meta = _meta_charset(raw)
    header = _normalize_charset(header_charset or "")
    if header in _LATIN_DEFAULT_CHARSETS:
        header = ""
    explicit = {item for item in (meta, header) if item}
    order: List[str] = []
    for enc in (meta, header, "utf-8", "gb18030"):
        if enc and enc not in order:
            order.append(enc)
    for enc in order:
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if enc == "gb18030" and enc not in explicit:
            head = text[:2000]
            cjk = sum(1 for ch in head if "\u4e00" <= ch <= "\u9fff")
            if cjk < 4:
                continue
        return text
    return raw.decode("latin-1")


def fetch_url_metadata(url: str, timeout: float = 4.0) -> Dict[str, str]:
    if not _safe_public_url(url):
        return {}
    opener = build_opener(ProxyHandler({}), _SafeRedirect())
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; GEOCitationBot/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with opener.open(req, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            if "html" not in content_type.lower():
                return {}
            raw = response.read(384 * 1024)
            charset = response.headers.get_content_charset()
            text = _decode_html(raw, charset)
            final_url = response.geturl() or url
    except Exception:
        icon = _fetch_site_favicon(url, timeout=min(timeout, 2.0))
        return {"icon": icon} if icon else {}
    parser = _MetadataParser()
    try:
        parser.feed(text)
    except Exception:
        icon = _fetch_site_favicon(url, timeout=min(timeout, 2.0))
        return {"icon": icon} if icon else {}
    metadata = parser.metadata(final_url)
    if not metadata["icon"]:
        metadata["icon"] = _fetch_site_favicon(url, timeout=min(timeout, 2.0))
    return metadata


def _fetch_site_favicon(url: str, timeout: float = 2.0) -> str:
    """Use a verified site favicon when a citation card has no icon."""
    parsed = urlparse(url)
    favicon_url = f"{parsed.scheme}://{parsed.netloc}/favicon.ico"
    if not _safe_public_url(favicon_url):
        return ""
    opener = build_opener(ProxyHandler({}), _SafeRedirect())
    request = Request(favicon_url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with opener.open(request, timeout=timeout) as response:
            data = response.read(32)
            final_url = response.geturl()
    except Exception:
        return ""
    is_image = (
        data.startswith((b"\x00\x00\x01\x00", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"\xff\xd8\xff"))
        or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
    )
    return final_url if is_image and _safe_public_url(final_url) else ""


def build_citation_details(
    urls: Iterable[str],
    dom_details: Optional[Iterable[Dict[str, Any]]] = None,
    fetcher: Callable[[str], Dict[str, str]] = fetch_url_metadata,
    max_fetch: int = 50,
) -> List[Dict[str, Any]]:
    ordered: List[str] = []
    seen = set()
    for raw in urls:
        url = str(raw or "").strip()
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        ordered.append(url)

    hints: Dict[str, Dict[str, Any]] = {}
    for item in dom_details or []:
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        current = hints.setdefault(url, {"url": url, "title": "", "icon": "", "site_name": ""})
        for field in ("title", "icon", "site_name"):
            candidate = str(item.get(field) or "").strip()
            if field in ("title", "site_name"):
                candidate = _maybe_fix_mojibake(candidate)
            if not candidate:
                continue
            if field == "title":
                if not _usable_title(str(current.get(field) or "")) and _usable_title(candidate):
                    current[field] = candidate
            elif not current.get(field):
                current[field] = candidate

    fetched: Dict[str, Dict[str, str]] = {}
    targets = [
        url
        for url in ordered
        if (
            not _usable_title(str((hints.get(url) or {}).get("title") or ""))
            or not str((hints.get(url) or {}).get("icon") or "").strip()
            or not str((hints.get(url) or {}).get("site_name") or "").strip()
        )
    ][:max_fetch]
    if targets:
        with ThreadPoolExecutor(max_workers=min(16, len(targets))) as pool:
            futures = {pool.submit(fetcher, url): url for url in targets}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    fetched[url] = future.result() or {}
                except Exception:
                    fetched[url] = {}

    out: List[Dict[str, Any]] = []
    for index, url in enumerate(ordered, start=1):
        hint = hints.get(url) or {}
        meta = fetched.get(url) or {}
        host = (urlparse(url).hostname or "").lower()
        site_name = _maybe_fix_mojibake(
            _clean_text(str(hint.get("site_name") or meta.get("site_name") or ""))
        )
        if not site_name or _still_garbled(site_name):
            site_name = host[4:] if host.startswith("www.") else host
        hint_title = _maybe_fix_mojibake(str(hint.get("title") or ""))
        meta_title = _maybe_fix_mojibake(str(meta.get("title") or ""))
        out.append(
            {
                "icon": str(hint.get("icon") or meta.get("icon") or ""),
                "index": index,
                "site_name": site_name,
                "title": (
                    _clean_title(hint_title)
                    if _usable_title(hint_title)
                    else _clean_title(meta_title)
                ),
                "url": url,
            }
        )
    return out
