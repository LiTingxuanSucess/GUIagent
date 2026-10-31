# -*- coding: utf-8 -*-
"""从各平台网络响应中解析 answer + citations。"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse


def _uniq_urls(urls: List[str]) -> List[str]:
    seen = set()
    out = []
    for u in urls:
        u = (u or "").strip()
        if not u or not u.startswith("http") or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def clean_answer_citation_markers(answer: str) -> str:
    """移除平台内部引用占位符，引用数据由 citation_details 单独返回。"""
    answer = _maybe_fix_mojibake(answer or "")
    answer = re.sub(r"\[\]\(@mark_[^)]+\)", "", answer)
    answer = re.sub(r"<REF>.*?</REF>", "", answer, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(
        r"[\[【]\s*(?:citation|reference)\s*:\s*\d+(?:\s*[,，-]\s*\d+)*\s*[\]】]",
        "",
        answer,
        flags=re.IGNORECASE,
    )
    return re.sub(r"[ \t]+(?=\n)", "", cleaned).strip()


def parse_deepseek_share_url(body: str) -> str:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return ""

    candidates: List[Tuple[str, str]] = []

    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                walk(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
        elif isinstance(value, str) and value.strip():
            candidates.append((key.lower(), value.strip()))

    walk(payload)
    for _, value in candidates:
        if re.match(r"^https://chat\.deepseek\.com/share/[A-Za-z0-9_-]+", value):
            return value
    for key, value in candidates:
        if key in ("share_id", "shareid") and re.fullmatch(r"[A-Za-z0-9_-]{6,}", value):
            return "https://chat.deepseek.com/share/" + value
    return ""


_DETAIL_URL_KEYS = (
    "url", "raw_url", "rawUrl", "doc_url", "docUrl", "link", "href",
    "source_url", "sourceUrl", "original_url", "originalUrl", "jump_url", "jumpUrl",
)
_DETAIL_TITLE_KEYS = (
    "title", "page_title", "pageTitle", "display_title", "displayTitle",
    "name", "display_name", "displayName",
)
_DETAIL_SITE_KEYS = (
    "site_name", "siteName", "sitename", "source_name", "sourceName",
    "website_name", "websiteName", "site", "domain",
)
_DETAIL_ICON_KEYS = (
    "icon", "icon_url", "iconUrl", "favicon", "favicon_url", "faviconUrl",
    "logo", "logo_url", "logoUrl",
)


def _first_text(item: Dict[str, Any], keys: Tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for nested_key in ("url", "src", "value", "name", "title"):
                nested = value.get(nested_key)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    return ""


def _detail_from_dict(item: Dict[str, Any]) -> Optional[Dict[str, str]]:
    raw_url = _first_text(item, _DETAIL_URL_KEYS)
    if not raw_url.startswith(("http://", "https://")):
        return None
    icon = _first_text(item, _DETAIL_ICON_KEYS)
    if icon.startswith("//"):
        icon = "https:" + icon
    elif icon and not icon.startswith(("http://", "https://", "data:")):
        icon = urljoin(raw_url, icon)
    return {
        "url": raw_url,
        "title": _maybe_fix_mojibake(_first_text(item, _DETAIL_TITLE_KEYS)),
        "site_name": _maybe_fix_mojibake(_first_text(item, _DETAIL_SITE_KEYS)),
        "icon": icon if icon.startswith(("http://", "https://")) else "",
    }


def _walk_citation_details(obj: Any, out: List[Dict[str, str]], depth: int = 0) -> None:
    if depth > 16:
        return
    if isinstance(obj, dict):
        detail = _detail_from_dict(obj)
        if detail:
            out.append(detail)
        for value in obj.values():
            _walk_citation_details(value, out, depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            _walk_citation_details(value, out, depth + 1)


def extract_capture_citation_details(body: str) -> List[Dict[str, str]]:
    """从 JSON/SSE/connect 响应保留平台原始引用元数据。"""
    roots: List[Any] = []
    whole = _try_parse_whole_json(body)
    if whole is not None:
        roots.append(whole)
    else:
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line or line == "[DONE]" or not line.startswith(("{", "[")):
                continue
            try:
                roots.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        # Kimi connect+json 是多个相邻 JSON 对象，不一定按行分隔。
        if not roots and '"op"' in body:
            decoder = json.JSONDecoder()
            pos = 0
            while len(roots) < 1000:
                pos = body.find("{", pos)
                if pos < 0:
                    break
                try:
                    value, end = decoder.raw_decode(body, pos)
                except json.JSONDecodeError:
                    pos += 1
                    continue
                roots.append(value)
                pos = end

    details: List[Dict[str, str]] = []
    for root in roots:
        _walk_citation_details(root, details)
    return details


def merge_capture_citation_details(
    captures: List[Dict[str, Any]], site_id: str
) -> List[Dict[str, str]]:
    """合并平台原始引用详情，同 URL 的非空字段互补。"""
    ordered: List[Dict[str, str]] = []
    by_url: Dict[str, Dict[str, str]] = {}
    for capture in captures:
        body = capture.get("body") or ""
        if not body:
            continue
        for detail in extract_capture_citation_details(body):
            raw_url = detail["url"]
            if _is_noise_citation(raw_url, site_id):
                continue
            current = by_url.get(raw_url)
            if current is None:
                current = {"url": raw_url, "title": "", "site_name": "", "icon": ""}
                by_url[raw_url] = current
                ordered.append(current)
            for field in ("title", "site_name", "icon"):
                if not current[field] and detail.get(field):
                    current[field] = detail[field]
    return ordered


def parse_deepseek_sse(body: str) -> Tuple[str, List[str]]:
    """Replay typed fragments; only RESPONSE content is a public answer.

    THINK and RESPONSE both stream through fragments/-1/content. Its path
    alone cannot identify the answer. BATCH children use relative paths, and
    negative indices refer to the fragment list as it exists at that event.
    """
    state: Dict[str, Any] = {}
    cur_path = ""
    cur_operation = "SET"

    def apply(path: str, value: Any, operation: str) -> None:
        if operation == "BATCH" and isinstance(value, list):
            for patch in value:
                if not isinstance(patch, dict):
                    continue
                child = str(patch.get("p") or "")
                target = child.lstrip("/") if child.startswith(("/", "response/")) or child == "response" else "/".join(p for p in (path, child) if p)
                apply(target, patch.get("v"), patch.get("o") or "APPEND")
            return
        if not path:
            if isinstance(value, dict):
                state.update(value)
            return
        keys = path.strip("/").split("/")
        parent: Any = state
        for index, key in enumerate(keys[:-1]):
            if isinstance(parent, dict):
                if key not in parent:
                    parent[key] = [] if re.fullmatch(r"-?\d+", keys[index + 1]) else {}
                parent = parent[key]
            elif isinstance(parent, list) and re.fullmatch(r"-?\d+", key):
                slot = int(key)
                if not -len(parent) <= slot < len(parent):
                    return
                parent = parent[slot]
            else:
                return
        key: Any = keys[-1]
        if isinstance(parent, list):
            if not re.fullmatch(r"-?\d+", key):
                return
            key = int(key)
            if not -len(parent) <= key < len(parent):
                return
            previous = parent[key]
        elif isinstance(parent, dict):
            previous = parent.get(key)
        else:
            return
        if operation == "APPEND" and isinstance(previous, str) and isinstance(value, str):
            parent[key] = previous + value
        elif operation == "APPEND" and isinstance(previous, list) and isinstance(value, list):
            previous.extend(value)
        elif operation == "APPEND" and isinstance(previous, dict) and isinstance(value, dict):
            previous.update(value)
        else:
            parent[key] = value

    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or "v" not in obj:
            continue
        if obj.get("p"):
            cur_path = str(obj["p"]).lstrip("/")
        if obj.get("o"):
            cur_operation = str(obj["o"])
        apply(cur_path, obj.get("v"), cur_operation)

    response = state.get("response") or state
    if not isinstance(response, dict):
        return "", []
    answer_parts: List[str] = []
    citations: List[str] = []
    for fragment in response.get("fragments") or []:
        if not isinstance(fragment, dict):
            continue
        if fragment.get("type") == "RESPONSE":
            content = fragment.get("content")
            if isinstance(content, dict):
                content = content.get("content")
            if isinstance(content, str):
                answer_parts.append(content)
        if fragment.get("type") in ("SEARCH", "TOOL_SEARCH"):
            citations.extend(item["url"] for item in fragment.get("results") or []
                             if isinstance(item, dict) and isinstance(item.get("url"), str))
    return "".join(answer_parts).strip(), _uniq_urls(citations)


def _deepseek_msg_text(msg: dict) -> str:
    parts: List[str] = []
    for fr in msg.get("fragments") or []:
        if fr.get("type") not in ("RESPONSE", "REQUEST", None, ""):
            # USER 常见 type=REQUEST 或无 type；ASSISTANT 为 RESPONSE
            if fr.get("type") in ("SEARCH", "TOOL_SEARCH"):
                continue
        c = fr.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, dict) and isinstance(c.get("content"), str):
            parts.append(c["content"])
    if parts:
        return "".join(parts).strip()
    # 兼容其它字段
    for k in ("content", "text"):
        v = msg.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def parse_deepseek_history(body: str, prompt: str = "") -> Tuple[str, List[str]]:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return "", []
    if not isinstance(data, dict) or data.get("code") not in (0, None):
        return "", []
    inner = data.get("data") or {}
    messages = (
        inner.get("biz_data", {}).get("chat_messages")
        or inner.get("chat_messages")
        or []
    )
    if not messages:
        return "", []

    def extract_assistant(msg: dict) -> Tuple[str, List[str]]:
        answer_parts: List[str] = []
        citations: List[str] = []
        for fr in msg.get("fragments") or []:
            if fr.get("type") == "RESPONSE":
                c = fr.get("content")
                if isinstance(c, str):
                    answer_parts.append(c)
                elif isinstance(c, dict) and c.get("content"):
                    answer_parts.append(c["content"])
            if fr.get("type") in ("SEARCH", "TOOL_SEARCH"):
                for r in fr.get("results") or []:
                    if r.get("url"):
                        citations.append(r["url"])
        return "".join(answer_parts).strip(), _uniq_urls(citations)

    # 按 prompt 匹配 USER，取「最后一轮」匹配后的 ASSISTANT（避免多轮串题/旧答）
    if prompt:
        p = prompt.strip()
        matched: Optional[Tuple[str, List[str]]] = None
        for i, m in enumerate(messages):
            if (m.get("role") or "").upper() != "USER":
                continue
            ut = _deepseek_msg_text(m)
            if not ut:
                continue
            if ut.strip() == p or p in ut or ut in p:
                for j in range(i + 1, len(messages)):
                    if (messages[j].get("role") or "").upper() == "ASSISTANT":
                        matched = extract_assistant(messages[j])
                        break
        if matched and (matched[0] or matched[1]):
            return matched
        # The requested turn is not present yet. Returning the latest assistant
        # message here would silently attach an older answer to this prompt.
        return "", []

    assistants = [m for m in messages if (m.get("role") or "").upper() == "ASSISTANT"]
    if not assistants:
        return "", []
    return extract_assistant(assistants[-1])


_YUANBAO_REPLACE_RE = re.compile(r"\[\]\(@replace=([^)\s]+)\)")


def _add_yuanbao_replace(found: Dict[str, Dict[str, Any]], item: Any) -> None:
    if not isinstance(item, dict):
        return
    rid = str(item.get("id") or "").strip()
    medias = item.get("multimedias")
    if medias is None and isinstance(item.get("multimedia"), list):
        medias = item["multimedia"]
    if not rid or not isinstance(medias, list):
        return
    found[rid] = item


def collect_yuanbao_replaces(body: str) -> Dict[str, Dict[str, Any]]:
    """元宝 detail 的 extra.replaces，或 SSE 里的 type=replace。"""
    found: Dict[str, Dict[str, Any]] = {}
    if not body or "replace" not in body:
        return found
    try:
        whole = json.loads(body)
    except json.JSONDecodeError:
        whole = None
    if isinstance(whole, dict):
        convs = whole.get("convs") or (whole.get("data") or {}).get("convs") or []
        ai_convs = [c for c in convs if isinstance(c, dict) and c.get("speaker") == "ai"]
        speeches = []
        if ai_convs:
            speeches = ai_convs[-1].get("speechesV2") or ai_convs[-1].get("speeches") or []
        for speech in speeches:
            if not isinstance(speech, dict):
                continue
            extra = speech.get("extra") or {}
            for item in extra.get("replaces") or []:
                _add_yuanbao_replace(found, item)
        return found
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]" or "replace" not in raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "replace":
            nested = obj.get("replace")
            _add_yuanbao_replace(found, nested if isinstance(nested, dict) else obj)
        for item in obj.get("replaces") or []:
            _add_yuanbao_replace(found, item)
    return found


def _http_field(item: Dict[str, Any], keys: Tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value.strip()
    return ""


def render_yuanbao_images(
    answer: str, bodies: List[str]
) -> Tuple[str, List[Dict[str, str]]]:
    """把 [](@replace=media_*) 换成 Markdown 图片，并返回可单独传递的图片列表。"""
    replaces: Dict[str, Dict[str, Any]] = {}
    for body in bodies:
        for rid, item in collect_yuanbao_replaces(body).items():
            replaces.setdefault(rid, item)
    images: List[Dict[str, str]] = []
    seen = set()

    def replace(match: re.Match) -> str:
        item = replaces.get(match.group(1))
        if not item:
            return ""
        display = str(item.get("display") or "")
        chunks: List[str] = []
        for media in item.get("multimedias") or []:
            if not isinstance(media, dict):
                continue
            desc = _maybe_fix_mojibake(
                str(media.get("description") or media.get("title") or media.get("desc") or "")
            )
            desc = re.sub(r"\s+", " ", desc).strip()
            jump = _http_field(media, ("jumpUrl", "jump_url"))
            kind = str(media.get("type") or media.get("mediaType") or display).lower()
            image_url = _http_field(media, ("url", "previewUrl", "originUrl", "cover"))
            if "image" in kind or display == "image":
                if not image_url:
                    continue
                alt = (desc or "图片").replace("[", " ").replace("]", " ")
                markdown = f"![{alt}]({image_url})"
                if jump:
                    markdown = f"[{markdown}]({jump})"
                chunks.append(markdown)
                if image_url not in seen:
                    seen.add(image_url)
                    images.append({"description": desc, "url": image_url, "jump_url": jump})
                continue
            link = jump or image_url
            if link:
                label = (desc or "链接").replace("[", " ").replace("]", " ")
                chunks.append(f"[{label}]({link})")
        return "\n\n".join(chunks)

    rendered = _YUANBAO_REPLACE_RE.sub(replace, answer or "")
    rendered = re.sub(r"\n{3,}", "\n\n", rendered).strip()
    return rendered, images


_QIANWEN_CARD_RE = re.compile(r"\[\[([A-Za-z0-9_]+)\]\]|\[\(([A-Za-z0-9_]+)\)\]")


def _qianwen_cards(body: str) -> Dict[str, Dict[str, Any]]:
    """千问 SSE 快照里的 multi_load 卡片，key 是正文占位符里的 source_seq。"""
    found: Dict[str, Dict[str, Any]] = {}
    if not body or "multi_load" not in body:
        return found
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or "multi_load" not in raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        data = obj.get("data") if isinstance(obj, dict) else None
        if not isinstance(data, dict):
            continue
        for msg in data.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            for item in (msg.get("meta_data") or {}).get("multi_load") or []:
                if not isinstance(item, dict):
                    continue
                seq = str(item.get("source_seq") or "").strip()
                if seq:
                    found[seq] = item
    return found


def _qianwen_video_markdown(item: Dict[str, Any], images: List[Dict[str, str]], seen: set) -> str:
    content = item.get("content") if isinstance(item.get("content"), dict) else {}
    chunks: List[str] = []
    for video in content.get("list") or []:
        if not isinstance(video, dict):
            continue
        cover = _http_field(video, ("cover",))
        jump = _http_field(video, ("norm_url", "url"))
        title = re.sub(r"\s+", " ", str(video.get("title") or "")).strip()
        duration = str(video.get("duration") or "").strip()
        desc = title
        if duration and duration not in desc:
            desc = f"{title} {duration}".strip()
        if not cover and not jump:
            continue
        label = (desc or "视频").replace("[", " ").replace("]", " ")
        if cover:
            markdown = f"![{label}]({cover})"
            if jump:
                markdown = f"[{markdown}]({jump})"
            if cover not in seen:
                seen.add(cover)
                images.append({"description": desc, "url": cover, "jump_url": jump})
        else:
            markdown = f"[{label}]({jump})"
        chunks.append(markdown)
    return "\n\n".join(chunks)


def render_qianwen_media(answer: str, bodies: List[str]) -> Tuple[str, List[Dict[str, str]]]:
    """把 [[card]] / [(card)] 换成视频封面，并丢掉追问按钮占位符。"""
    cards: Dict[str, Dict[str, Any]] = {}
    for body in bodies:
        for seq, item in _qianwen_cards(body).items():
            cards.setdefault(seq, item)
    images: List[Dict[str, str]] = []
    seen: set = set()

    def replace(match: re.Match) -> str:
        seq = match.group(1) or match.group(2)
        item = cards.get(seq) or {}
        if item.get("type") == "video_note_list":
            return _qianwen_video_markdown(item, images, seen)
        return ""

    rendered = _QIANWEN_CARD_RE.sub(replace, answer or "")
    rendered = re.sub(r"\n{3,}", "\n\n", rendered).strip()
    return rendered, images


def parse_yuanbao_sse(body: str) -> Tuple[str, List[str]]:
    """元宝 SSE：只抽 docs/searchDocs.url 与文本增量，不做全树 walk。"""
    texts: List[str] = []
    citations: List[str] = []
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        for key in ("content", "text", "msg"):
            v = obj.get(key)
            if isinstance(v, str) and len(v) > 0:
                texts.append(v)
        for key in ("docs", "searchDocs"):
            for d in obj.get(key) or []:
                if isinstance(d, dict):
                    u = d.get("url") or d.get("docUrl") or d.get("link")
                    if u:
                        citations.append(u)
        # content 可能是 list[{type,msg/docs}]
        content = obj.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "searchGuid":
                    for d in block.get("docs") or []:
                        if isinstance(d, dict) and d.get("url"):
                            citations.append(d["url"])
                elif isinstance(block.get("msg"), str):
                    texts.append(block["msg"])
                elif isinstance(block.get("text"), str):
                    texts.append(block["text"])
    answer = "".join(texts).strip()
    # SSE 增量可能极碎，保留拼接
    return answer, _uniq_urls(citations)


def parse_yuanbao_detail(body: str) -> Tuple[str, List[str]]:
    """元宝 conversation/v1/detail：只取最后一条 AI speech 的正文 + searchGuid.docs。"""
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        return "", []
    convs = obj.get("convs") or (obj.get("data") or {}).get("convs") or []
    ai_convs = [c for c in convs if c.get("speaker") == "ai"]
    if not ai_convs:
        return "", []
    # 最后一条 AI
    conv = ai_convs[-1]
    texts: List[str] = []
    citations: List[str] = []
    for sp in conv.get("speechesV2") or conv.get("speeches") or []:
        for block in sp.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "searchGuid":
                for doc in block.get("docs") or []:
                    if isinstance(doc, dict) and doc.get("url"):
                        citations.append(doc["url"])
            elif btype in ("text", "markdown", None, ""):
                t = block.get("msg") or block.get("text")
                if isinstance(t, str) and len(t) > 20:
                    texts.append(t)
            else:
                t = block.get("msg") or block.get("text")
                if isinstance(t, str) and len(t) > 40 and "http" not in t[:30]:
                    texts.append(t)
    answer = max(texts, key=len) if texts else ""
    # 清元宝内部标记
    answer = re.sub(r"\[\]\(@mark_[^)]+\)", "", answer)
    answer = re.sub(r"\[citation:\d+\]", "", answer)
    return _maybe_fix_mojibake(answer.strip()), _uniq_urls(citations)


def parse_doubao_chain(body: str) -> Tuple[str, List[str]]:
    """豆包 /im/chain/single：助手 text_block + search_query_result_block.results[].text_card.url。"""
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        return "", []
    texts: List[str] = []
    citations: List[str] = []
    msgs = (
        obj.get("downlink_body", {}).get("pull_singe_chain_downlink_body", {}).get("messages")
        or obj.get("messages")
        or []
    )
    # 搜索卡片和正文可能拆在不同的助手消息里，引用要全部收下。
    assistants = [m for m in msgs if m.get("user_type") != 1]
    if not assistants:
        return "", []
    for msg in assistants:
        for block in msg.get("content_block") or []:
            content = block.get("content") or {}
            tb = content.get("text_block") or {}
            if isinstance(tb.get("text"), str) and len(tb["text"]) > 10:
                texts.append(tb["text"])
            sq = content.get("search_query_result_block") or {}
            for r in sq.get("results") or []:
                if not isinstance(r, dict):
                    continue
                tc = r.get("text_card") or r
                if isinstance(tc, dict):
                    u = tc.get("url") or tc.get("link")
                    if u and isinstance(u, str) and u.startswith("http"):
                        citations.append(u)
    # 取最长 text_block 作为正文（避免拼接搜索摘要）
    answer = max(texts, key=len) if texts else ""
    return answer.strip(), _uniq_urls(citations)


def parse_doubao_completion_sse(body: str) -> Tuple[str, List[str]]:
    """豆包 /chat/completion event-stream：CHUNK_DELTA 拼正文 + text_card.url / text_block。"""
    texts: List[str] = []
    delta_parts: List[str] = []
    citations: List[str] = []
    for block in body.split("\n\n"):
        event = ""
        data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            if line.startswith("data:"):
                data = line[5:].strip()
        if not data or data == "{}":
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        if event == "CHUNK_DELTA" and isinstance(obj.get("text"), str) and obj["text"]:
            delta_parts.append(obj["text"])
        # 定向找 text_card / text_block，避免全树 walk 吸 CDN
        stack = [obj]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                tc = cur.get("text_card")
                if isinstance(tc, dict) and isinstance(tc.get("url"), str):
                    citations.append(tc["url"])
                tb = cur.get("text_block")
                if isinstance(tb, dict) and isinstance(tb.get("text"), str) and len(tb["text"]) > 40:
                    texts.append(tb["text"])
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
    delta_answer = _maybe_fix_mojibake("".join(delta_parts).strip())
    block_answer = max((_maybe_fix_mojibake(t) for t in texts), key=len) if texts else ""
    answer = block_answer if len(block_answer) >= len(delta_answer) else delta_answer
    return answer.strip(), _uniq_urls(citations)


def _cjk_count(s: str) -> int:
    return sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")


def _maybe_fix_mojibake(s: str) -> str:
    """修复 UTF-8/GBK 被按 cp1252/latin-1 误解码的文本。

    元宝、豆包的正文和引用标题都会出现这种乱码。短标题（如「首页」）
    的 UTF-8 首字节不一定落在 å/æ/ç 上，所以按高位字符判断，而不是只认那几个字母。
    """
    if not s or len(s) < 3:
        return s
    cjk = _cjk_count(s)
    if cjk >= max(4, len(s) // 30):
        return s
    if not any(ord(ch) >= 0x80 for ch in s):
        return s
    raw = bytearray()
    for ch in s:
        o = ord(ch)
        if o < 256:
            raw.append(o)
            continue
        try:
            raw.extend(ch.encode("cp1252"))
        except Exception:
            continue
    blob = bytes(raw)
    try:
        fixed = blob.decode("utf-8")
    except UnicodeDecodeError:
        fixed = blob.decode("utf-8", errors="replace")
    fixed_cjk = _cjk_count(fixed)
    if fixed_cjk > cjk and "\ufffd" not in fixed[:200]:
        return fixed
    # UTF-8 对不上时，再试 GBK。只有原文几乎全是高位字符、还原后以汉字为主才接受，
    # 避免把正常的西欧文本误修。
    chars = [ch for ch in s if not ch.isspace()]
    high = sum(1 for ch in chars if ord(ch) >= 0x80)
    if not chars or high < 4 or high * 2 < len(chars):
        return s
    # GBK 被按 Latin-1 解开时，几乎总会夹着 U+0080–U+00BF（如 ¹ « ¸）。
    # 纯西欧字母（Å ä ö é）落在 U+00C0 以上，不能拿去当 GBK 解。
    if not any(0x80 <= b <= 0xBF for b in blob):
        return s
    try:
        gb = blob.decode("gb18030")
    except UnicodeDecodeError:
        return s
    gb_cjk = _cjk_count(gb)
    # 重复的高位字符也能被 GB18030 解成汉字（如 “àààà” → “噜噜”）。
    # 真实中文标题的字节更多样，还原结果也不会只有同一个字。
    if (
        "\ufffd" in gb[:200]
        or len(set(blob)) < 4
        or len(set(gb)) < 2
        or gb_cjk < 2
        or gb_cjk * 3 < len(gb)
        or gb_cjk <= cjk
    ):
        return s
    return gb


_KIMI_URL_NOISE = (
    "kimi.com",
    "kimi.ai",
    "moonshot",
    "volces",
    "kimi.link",
    "kimi.page",
    "icon-cache",
    "gstatic.com",
    "ok.kimi",
)


def _kimi_clean_url(u: str) -> Optional[str]:
    u = (u or "").strip().rstrip("\\").rstrip(".,);]")
    if not u.startswith("http"):
        return None
    if any(x in u for x in _KIMI_URL_NOISE):
        return None
    # 丢弃明显截断/残缺
    host = urlparse(u).netloc
    if not host or "." not in host or host.endswith("."):
        return None
    return u


def _kimi_walk_urls(obj: Any, out: List[str]) -> None:
    """从 searchChunks / references / searchResult 结构抽 base.url。"""
    if isinstance(obj, dict):
        # 优先结构化字段，避免把 iconUrl/coverUrl 当引用
        base = obj.get("base")
        if isinstance(base, dict):
            u = _kimi_clean_url(str(base.get("url") or ""))
            if u:
                out.append(u)
        for key in ("url", "link", "href"):
            if key in obj and key != "iconUrl" and key != "coverUrl":
                # 仅当不在 icon/cover 上下文；base.url 已处理
                if "base" not in obj:
                    u = _kimi_clean_url(str(obj.get(key) or ""))
                    if u:
                        out.append(u)
        for v in obj.values():
            _kimi_walk_urls(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _kimi_walk_urls(v, out)


def parse_kimi_connect(body: str) -> Tuple[str, List[str]]:
    """Kimi application/connect+json：正文 + searchChunks/references 引用。"""
    answer_parts: List[str] = []
    citations: List[str] = []
    idxs = [m.start() for m in re.finditer(r'\{\s*"', body)]
    decoder = json.JSONDecoder()
    consumed = 0
    for start in idxs:
        if start < consumed:
            continue
        try:
            obj, end = decoder.raw_decode(body, start)
        except json.JSONDecodeError:
            continue
        consumed = end
        if not isinstance(obj, dict):
            continue
        mask = obj.get("mask") or ""
        if obj.get("op") == "append" and mask == "block.text.content":
            c = ((obj.get("block") or {}).get("text") or {}).get("content")
            if isinstance(c, str):
                answer_parts.append(c)
        if any(
            k in mask
            for k in (
                "searchChunks",
                "references",
                "searchResult",
                "block.tool",
                "refs",
            )
        ):
            _kimi_walk_urls(obj, citations)

    # 兜底：正文旁白里的完整 "url":"https://..."（排除 icon/cover）
    if not citations:
        for m in re.finditer(r'"url"\s*:\s*"(https://[^"]+)"', body):
            # 跳过 iconUrl/coverUrl 键（正则已限定键名为 url）
            u = _kimi_clean_url(m.group(1))
            if u:
                citations.append(u)

    return "".join(answer_parts).strip(), _uniq_urls(citations)


def parse_qianwen_chat_sse(body: str) -> Tuple[str, List[str]]:
    """Read both full snapshots and Qianwen's append/replace content patches."""
    best_answer = ""
    current_content: Dict[str, str] = {}
    citations: List[str] = []
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        data = obj.get("data") or {}
        for msg in data.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            channel = str(msg.get("mime_type") or "default")
            content_patched = False
            if isinstance(content, str):
                current_content[channel] = content
            for patch in msg.get("patch") or []:
                if not isinstance(patch, dict) or patch.get("path") != "/content":
                    continue
                value = patch.get("value")
                if not isinstance(value, str):
                    continue
                if patch.get("op") == "add":
                    current_content[channel] = current_content.get(channel, "") + value
                    content_patched = True
                elif patch.get("op") == "replace":
                    current_content[channel] = value
                    content_patched = True
            if msg.get("patch"):
                content = current_content.get(channel, "")
            if content_patched and channel in ("multi_load/iframe", "text/plain", "text/markdown"):
                best_answer = content
            elif isinstance(content, str) and len(content) > len(best_answer):
                best_answer = content
            elif isinstance(content, list):
                parts = []
                for part in content:
                    if isinstance(part, dict):
                        t = part.get("text") or part.get("content") or part.get("value")
                        if isinstance(t, str):
                            parts.append(t)
                    elif isinstance(part, str):
                        parts.append(part)
                joined = "".join(parts)
                if len(joined) > len(best_answer):
                    best_answer = joined
            meta = msg.get("meta_data") or {}
            # Current Qianwen emits search documents in the workflow stream,
            # separately from inline citation cards in the final answer.
            for block in meta.get("multi_load") or []:
                if not isinstance(block, dict) or block.get("type") != "bar_ref_source_inline":
                    continue
                search = block.get("content")
                if not isinstance(search, dict):
                    continue
                for doc in search.get("docs") or []:
                    if isinstance(doc, dict) and doc.get("type") in ("web_source", "web", "search", None, ""):
                        url = doc.get("raw_url") or doc.get("url")
                        if isinstance(url, str) and url.startswith(("http://", "https://")):
                            citations.append(url)
            for item in meta.get("list") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("type") in ("web_source", "web", "search", None, ""):
                    u = item.get("url") or item.get("raw_url")
                    if u and str(u).startswith("http"):
                        citations.append(u)
            for rk in ("references", "reference", "search_results", "sources"):
                for ref in msg.get(rk) or []:
                    if isinstance(ref, dict) and ref.get("url"):
                        citations.append(ref["url"])
    return best_answer.strip(), _uniq_urls(citations)


