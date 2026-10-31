"""
capture_fixtures.py — 一次性录制 Douyin CS 页面的核心 DOM 数据
=================================================================
运行一次，把真实页面状态存成 JSON fixture，
之后 test_on_fixtures.py 完全离线对这份数据跑断言。

用法：
  先运行 start_chrome.bat，打开抖音企业号客服页面
  py -3.11 tools/capture_fixtures.py
  py -3.11 tools/capture_fixtures.py --cdp http://localhost:9222 --out fixtures/

抓取内容：
  - page_info.json     : URL、标题、输入框是否存在
  - xhr_samples.json   : 最多 5 条拦截到的 IM API XHR 响应 body
  - js_extract.json    : EXTRACT_CHAT_JS 执行结果
  - dom_snapshot.json  : 关键选择器的 DOM 查询结果
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from playwright.async_api import async_playwright, Response  # noqa: E402
from douyin_watch import EXTRACT_CHAT_JS, IM_API_KEYWORDS, INPUT_PLACEHOLDER, SESSION_URL  # noqa: E402


async def capture(cdp_url: str, out_dir: Path, wait_sec: int) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        print(f"[capture] 连接 CDP: {cdp_url}")
        browser = await pw.chromium.connect_over_cdp(cdp_url)

        # 找到抖音客服页
        page = None
        for ctx in browser.contexts:
            for p in ctx.pages:
                if "leads.cluerich.com" in p.url or "csr" in p.url.lower():
                    page = p
                    break
            if page:
                break

        if not page:
            print("[capture] 未找到已打开的客服页，尝试直接打开...")
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await ctx.new_page()
            await page.goto(SESSION_URL, wait_until="domcontentloaded", timeout=30_000)
            print("[capture] 请在浏览器完成登录后回车继续...")
            input()

        print(f"[capture] 使用页面: {page.url}")

        # ── 1. page_info ─────────────────────────────────────────────────
        title = await page.title()
        input_exists = await page.get_by_placeholder(INPUT_PLACEHOLDER).count() > 0

        page_info = {
            "url": page.url,
            "title": title,
            "input_placeholder": INPUT_PLACEHOLDER,
            "input_exists": input_exists,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        (out_dir / "page_info.json").write_text(
            json.dumps(page_info, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[capture] page_info: url={page.url!r}  input_exists={input_exists}")

        # ── 2. xhr_samples (拦截 IM API XHR) ─────────────────────────────
        xhr_samples: list[dict] = []
        all_xhr_urls: list[str] = []   # 调试用：记录所有 XHR/fetch 响应

        # 静态资源后缀，排除掉
        _STATIC_EXT = (".js", ".css", ".png", ".jpg", ".woff", ".woff2", ".svg", ".ico", ".ttf")

        bff_samples: list[dict] = []   # leads.cluerich.com/bff/ 的所有响应，用于发现真实 API
        conv_detail_request: dict = {}  # 记录 conversation-detail 的请求体

        from playwright.async_api import Request as PwRequest

        async def on_request(req: PwRequest) -> None:
            """拦截 conversation-detail 的请求体，用于了解 POST 参数格式"""
            if "conversation-detail" not in req.url:
                return
            try:
                body_bytes = req.post_data
                body_str = req.post_data_buffer.decode("utf-8") if req.post_data_buffer else body_bytes or ""
                conv_detail_request["url"] = req.url
                conv_detail_request["method"] = req.method
                conv_detail_request["body"] = body_str
                conv_detail_request["headers"] = dict(req.headers)
                print(f"[capture]   conversation-detail REQUEST: method={req.method}  body={body_str[:200]}")
            except Exception as e:
                conv_detail_request["error"] = str(e)

        page.on("request", on_request)

        async def on_response(resp: Response) -> None:
            if resp.status != 200:
                return
            url = resp.url
            # 排除静态资源文件
            path_part = url.split("?")[0]
            if any(path_part.endswith(ext) for ext in _STATIC_EXT):
                return
            ct = resp.headers.get("content-type", "")
            if "json" not in ct and "text" not in ct:
                return
            # 记录所有非静态 API 响应 URL
            all_xhr_urls.append(f"[{resp.status}] {url}")

            # 额外捕获 bff/ 接口的完整 body（帮助发现消息列表 API）
            if ("leads.cluerich.com/bff/" in url or "leads.cluerich.com/clue-ai/bff/" in url) and len(bff_samples) < 30:
                try:
                    body = await resp.json()
                    bff_samples.append({"url": url, "body": body})
                    print(f"[capture]   bff API: {url.split('/bff/')[-1].split('?')[0]!r}  "
                          f"keys={list(body.keys())[:5]}")
                except Exception:
                    pass

            # 命中 IM 关键词才存入 xhr_samples
            if not any(k in url for k in IM_API_KEYWORDS):
                return
            if len(xhr_samples) >= 5:
                return
            try:
                body = await resp.json()
            except Exception:
                return
            xhr_samples.append({
                "url": resp.url,
                "status": resp.status,
                "body": body,
            })
            print(f"[capture]   XHR #{len(xhr_samples)}: {resp.url.split('/')[2]}  "
                  f"body_keys={list(body.keys())[:4]}")

        page.on("response", on_response)

        # 先导航到会话中心页面（重新加载，让会话列表处于"未选中任何会话"状态）
        print(f"[capture] 导航到会话中心，重置会话选中状态...")
        await page.goto(SESSION_URL, wait_until="domcontentloaded", timeout=20_000)

        # 等待会话列表加载完成（"正在努力加载中" 消失，或列表项出现）
        print(f"[capture]   等待会话列表加载（最多15s）...")
        for i in range(15):
            await asyncio.sleep(1)
            # 检查是否还在加载
            loading = await page.evaluate("""
                () => {
                    const body = document.body.innerText || '';
                    return body.includes('正在努力加载');
                }
            """)
            # 检查是否有会话项出现（x: 160-450 区域有文本内容）
            has_sessions = await page.evaluate("""
                () => {
                    const all = document.querySelectorAll('*');
                    for (const el of all) {
                        const r = el.getBoundingClientRect();
                        if (r.height < 40 || r.height > 120) continue;
                        if (r.left < 140 || r.right > 460) continue;
                        if (r.top < 150) continue;
                        if ((el.innerText || '').trim().length > 1) return true;
                    }
                    return false;
                }
            """)
            print(f"[capture]     {i+1}s  loading={loading}  has_sessions={has_sessions}")
            if not loading and has_sessions:
                break

        # 截图看当前状态
        snap_pre = out_dir / "screenshot_before_click.png"
        await page.screenshot(path=str(snap_pre))
        print(f"[capture]   点击前截图: {snap_pre}")

        # 扫描会话列表区域（x: 160-430），跳过纯导航文字，找会话摘要行
        # 关键：limit 调大，不能在导航项就停止
        session_info = await page.evaluate("""
            () => {
                // 导航类短文本，跳过
                const NAV_WORDS = new Set(['首页','场景管理','短视频管理','直播管理','评价管理',
                    '咨询管理','会话中心','自动化设置','客服中心','咨询中控台','店铺管理',
                    '主页装修','独立店装修','流量入口','门店设置','服务管理','高级在线预约',
                    '营销管理','活动广场','经营工具','留资获客','私域经营','用户管理',
                    '高潜用户','经营分析','管理设置','数据分析']);
                const result = [];
                const all = document.querySelectorAll('*');
                for (const el of all) {
                    const r = el.getBoundingClientRect();
                    if (r.height < 35 || r.height > 180) continue;
                    if (r.width < 80 || r.width > 400) continue;
                    // 会话列表列：x范围 155-430
                    if (r.left < 155 || r.right > 430) continue;
                    if (r.top < 130) continue;
                    const text = (el.innerText || '').trim();
                    if (!text || text.length < 2) continue;
                    // 跳过纯导航菜单项
                    if (NAV_WORDS.has(text)) continue;
                    result.push({
                        tag: el.tagName,
                        cls: (el.className || '').slice(0, 60),
                        x: Math.round(r.left + r.width / 2),
                        y: Math.round(r.top + r.height / 2),
                        w: Math.round(r.width),
                        h: Math.round(r.height),
                        text: text.slice(0, 50),
                    });
                    if (result.length >= 20) break;
                }
                return result;
            }
        """)
        print(f"[capture] 会话列表区域 DOM 元素 ({len(session_info)} 个):")
        for s in session_info:
            print(f"[capture]   [{s['tag']}] cls={s['cls']!r}  text={s['text']!r}  xy=({s['x']},{s['y']})")

        # 在会话列表区域内（x: 160-460）找文字最长（最像"用户名+消息预览"）的元素
        # 按文字长度排序，最长的通常是会话摘要行
        best = sorted(session_info, key=lambda s: -len(s["text"]))

        clicked = False
        for candidate in best[:3]:
            cls = candidate["cls"].lower()
            # 跳过明显的导航菜单项（纯汉字2字，且不像会话预览）
            if len(candidate["text"]) <= 6 and any(nav in candidate["text"] for nav in
               ["管理", "设置", "经营", "首页", "中心", "数据", "装修"]):
                continue
            try:
                await page.mouse.click(candidate["x"], candidate["y"])
                print(f"[capture]   点击坐标 ({candidate['x']},{candidate['y']}) -> {candidate['text']!r}")
                clicked = True
                await asyncio.sleep(2.5)
                break
            except Exception as e:
                print(f"[capture]   点击 {candidate['text']!r} 失败: {e}")

        if not clicked:
            # 最后尝试：直接点击会话列表列中央（x≈260，y≈215 是典型的第一条会话位置）
            print(f"[capture]   [!] 未能识别会话项，尝试点击默认坐标 (265, 215)...")
            await page.mouse.click(265, 215)
            clicked = True
            await asyncio.sleep(2.5)

        print(f"[capture] 等待 {wait_sec}s 拦截 XHR...")
        await asyncio.sleep(wait_sec)

        (out_dir / "xhr_samples.json").write_text(
            json.dumps(xhr_samples, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if conv_detail_request:
            (out_dir / "conv_detail_request.json").write_text(
                json.dumps(conv_detail_request, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"[capture] conversation-detail 请求体已保存: conv_detail_request.json")

        # ── 主动 POST conversation-detail，传入已知会话 ID ──────────────────
        # ConversationShortId 来自 read-status URL 参数，已知值
        KNOWN_CONV_IDS = [
            "7630131492751983155",  # 从 read-status URL 提取
        ]
        # 如果 bff_samples 里有 read-status，也可以从中提取
        for s in bff_samples:
            if "read-status" in s.get("url", "") and "ConversationShortId=" in s.get("url", ""):
                import re as _re
                m = _re.search(r"ConversationShortId=(\d+)", s["url"])
                if m and m.group(1) not in KNOWN_CONV_IDS:
                    KNOWN_CONV_IDS.append(m.group(1))

        print(f"[capture] 主动 POST conversation-detail，尝试 conv IDs: {KNOWN_CONV_IDS}")
        conv_api_url = "https://leads.cluerich.com/clue-ai/bff/chat/conversation-detail"
        for conv_id in KNOWN_CONV_IDS:
            for id_field in ("conversationShortId", "imCovShortId", "conversationID"):
                payload = {
                    "platformID": 1,
                    "pageInfo": {"page": 1, "size": 20},
                    id_field: conv_id,
                }
                try:
                    resp_direct = await page.request.post(
                        conv_api_url,
                        data=json.dumps(payload, ensure_ascii=False),
                        headers={"Content-Type": "application/json"},
                        timeout=8000,
                    )
                    body_direct = await resp_direct.json()
                    msgs = (body_direct.get("data") or {}).get("messages") or []
                    print(f"[capture]   {id_field}={conv_id[:12]}...  messages={len(msgs)}")
                    if msgs:
                        # 成功！保存这个响应作为 xhr_samples 的补充
                        with_msgs = {"url": conv_api_url, "status": 200, "body": body_direct,
                                     "_request_payload": payload}
                        (out_dir / "xhr_conv_detail.json").write_text(
                            json.dumps(with_msgs, ensure_ascii=False, indent=2), encoding="utf-8"
                        )
                        print(f"[capture]   ✓ 找到 {len(msgs)} 条消息，已保存至 xhr_conv_detail.json")
                        break
                except Exception as e:
                    print(f"[capture]   {id_field} POST 失败: {e}")
            else:
                continue
            break

        (out_dir / "bff_samples.json").write_text(
            json.dumps(bff_samples, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # 保存所有 JSON XHR URL，供排查关键词是否匹配
        (out_dir / "all_xhr_urls.txt").write_text(
            "\n".join(all_xhr_urls), encoding="utf-8"
        )
        print(f"[capture] xhr_samples: {len(xhr_samples)} 条  bff_samples: {len(bff_samples)} 条  "
              f"(共见到 {len(all_xhr_urls)} 条 JSON 响应)")
        if all_xhr_urls and not xhr_samples:
            print("[capture] [!] 未命中 IM_API_KEYWORDS，实际 JSON 请求如下（取前15条）:")
            for u in all_xhr_urls[:15]:
                print(f"[capture]     {u}")

        # ── 2b. 截图存档（调试用）─────────────────────────────────────────
        try:
            screenshot_path = out_dir / "screenshot.png"
            await page.screenshot(path=str(screenshot_path), full_page=False)
            print(f"[capture] 截图已保存: {screenshot_path}")
        except Exception as e:
            print(f"[capture] 截图失败: {e}")

        # ── 2c. 详细 DOM 结构探查（帮助找到左侧会话列表的真实选择器）───────
        dom_debug = await page.evaluate("""
            () => {
                // 收集页面中所有元素的类名和位置统计
                const leftItems = [];   // 左侧面板 (x < 500)
                const all = document.querySelectorAll('*');
                const seen = new Set();
                for (const el of all) {
                    const r = el.getBoundingClientRect();
                    if (r.width < 40 || r.height < 30) continue;
                    if (r.right > 550) continue;   // 只看左侧面板
                    if (r.height > 200) continue;  // 排除大容器
                    const cls = el.className;
                    if (typeof cls !== 'string' || !cls) continue;
                    const key = cls.slice(0, 40);
                    if (seen.has(key)) continue;
                    seen.add(key);
                    leftItems.push({
                        tag: el.tagName,
                        cls: key,
                        x: Math.round(r.left),
                        y: Math.round(r.top),
                        w: Math.round(r.width),
                        h: Math.round(r.height),
                        text: (el.innerText || '').trim().slice(0, 30),
                    });
                    if (leftItems.length >= 20) break;
                }
                return leftItems;
            }
        """)
        print(f"[capture] 左侧面板 DOM 元素 ({len(dom_debug)} 个):")
        for d in dom_debug[:15]:
            print(f"[capture]   [{d['tag']}] cls={d['cls']!r} "
                  f"xy=({d['x']},{d['y']}) wh=({d['w']}x{d['h']}) text={d['text']!r}")

        # ── 3. js_extract ─────────────────────────────────────────────────
        try:
            js_result = await page.evaluate(EXTRACT_CHAT_JS)
        except Exception as e:
            js_result = {"ok": False, "reason": str(e)}
        (out_dir / "js_extract.json").write_text(
            json.dumps(js_result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        ok = js_result.get("ok", False)
        msgs = js_result.get("msgs", [])
        print(f"[capture] js_extract: ok={ok}  msgs={len(msgs)}")
        if not ok:
            print(f"[capture]   reason: {js_result.get('reason', '?')}")

        # ── 4. dom_snapshot ───────────────────────────────────────────────
        dom_checks = [
            ("input_by_placeholder",
             f'document.querySelector(\'[placeholder*="Enter键发送"]\') !== null'),
            ("input_count_by_placeholder",
             f'document.querySelectorAll(\'[placeholder*="Enter键发送"]\').length'),
            ("chat_container_exists",
             'document.querySelector(".chat-message-list, [class*=message]") !== null'),
            ("page_title_text",
             'document.title'),
        ]
        dom_snapshot: dict = {}
        for key, js in dom_checks:
            try:
                val = await page.evaluate(js)
                dom_snapshot[key] = val
            except Exception as e:
                dom_snapshot[key] = f"ERROR: {e}"
        (out_dir / "dom_snapshot.json").write_text(
            json.dumps(dom_snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[capture] dom_snapshot: {dom_snapshot}")

        print(f"\n[capture] 完成！fixture 已保存至: {out_dir.resolve()}")
        print("[capture] 运行测试: py -3.11 tools/test_on_fixtures.py")


def main() -> int:
    ap = argparse.ArgumentParser(description="Capture Douyin CS page fixtures via CDP")
    ap.add_argument("--cdp", default="http://localhost:9222")
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "fixtures")
    ap.add_argument("--wait", type=int, default=5, help="Seconds to wait for XHR after reload")
    args = ap.parse_args()
    asyncio.run(capture(args.cdp, args.out, args.wait))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
