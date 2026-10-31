"""
抖音企业号客服智能回复 Agent
==============================
原理：
  1. 通过 CDP 连接已登录的 Chrome（保留 Cookie / Session，无需重新登录）
  2. 拦截 imapi3-normal.zijieapi.com 的消息拉取响应，解析出会话消息列表
  3. 同时监听 bff/cs/server-realtime 的长轮询响应，感知新消息通知
  4. 当检测到最新一条消息来自「访客」时，调用 Qwen 生成回复
  5. 通过 Playwright DOM 操作（fill + Enter）把回复填入输入框并发送

用法：
  1. 先运行 start_chrome.bat 启动 Chrome（会保留你的登录态）
  2. 在抖音企业号后台打开会话中心页面
  3. 运行本脚本：py -3.11 douyin_watch.py
  4. Ctrl+C 退出

依赖：playwright, pyyaml, requests（见 requirements.txt）
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import re

import yaml
from playwright.async_api import async_playwright, Page, Response

# 确保 src/ 可 import
sys.path.insert(0, str(Path(__file__).parent))
from src.qwen_text import QwenTextClient
from src.platform_reporter import PlatformReporter

_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

log = logging.getLogger("douyin_agent")

# ── 常量 ──────────────────────────────────────────────────────────────
CDP_URL = "http://localhost:9222"
SESSION_URL = "https://leads.cluerich.com/pc/cs/chat/session"

# IM API 域名前缀（消息拉取），多个 host 均使用此拦截
# leads.cluerich.com 平台：
#   clue-ai/bff/chat/conversation-detail  - 消息历史列表（主要消息来源）
#   bff/cs/server-realtime                - 长轮询新消息通知
IM_API_KEYWORDS = (
    "imapi3-normal.zijieapi.com",
    "imapi-oth.zijieapi.com",
    "leads.cluerich.com/clue-ai/bff/chat/",
    "leads.cluerich.com/bff/cs/server-realtime",
)

# 输入框 placeholder（稳定，不依赖 CSS 类名）
INPUT_PLACEHOLDER = "回复内容，可按Enter键发送，按Alt+Enter换行"


# ── 配置加载 ──────────────────────────────────────────────────────────
def load_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"未找到配置文件: {path}，请先复制 config.example.yaml")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ── 消息解析 ──────────────────────────────────────────────────────────
def parse_messages(body: dict) -> list[dict]:
    """
    从 imapi 响应体中提取消息列表。
    字段路径因接口版本不同，做多层 fallback。
    返回 list of {msg_id, sender_uid, content_text, create_time}
    """
    candidates: list[dict] = []

    # v1: data.message_list  /  v2: messages.message_list  /  直接 message_list
    for root_key in ("data", "messages", ""):
        sub = body.get(root_key, body) if root_key else body
        if isinstance(sub, dict):
            ml = sub.get("message_list") or sub.get("msg_list") or []
            if ml:
                candidates = ml
                break

    result = []
    for m in candidates:
        if not isinstance(m, dict):
            continue
        # 消息内容：有的是 JSON 字符串，有的是直接 dict
        content_raw = m.get("content") or m.get("msg_content") or ""
        if isinstance(content_raw, str):
            try:
                content_obj = json.loads(content_raw)
                text = content_obj.get("text") or content_obj.get("content") or ""
            except Exception:
                text = content_raw
        elif isinstance(content_raw, dict):
            text = content_raw.get("text") or content_raw.get("content") or str(content_raw)
        else:
            text = str(content_raw)

        result.append(
            {
                "msg_id": str(m.get("msg_id") or m.get("client_msg_id") or ""),
                "sender_uid": str(m.get("sender_uid") or m.get("from_uid") or ""),
                "content_text": str(text).strip(),
                "create_time": int(m.get("create_time") or m.get("send_time") or 0),
            }
        )
    return result


# ── JS：从 DOM 读取最近聊天记录（备用方案） ──────────────────────────
EXTRACT_CHAT_JS = """
() => {
    const input = document.querySelector('[placeholder*="Enter键发送"]');
    if (!input) return { ok: false, reason: 'input not found' };

    // 策略1：向上找能滚动的聊天容器（最多30层）
    let container = null;
    let el = input.parentElement;
    for (let i = 0; i < 30; i++) {
        if (!el || el === document.body) break;
        const s = window.getComputedStyle(el);
        const oy = s.overflowY;
        if ((oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight + 10) {
            container = el;
            break;
        }
        el = el.parentElement;
    }

    // 策略2：fallback——找输入框上方最近的大面积区域（聊天消息容器）
    if (!container) {
        const inputRect = input.getBoundingClientRect();
        const all = document.querySelectorAll('*');
        let bestEl = null, bestArea = 0;
        for (const e of all) {
            const r = e.getBoundingClientRect();
            // 宽度 > 200px，高度 > 200px，底部在输入框上方
            if (r.width < 200 || r.height < 200) continue;
            if (r.bottom > inputRect.top + 10) continue;
            if (r.top < 50) continue;
            const area = r.width * r.height;
            if (area > bestArea) { bestArea = area; bestEl = e; }
        }
        container = bestEl;
    }

    if (!container) return { ok: false, reason: 'scroll container not found' };

    // 收集所有文本节点及其位置（x 坐标区分左/右气泡）
    const msgs = [];
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
        const t = walker.currentNode.textContent.trim();
        if (t.length < 2) continue;
        const el = walker.currentNode.parentElement;
        if (!el) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width < 5 || rect.height < 5) continue;
        // 过滤掉输入框自身的 placeholder
        if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') continue;
        msgs.push({ text: t, x: Math.round(rect.left), y: Math.round(rect.top) });
    }

    if (!msgs.length) return { ok: false, reason: 'no text nodes' };

    // 简单去重（相邻重复行）
    const deduped = [msgs[0]];
    for (let i = 1; i < msgs.length; i++) {
        const prev = deduped[deduped.length - 1];
        if (msgs[i].text !== prev.text || Math.abs(msgs[i].y - prev.y) > 5) {
            deduped.push(msgs[i]);
        }
    }

    return { ok: true, msgs: deduped };
}
"""


# ── 填入输入框并发送 ──────────────────────────────────────────────────
async def send_reply(page: Page, text: str) -> bool:
    """把 text 填入消息输入框并发送（Enter）。"""
    try:
        box = page.get_by_placeholder(INPUT_PLACEHOLDER)
        await box.click()
        await asyncio.sleep(0.2)

        # 清空后填入（fill 会替换已有内容）
        await box.fill(text)
        await asyncio.sleep(0.3)

        # 直接按 Enter 发送
        await box.press("Enter")
        log.info("✅ 已发送回复: %r", text[:60])
        return True
    except Exception as e:
        log.error("❌ 发送失败: %s", e)
        return False


# ── 主 Agent 逻辑 ─────────────────────────────────────────────────────
class DouyinCSAgent:
    def __init__(self, cfg: dict, platform_url: str | None = None) -> None:
        qwen_cfg = cfg.get("qwen") or {}
        reply_cfg = cfg.get("reply") or {}
        watch_cfg = cfg.get("watch") or {}

        self.client = QwenTextClient(
            api_key=str(qwen_cfg.get("api_key", "")),
            base_url=str(qwen_cfg.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")),
            model=str(qwen_cfg.get("model", "qwen-turbo")),
            temperature=float(qwen_cfg.get("temperature", 0.3)),
            timeout=int(qwen_cfg.get("timeout", 60)),
        )
        self.system_prompt: str = reply_cfg.get("system") or "你是抖音企业号上的客服。用简短、专业、友好的中文回复。"
        self.interval: float = float(watch_cfg.get("interval_sec", 3.0))
        self.post_send_pause: float = float(watch_cfg.get("post_send_pause_sec", 2.0))
        self.agent_uid: str = str(cfg.get("agent_uid", ""))  # 坐席 UID，用于区分访客消息

        # 平台上报
        _pu = platform_url or cfg.get("platform", {}).get("url")
        self.reporter: PlatformReporter | None = PlatformReporter(_pu) if _pu else None
        # 已上报的手机号（避免同一会话重复创建线索）
        self._reported_phones: set[str] = set()
        # 当前活跃会话的 contact uid -> lead_id
        self._contact_lead: dict[str, int] = {}

        # 运行时状态
        self._seen_msg_ids: set[str] = set()
        self._last_sent_text: str = ""
        self._pending_reply_flag: bool = False  # 避免在同一会话反复回复

        # 从 API 响应中拿到的最新消息列表（访客侧排前）
        self._latest_messages: list[dict] = []
        self._messages_updated = asyncio.Event()

    # ── 拦截 IM API 响应 ─────────────────────────────────────────────
    async def _on_response(self, resp: Response) -> None:
        url = resp.url
        if not any(k in url for k in IM_API_KEYWORDS):
            return
        if resp.status != 200:
            return
        try:
            body = await resp.json()
        except Exception:
            return

        msgs = parse_messages(body)
        if msgs:
            self._latest_messages = msgs
            self._messages_updated.set()
            log.debug("拦截到 %d 条消息 (from %s)", len(msgs), url.split("/")[2])

    # ── 检测是否有未回复的访客新消息 ────────────────────────────────
    def _get_pending_guest_message(self) -> str | None:
        """
        返回最新一条未处理的访客消息文本，或 None。
        访客 = sender_uid 不等于 agent_uid（若 agent_uid 已知）
              或消息 uid 不在 seen 集合里
        """
        if not self._latest_messages:
            return None

        # 按时间排序，最新在后
        sorted_msgs = sorted(self._latest_messages, key=lambda m: m["create_time"])

        # 找最后一条消息
        last = sorted_msgs[-1]
        msg_id = last["msg_id"]
        text = last["content_text"]

        if not text:
            return None

        # 是否已处理过
        if msg_id and msg_id in self._seen_msg_ids:
            return None

        # 是否是坐席自己发的（刚才发出的回复）
        if self.agent_uid and last["sender_uid"] == self.agent_uid:
            return None

        # 是否是我们刚发出的内容（按文本去重）
        if text == self._last_sent_text:
            if msg_id:
                self._seen_msg_ids.add(msg_id)
            return None

        # 确认是访客消息
        if msg_id:
            self._seen_msg_ids.add(msg_id)
        return text

    # ── Qwen 生成回复 ────────────────────────────────────────────────
    def _generate_reply(self, guest_text: str, context_msgs: list[dict]) -> str:
        # 构造近期会话上下文
        recent = context_msgs[-10:]  # 最近 10 条
        context_lines = []
        for m in recent:
            role = "访客" if (not self.agent_uid or m["sender_uid"] != self.agent_uid) else "客服"
            context_lines.append(f"{role}: {m['content_text']}")

        user_prompt = (
            "以下是最近的会话记录：\n\n"
            + "\n".join(context_lines)
            + f"\n\n访客最新消息：{guest_text}\n\n"
            "请生成一条可直接发给访客的中文回复（只输出正文，不要前缀或 Markdown）。"
        )
        return self.client.complete(self.system_prompt, user_prompt)

    # ── 主循环 ───────────────────────────────────────────────────────
    async def run(self, page: Page) -> None:
        log.info("▶ DouyinCSAgent 启动，监听中... (Ctrl+C 退出)")

        # 注册响应拦截
        page.on("response", self._on_response)

        # 初始触发一次页面刷新，让浏览器重新拉取消息
        try:
            await page.reload(wait_until="domcontentloaded", timeout=15_000)
            await asyncio.sleep(2)
        except Exception as e:
            log.warning("页面刷新失败（跳过）: %s", e)

        loop_count = 0
        while True:
            try:
                # 等待消息更新事件（最多 interval 秒超时）
                try:
                    await asyncio.wait_for(
                        self._messages_updated.wait(),
                        timeout=self.interval,
                    )
                    self._messages_updated.clear()
                except asyncio.TimeoutError:
                    pass

                loop_count += 1
                guest_text = self._get_pending_guest_message()

                if not guest_text:
                    if loop_count % 10 == 0:
                        log.info("[%d] 暂无新访客消息", loop_count)
                    continue

                log.info("[%d] 🔔 新访客消息: %r", loop_count, guest_text[:80])

                # 获取当前会话访客 UID（取最新消息的 sender_uid）
                sorted_msgs = sorted(self._latest_messages, key=lambda m: m["create_time"])
                last_msg = sorted_msgs[-1] if sorted_msgs else {}
                contact_uid = last_msg.get("sender_uid") or ""

                # ── 上报访客消息（非阻塞） ────────────────────────────────
                if self.reporter:
                    self.reporter.report_conversation(
                        platform="douyin",
                        direction="in",
                        content=guest_text,
                        contact_id=contact_uid or None,
                    )

                # ── 手机号检测：发现手机号则创建/更新线索 ─────────────────
                if self.reporter:
                    phones = _PHONE_RE.findall(guest_text)
                    for phone in phones:
                        if phone not in self._reported_phones:
                            self._reported_phones.add(phone)
                            # 把当前会话最近消息作为 context
                            ctx_lines = [
                                f"{'访客' if (not self.agent_uid or m['sender_uid'] != self.agent_uid) else '客服'}: {m['content_text']}"
                                for m in sorted_msgs[-6:]
                            ]
                            lead_id = self.reporter.report_lead(
                                phone=phone,
                                source="douyin",
                                source_user=contact_uid or None,
                                source_context="\n".join(ctx_lines),
                            )
                            if lead_id and contact_uid:
                                self._contact_lead[contact_uid] = lead_id
                            log.info("  📞 检测到手机号 %s → lead_id=%s", phone, lead_id)

                # 生成回复
                log.info("  调用 Qwen...")
                try:
                    reply = self._generate_reply(guest_text, self._latest_messages)
                except Exception as e:
                    log.error("  Qwen 失败: %s", e)
                    continue

                log.info("  Qwen 回复: %r", reply[:100])

                # 发送
                ok = await send_reply(page, reply)
                if ok:
                    self._last_sent_text = reply
                    # ── 上报客服回复消息（非阻塞） ────────────────────────
                    if self.reporter:
                        self.reporter.report_conversation(
                            platform="douyin",
                            direction="out",
                            content=reply,
                            contact_id=contact_uid or None,
                        )
                    await asyncio.sleep(self.post_send_pause)

            except KeyboardInterrupt:
                raise
            except Exception as e:
                log.exception("主循环异常（继续运行）: %s", e)
                await asyncio.sleep(2)


# ── 入口 ──────────────────────────────────────────────────────────────
async def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="抖音企业号客服 Agent（Playwright CDP）")
    parser.add_argument("--config", type=Path, default=Path(__file__).parent / "config.yaml")
    parser.add_argument("--cdp", default=CDP_URL, help="Chrome CDP 地址，默认 http://localhost:9222")
    parser.add_argument(
        "--platform-url",
        default=None,
        help="Agent Console 后端 URL（如 http://127.0.0.1:8000），用于上报线索和消息",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(args.config)

    agent = DouyinCSAgent(cfg, platform_url=args.platform_url)

    async with async_playwright() as pw:
        log.info("连接 Chrome CDP: %s", args.cdp)
        try:
            browser = await pw.chromium.connect_over_cdp(args.cdp)
        except Exception as e:
            log.error("❌ 无法连接 Chrome：%s\n请先运行 start_chrome.bat", e)
            return 1

        # 找到抖音客服页
        page = None
        for ctx in browser.contexts:
            for p in ctx.pages:
                if "leads.cluerich.com" in p.url:
                    page = p
                    break
            if page:
                break

        if not page:
            log.info("未找到已打开的客服页，新建 tab...")
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await ctx.new_page()
            await page.goto(SESSION_URL, wait_until="domcontentloaded", timeout=30_000)
            log.info("请在浏览器中完成登录后脚本自动继续（等待 5 秒）...")
            await asyncio.sleep(5)

        log.info("使用页面: %s", page.url)
        try:
            await agent.run(page)
        except KeyboardInterrupt:
            log.info("已停止。")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