def parse_yiyan_search(body: str) -> Tuple[str, List[str]]:
    """文心 searchresult：仅 data.results[].url 作引用（无完整 AI 正文）。"""
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        return "", []
    citations = []
    for r in (obj.get("data") or {}).get("results") or []:
        if isinstance(r, dict) and isinstance(r.get("url"), str):
            citations.append(r["url"])
    return "", _uniq_urls(citations)


def parse_yiyan_task_sources(body: str, prompt: str = "") -> Tuple[str, List[str]]:
    """Read only referenceList from a task response tied to this question.

    Task progress and reasoning are not final answers. Wenxin's canvas JSON
    and task-mode SSE contain nested source lists the history parser misses.
    """
    if not prompt:
        return "", []
    whole = _try_parse_whole_json(body)
    roots = [whole] if isinstance(whole, dict) else []
    if not roots:
        for line in body.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                value = json.loads(line[5:].strip())
                if isinstance(value, dict):roots.append(value)
            except (ValueError, TypeError):
                continue
    citations: List[str] = []
    matches = False
    expected = re.sub(r"\s+", " ", prompt).strip()
    if not expected:
        return "", []
    def references(value, depth=0):
        if depth > 16:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "referenceList" and isinstance(child, list):
                    for ref in child:
                        if isinstance(ref, dict):
                            url = ref.get("url") or ref.get("link")
                            if isinstance(url, str) and url.startswith(("http://", "https://")):
                                citations.append(url)
                else:
                    references(child, depth+1)
        elif isinstance(value, list):
            for child in value:references(child, depth+1)
    for root in roots:
        data = root.get("data") if isinstance(root.get("data"), dict) else {}
        query = root.get("query", data.get("query"))
        if query is not None:
            text = query if isinstance(query, str) else ""
            if isinstance(query, list):
                parts = []
                for part in query:
                    data = part.get("data") if isinstance(part, dict) else None
                    item = data.get("text") if isinstance(data, dict) else None
                    value = item.get("query") if isinstance(item, dict) else None
                    if isinstance(value, str):parts.append(value)
                text = "".join(parts)
            matches = re.sub(r"\s+", " ", text).strip() == expected
        if matches:
            references(root)
    return "", _uniq_urls(citations)


