"""Diagnose the media upload flow on WeCom group-send-moments page."""
from __future__ import annotations
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

SHOTS = Path(__file__).parent / "shots" / "pengyouquan"
SHOTS.mkdir(parents=True, exist_ok=True)

TARGET = "https://work.weixin.qq.com/wework_admin/frame#/customer/config/groupSend"


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9223")
        ctx = browser.contexts[0]
        pages = ctx.pages
        page = next((pg for pg in pages if "work.weixin" in pg.url), pages[0] if pages else None)
        print("Current URL:", page.url)

        await page.goto(TARGET, wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_load_state("networkidle", timeout=15_000)
        await asyncio.sleep(1)

        # close leftover mask
        mask = page.locator(".js_dialog_mask")
        if await mask.count() and await mask.is_visible():
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
            if await mask.count() and await mask.is_visible():
                await page.reload(wait_until="networkidle", timeout=20_000)
                await asyncio.sleep(1)

        # open 新建内容
        await page.locator("text=新建内容").first.click(timeout=10_000)
        await asyncio.sleep(2)
        await page.screenshot(path=str(SHOTS / "diag1_after_new.png"))

        # inspect the add-media button
        info = await page.evaluate("""() => {
            const els = [...document.querySelectorAll('*')];
            const el = els.find(e => e.childNodes.length &&
                [...e.childNodes].some(n => n.nodeType===3 && n.textContent.includes('添加图片')));
            if (!el) {
                // broader search
                const el2 = els.find(e => e.textContent.includes('添加图片'));
                if (!el2) return {found: false};
                return {found: true, tag: el2.tagName, cls: el2.className,
                        html: el2.outerHTML.slice(0,400), pid: el2.parentElement?.tagName,
                        pcls: el2.parentElement?.className};
            }
            return {found: true, tag: el.tagName, cls: el.className,
                    html: el.outerHTML.slice(0,400), pid: el.parentElement?.tagName,
                    pcls: el.parentElement?.className};
        }""")
        print("\n=== ADD MEDIA BUTTON INFO ===")
        for k, v in info.items():
            print(f"  {k}: {v}")

        # Try to intercept file chooser BEFORE clicking the add-media button
        print("\n=== TEST: expect_file_chooser wrapping add-media click ===")
        try:
            async with page.expect_file_chooser(timeout=5_000) as fc_info:
                await page.locator("text=添加图片").first.click(timeout=5_000)
            fc = await fc_info.value
            print("SUCCESS: file chooser opened directly from add-media click!")
            await fc.set_files(r"D:\Workspace\kg_graph.jpg")
            print("File set via file chooser")
        except Exception as e:
            print(f"No file chooser after add-media click: {e}")
            await page.screenshot(path=str(SHOTS / "diag2_after_add_click.png"))

            # Inspect what appeared after clicking
            new_els = await page.evaluate("""() => {
                const visible = e => {
                    const r = e.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const menus = [...document.querySelectorAll(
                    '[class*=menu],[class*=dropdown],[class*=popup],[class*=panel],[class*=layer],[class*=modal]'
                )];
                return menus.filter(visible).map(e => ({
                    tag: e.tagName, cls: e.className.slice(0,60),
                    text: e.textContent.trim().slice(0,80)
                }));
            }""")
            print("Visible menus/dropdowns after click:", new_els)

            # Look for 图片 option
            photo_option = page.locator("text=图片").first
            if await photo_option.count():
                print("Found '图片' option, trying file chooser around it...")
                try:
                    async with page.expect_file_chooser(timeout=5_000) as fc_info:
                        await photo_option.click(timeout=5_000)
                    fc = await fc_info.value
                    print("SUCCESS: file chooser opened from '图片' option!")
                    await fc.set_files(r"D:\Workspace\kg_graph.jpg")
                    print("File set via file chooser")
                except Exception as e2:
                    print(f"No file chooser from '图片' option: {e2}")

            # Final: find all input[type=file] and their visibility
            inputs_info = await page.evaluate("""() => {
                return [...document.querySelectorAll('input[type=file]')].map(e => ({
                    name: e.name, accept: e.accept,
                    visible: e.getBoundingClientRect().width > 0,
                    display: getComputedStyle(e).display,
                    visibility: getComputedStyle(e).visibility
                }));
            }""")
            print("All input[type=file]:", inputs_info)

        await asyncio.sleep(2)
        await page.screenshot(path=str(SHOTS / "diag3_final.png"))
        print("\nDone. Screenshots saved to", SHOTS)


if __name__ == "__main__":
    asyncio.run(main())
