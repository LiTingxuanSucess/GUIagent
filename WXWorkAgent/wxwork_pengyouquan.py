"""
wxwork_pengyouquan.py — 企业朋友圈群发自动化
===============================================
通过 CDP 连接已登录的 Chrome，自动完成以下流程：
  1. 打开企业微信管理后台的朋友圈群发页
  2. 点击「新建内容」
  3. 输入文案
  4. 添加图片
  5. 点击「通知成员发表」

前置条件：
  先运行 start_chrome_admin.bat，在弹出的 Chrome 中完成企业微信登录，
  然后再运行本脚本。

用法：
  py -3.11 wxwork_pengyouquan.py
  py -3.11 wxwork_pengyouquan.py --content "周末活动来啦" --image "D:\\pic.jpg"
  py -3.11 wxwork_pengyouquan.py --cdp http://127.0.0.1:9222 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pengyouquan")

TARGET_URL = "https://work.weixin.qq.com/wework_admin/frame#/customer/config/groupSend"
DEFAULT_IMAGE = r"D:\Workspace\kg_graph.jpg"
DEFAULT_CONTENT = "欢迎关注我们，有任何问题欢迎随时联系！"


def _normalize_cdp_url(url: str) -> str:
    """强制走 IPv4 本机地址，避免 localhost→::1 而 Chrome CDP 只监听 127.0.0.1（Windows ECONNREFUSED）。"""
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return "http://127.0.0.1:9222"
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower().strip().strip("[]")
    # [::1] 等解析后 hostname 常为 "::1"
    if host not in ("localhost", "::1"):
        return raw.rstrip("/")
    hp = "127.0.0.1"
    if parsed.port is not None:
        hp += f":{parsed.port}"
    auth = ""
    if parsed.username is not None:
        u = quote(parsed.username, safe="")
        pw = ":" + quote(parsed.password, safe="") if parsed.password is not None else ""
        auth = u + pw + "@"
    new_netloc = auth + hp
    return urlunparse(
        (
            parsed.scheme,
            new_netloc,
            parsed.path or "",
            parsed.params or "",
            parsed.query or "",
            parsed.fragment or "",
        )
    ).rstrip("/")


async def run(cdp_url: str, content: str, image: str | None, dry_run: bool) -> None:
    from playwright.async_api import async_playwright

    cdp_url = _normalize_cdp_url(cdp_url)
    log.info("连接 CDP: %s", cdp_url)
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(cdp_url)

        # 找到目标页面，或新建 tab
        page = None
        for ctx in browser.contexts:
            for p in ctx.pages:
                if "work.weixin.qq.com" in p.url:
                    page = p
                    break
            if page:
                break

        if page is None:
            log.info("未找到企业微信后台页，新建 tab...")
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await ctx.new_page()

        # 导航到群发朋友圈页（强制刷新确保干净状态）
        log.info("导航到朋友圈群发页...")
        await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_load_state("networkidle", timeout=15_000)
        await asyncio.sleep(1.5)

        # SPA 不会清除上次残留的弹窗蒙层，用 JS 强制移除
        removed = await page.evaluate("""() => {
            const masks = [...document.querySelectorAll('.js_dialog_mask, .ww_dialog')];
            masks.forEach(e => { try { e.remove(); } catch(_){} });
            return masks.length;
        }""")
        if removed:
            log.info("移除了 %d 个残留蒙层/弹窗元素", removed)
            await asyncio.sleep(0.3)

        # ── 步骤 1：点击「新建内容」 ─────────────────────────────────────
        log.info("点击「新建内容」...")
        btn_new = page.get_by_role("button", name="新建内容").first
        if not await btn_new.count():
            btn_new = page.locator("text=新建内容").first
        await btn_new.click(timeout=10_000)
        await asyncio.sleep(1.5)
        await _screenshot(page, "step1_new_content_clicked.png")

        # ── 步骤 1.5：清空旧内容和已有图片 ─────────────────────────────
        editor = page.locator("[contenteditable='true']").first
        if not await editor.count():
            editor = page.locator("textarea").first

        # 清空文案编辑器
        if await editor.count():
            await editor.click(timeout=5_000)
            await page.keyboard.press("Control+a")
            await page.keyboard.press("Delete")
            await asyncio.sleep(0.3)

        # 删除已有图片（hover 出 × 按钮后点击，或用 JS 移除）
        img_items = page.locator(".js_moment_imgItem, .csMessage_infoItem_preview_item")
        img_count = await img_items.count()
        if img_count:
            log.info("发现 %d 张已有图片，先删除...", img_count)
            for i in range(img_count - 1, -1, -1):
                item = img_items.nth(i)
                await item.hover()
                await asyncio.sleep(0.3)
                del_btn = item.locator(".js_del, .ww_del, [class*=del], [class*=close]").first
                if await del_btn.count():
                    await del_btn.click(timeout=3_000)
                else:
                    hdl = await item.element_handle()
                    if hdl:
                        await page.evaluate("(el) => el.remove()", hdl)
                await asyncio.sleep(0.3)
            await asyncio.sleep(0.5)
            log.info("已清除旧图片")

        # ── 步骤 2：输入文案 ─────────────────────────────────────────────
        log.info("输入文案: %r", content[:30])
        await editor.click(timeout=8_000)
        await editor.fill(content)
        await asyncio.sleep(0.5)
        await _screenshot(page, "step2_content_typed.png")

        if dry_run:
            log.info("[dry-run] 到达文案输入步骤，停止（不上传图片，不提交）")
            return

        # ── 步骤 3-4：上传图片（直接 set_input_files，不经过弹窗）───────
        if image:
            img_path = Path(image)
            if not img_path.exists():
                log.warning("图片文件不存在: %s，跳过上传", image)
                image = None

        if image:
            log.info("上传图片: %s", image)
            img_input = page.locator("input[name='uploadImageFile']").first
            if not await img_input.count():
                img_input = page.locator("input[type='file']").first

            await img_input.set_input_files(str(img_path))
            log.info("文件已设置（set_input_files on uploadImageFile）")
            await asyncio.sleep(3.0)
            await _screenshot(page, "step5_image_uploaded.png")

        # ── 步骤 6：等待图片预览加载 ─────────────────────────────────────
        log.info("等待图片处理完成...")
        await asyncio.sleep(2.0)
        await _screenshot(page, "step6_before_submit.png")

        # ── 步骤 6.5：选择「可见的客户」→ 选公开 → 确定 ─────────────────
        # 表单顶部有「选择客户」链接，不点就无法提交
        log.info("点击「选择客户」链接...")
        select_cust = page.get_by_text("选择客户", exact=True).first
        await select_cust.click(timeout=8_000)
        await asyncio.sleep(0.8)
        await _screenshot(page, "step6b_select_customer_dialog.png")

        # 对话框「选择可见的客户」：点击「公开」单选按钮
        # 直接点击单选 input，或点击 label 文字
        radio_public = page.locator("input[type='radio']").first
        if await radio_public.count():
            await radio_public.click(timeout=3_000)
            log.info("已点击第一个单选（公开）")
        else:
            log.info("单选按钮未找到，尝试点击文字「公开」")
            await page.get_by_text("公开", exact=True).first.click(timeout=3_000)

        await asyncio.sleep(0.3)

        # 点击对话框的「确定」按钮（可能是 <a> 不是 <button>）
        dlg_ok = page.locator("text=确定").first
        await dlg_ok.click(timeout=5_000)
        await asyncio.sleep(0.8)
        log.info("「选择可见的客户」对话框已确认")
        await _screenshot(page, "step6c_customer_confirmed.png")

        # ── 步骤 7：点击「通知成员发表」 ─────────────────────────────────
        log.info("点击「通知成员发表」...")
        btn_notify = page.get_by_role("button", name="通知成员发表").first
        if not await btn_notify.count():
            btn_notify = page.locator("text=通知成员发表").first
        await btn_notify.click(timeout=10_000)
        await asyncio.sleep(1.5)
        await _screenshot(page, "step7_notify_clicked.png")

        # 「提示」弹窗：「成员将收到服务通知，确认后内容将发表到客户的朋友圈」
        # 点击「发送通知」确认最终发送
        send_notify_btn = page.get_by_role("button", name="发送通知").first
        if not await send_notify_btn.count():
            send_notify_btn = page.locator("text=发送通知").first
        if await send_notify_btn.count() and await send_notify_btn.is_visible():
            log.info("点击「发送通知」最终确认...")
            await send_notify_btn.click(timeout=5_000)
            await asyncio.sleep(1.5)
        else:
            # fallback：通用「确定」按钮
            confirm = page.get_by_role("button", name="确定").first
            if await confirm.count() and await confirm.is_visible():
                log.info("点击「确定」确认...")
                await confirm.click(timeout=5_000)
                await asyncio.sleep(1.0)

        await _screenshot(page, "step8_done.png")
        log.info("完成！朋友圈发表任务已提交。")


async def _screenshot(page, name: str) -> None:
    """保存调试截图到 shots/pengyouquan/"""
    out_dir = Path(__file__).parent / "shots" / "pengyouquan"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    try:
        await page.screenshot(path=str(path))
        log.debug("截图: %s", path)
    except Exception as e:
        log.debug("截图失败: %s", e)


def main() -> int:
    ap = argparse.ArgumentParser(description="企业微信朋友圈群发自动化")
    ap.add_argument("--cdp", default="http://127.0.0.1:9222", help="CDP (default http://127.0.0.1:9222，勿用 localhost 避免 IPv6)")
    ap.add_argument("--content", default=DEFAULT_CONTENT, help="发圈文案")
    ap.add_argument("--image", default=DEFAULT_IMAGE, help="图片路径 (留空则不上传图片)")
    ap.add_argument("--no-image", action="store_true", help="不上传图片")
    ap.add_argument("--dry-run", action="store_true", help="仅走到文案输入步骤，不提交")
    args = ap.parse_args()

    image = None if args.no_image else args.image
    asyncio.run(run(args.cdp, args.content, image, args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