def parse_yiyan_history(body: str, prompt: str = "") -> Tuple[str, List[str]]:
    """文心 history/list：优先匹配 prompt 的会话，取 generator.value + referenceList.url。"""
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        return "", []
    hd = (obj.get("data") or {}).get("historyData") or []
    candidates = []
    for item in hd:
        msgs = item.get("msgList") or []
        if not msgs:
            continue
        msg = msgs[-1]
        qraw = msg.get("query")
        qtext = ""
        if isinstance(qraw, list):
            for part in qraw:
                try:
                    qtext += (part.get("data") or {}).get("text", {}).get("query") or ""
                except Exception:
                    pass
        elif isinstance(qraw, str):
            qtext = qraw
        answer_parts: List[str] = []
        cites: List[str] = []
        for cr in msg.get("chatResponse") or []:
            content = ((cr.get("data") or cr).get("message") or {}).get("content") or {}
            for g in content.get("generator") or []:
                gd = g.get("data") or {}
                val = gd.get("value") or gd.get("text") or ""
                if isinstance(val, str) and val.strip():
                    answer_parts.append(val)
                for ref in gd.get("referenceList") or []:
                    if isinstance(ref, dict):
                        u = ref.get("url") or ref.get("link")
                        if u:
                            cites.append(u)
        # 多段 generator 按序拼接（取最长单段会截断开头/中间）
        answer = "".join(answer_parts)
        if not answer and answer_parts:
            answer = max(answer_parts, key=len)
        if not answer and not cites:
            continue
        raw = (answer or "").strip()
        if raw.startswith("需要重新规划检索") or '"rewrite_query"' in raw[:400]:
            continue
        if (
            "大概需要3-5min" in raw
            or "生成过程中你可以继续" in raw
            or ("收到你的需求" in raw and "联网搜索" in raw)
        ):
            continue
        if prompt:
            p = prompt.strip()
            if p not in (qtext or "") and (qtext or "").strip() != p:
                continue
        score = 0
        if prompt and prompt in qtext:
            score += 100
        if prompt and qtext.strip() == prompt.strip():
            score += 50
        score += min(len(answer), 5000) // 100
        candidates.append((score, answer, cites, qtext))
    if not candidates:
        return "", []
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, best_answer, best_cites, _ = candidates[0]
    return best_answer.strip(), _uniq_urls(best_cites)


def parse_generic_json(body: str) -> Tuple[str, List[str]]:
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        # 切勿把原始二进制/SSE 当 answer
        return "", []
    texts, links = _walk_json_for_text_links(obj)
    return "\n".join(texts).strip(), _uniq_urls(links)


def _try_parse_whole_json(body: str) -> Any:
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _find_key_recursive(obj: Any, key: str) -> List[Any]:
    found = []
    if isinstance(obj, dict):
        if key in obj:
            found.append(obj[key])
        for v in obj.values():
            found.extend(_find_key_recursive(v, key))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_find_key_recursive(item, key))
    return found


def _walk_json_for_text_links(obj: Any, depth: int = 0) -> Tuple[List[str], List[str]]:
    texts: List[str] = []
    links: List[str] = []
    if depth > 12:
        return texts, links
    if isinstance(obj, str):
        if obj.startswith("http"):
            links.append(obj)
        elif len(obj) > 80 and not obj.startswith("{") and not obj.startswith("["):
            texts.append(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            lk = k.lower()
            if lk in ("url", "link", "href", "docurl", "source_url") and isinstance(v, str) and v.startswith("http"):
                links.append(v)
            elif lk in ("content", "text", "answer", "markdown", "summary", "display_text") and isinstance(v, str) and len(v) > 20:
                texts.append(v)
            else:
                t, l = _walk_json_for_text_links(v, depth + 1)
                texts.extend(t)
                links.extend(l)
    elif isinstance(obj, list):
        for item in obj:
            t, l = _walk_json_for_text_links(item, depth + 1)
            texts.extend(t)
            links.extend(l)
    return texts, links


def parse_capture(
    site_id: str, url: str, body: str, mime: str, prompt: str = ""
) -> Tuple[str, List[str]]:
    """按 URL 选择解析器。"""
    u = url.lower()
    if "deepseek.com" in u and "completion" in u:
        return parse_deepseek_sse(body)
    if "deepseek.com" in u and "history_messages" in u:
        return parse_deepseek_history(body, prompt=prompt)
    if "yuanbao.tencent.com" in u and "/api/chat/" in u:
        return parse_yuanbao_sse(body)
    if "yuanbao.tencent.com" in u and "conversation" in u and ("detail" in u or "v1/detail" in u):
        return parse_yuanbao_detail(body)
    if "doubao.com" in u and "/im/chain/single" in u:
        return parse_doubao_chain(body)
    if "doubao.com" in u and "/chat/completion" in u:
        return parse_doubao_completion_sse(body)
    # 豆包其它 IM JSON 勿走通用 walk（会吸入 byteimg CDN）
    if "doubao.com" in u and "/im/" in u:
        return "", []
    if "chat.baidu.com" in u and "searchresult" in u:
        return parse_yiyan_search(body)
    if "chat.baidu.com" in u and "history/list" in u:
        return parse_yiyan_history(body, prompt=prompt)
    if "chat.baidu.com" in u and any(path in u for path in ("/chat/api/agent/task_mode", "/aichat/api/canvas/search")):
        return parse_yiyan_task_sources(body, prompt=prompt)
    # 文心其它接口（aitabserver/sidebar/sug）噪声大，忽略
    if "chat.baidu.com" in u or "wenxin.baidu.com" in u or "yiyan.baidu.com" in u:
        return "", []
    if "kimi.com" in u and "apiv2" in u:
        # Feed / ListMessages / GetChat 易混入历史会话，只信 Chat 流
        if any(x in url for x in ("ListFeeds", "ListMessages", "GetChat", "FeedService")):
            return "", []
        if "ChatService/Chat" in url or (
            ("connect+json" in mime or '"op":' in body[:5000])
            and "ChatService" in url
            and url.rstrip("/").endswith("/Chat")
        ):
            return parse_kimi_connect(body)
        return "", []
    if "qianwen.com" in u and "/api/v2/chat" in u:
        return parse_qianwen_chat_sse(body)
    # 千问 cms/配置类噪声极大，忽略
    if "qianwen.com" in u or "tongyi.aliyun.com" in u:
        return "", []
    # 元宝 / Kimi 其它 API 勿 walk
    if "yuanbao.tencent.com" in u or site_id == "kimi":
        return "", []
    return "", []


def merge_captures(
    captures: List[Dict[str, Any]], site_id: str, prompt: str = ""
) -> Tuple[str, List[str], str]:
    """合并多次捕获，返回 (answer, citations, source)。"""
    best_answer = ""
    all_citations: List[str] = []
    source = "dom"
    best_source = "dom"
    chain_citations: List[str] = []
    for cap in captures:
        url = cap.get("url", "") or ""
        if _is_static_asset(url):
            continue
        body = cap.get("body", "") or ""
        mime = cap.get("mime", "") or ""
        if not body:
            continue
        a, c = parse_capture(site_id, url, body, mime, prompt=prompt)
        clean_c = [u for u in c if not _is_noise_citation(u, site_id)]
        if site_id == "doubao" and "/im/chain/single" in url and clean_c:
            chain_citations.extend(clean_c)
        elif not (site_id == "doubao" and chain_citations):
            # 豆包若已有 chain 引用，不再合并 completion 避免翻倍
            all_citations.extend(clean_c)
        # Kimi：Chat 流优先于其它（即便更短）
        kimi_chat = site_id == "kimi" and url.rstrip("/").endswith("/Chat")
        prefer = False
        if kimi_chat and a:
            prefer = True
        elif len(a) > len(best_answer):
            if site_id == "doubao" and "/im/chain/single" not in url and best_source.endswith("chain/single"):
                prefer = False
            elif site_id == "kimi" and best_source.endswith("/Chat"):
                prefer = False
            else:
                prefer = True
        if prefer:
            best_answer = a
            best_source = f"network:{urlparse(url).path}"
    if chain_citations:
        all_citations = chain_citations + all_citations
    if best_answer:
        source = best_source
    return best_answer, _uniq_urls(all_citations), source


def _is_noise_citation(url: str, site_id: str = "") -> bool:
    u = (url or "").lower()
    noise = (
        "byteimg.com", "bytednsdoc.com", "byteacctimg.com", "douyinvod.com",
        "douyinpic.com", "lf-flow", "volces.com", "fengkongcloud",
        "aisearch.cdn.bcebos.com", "aichat-cdn.cdn.bcebos.com", "psstatic.cdn.bcebos.com",
        "gips0.baidu.com", "gips1.baidu.com", "gips2.baidu.com", "gips3.baidu.com",
        "t7.baidu.com", "t8.baidu.com", "t9.baidu.com", "ss0.baidu.com", "ss1.baidu.com", "ss2.baidu.com",
        "cdn-yb.icon.qq.com", "cdn-hybrid-prod.hunyuan.tencent.com",
        "account.tencent.com", "privacy.qq.com", "rule.tencent.com", "kf.qq.com",
        "hy-openapi-pulbic.hunyuan.tencent.com", "hy-openapi-public",
        "cms-sdk-server.qianwen.com", "img.alicdn.com", "terms.alicdn.com",
        "p.qianwen.com", "pdds.qianwen.com", "act.qianwen.com",
        "kimi.ai", "kimi.com", "kimi.link", "kimi.page", "moonshot.cn",
        "icon-cache",
        "/favicon",
    )
    if any(x in u for x in noise):
        return True
    if u.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js", ".woff", ".woff2")):
        return True
    if site_id == "doubao" and ("doubao.com" in u and "/chat/" not in u):
        # 站内导航不算信源
        if "share" not in u and "article" not in u:
            return "doubao.com/" in u and "http" in u
    return False


def _is_static_asset(url: str) -> bool:
    u = url.lower()
    if any(u.endswith(ext) for ext in (".js", ".css", ".woff", ".woff2", ".png", ".jpg", ".svg")):
        return True
    if "static/js" in u or "static/css" in u:
        return True
    return False
