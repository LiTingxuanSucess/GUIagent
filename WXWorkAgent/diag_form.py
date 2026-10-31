"""Diagnose form element class names (image items, select-customer dialog)."""
from __future__ import annotations
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

SHOTS = Path(__file__).parent / "shots" / "pengyouquan"
TARGET = "https://work.weixin.qq.com/wework_admin/frame#/customer/config/groupSend"


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9223")
        ctx = browser.contexts[0]
        pages = ctx.pages
        page = next((pg for pg in pages if "work.weixin" in pg.url), pages[0])

        await page.goto(TARGET, wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_load_state("networkidle", timeout=15_000)
        await asyncio.sleep(1)

        # Remove stale masks via JS
        await page.evaluate("""() => {
            document.querySelectorAll('.js_dialog_mask,.ww_dialog').forEach(e => e.remove());
        }""")

        # Click 新建内容
        await page.locator("text=新建内容").first.click(timeout=10_000)
        await asyncio.sleep(1.5)

        # --- Find image items ---
        img_info = await page.evaluate("""() => {
            // Look for the image preview area
            const allDivs = [...document.querySelectorAll('div,li,span')];
            const candidates = allDivs.filter(e => {
                const r = e.getBoundingClientRect();
                return r.width > 40 && r.height > 40 && r.width < 200 && r.height < 200;
            });
            // Find elements that contain img src or are the preview thumbnails
            const imgs = [...document.querySelectorAll('img')].filter(e => {
                const r = e.getBoundingClientRect();
                return r.width > 30 && r.width < 200;
            });
            const parents = imgs.map(img => {
                let p = img.parentElement;
                while(p && p.className === '') p = p.parentElement;
                return {cls: p?.className || '', tag: p?.tagName || ''};
            });
            return {imgs: imgs.length, parents: parents.slice(0, 5)};
        }""")
        print("Image items info:", img_info)

        # More focused search for delete buttons
        del_info = await page.evaluate("""() => {
            const items = [...document.querySelectorAll('[class*=imgItem],[class*=preview]')];
            return items.map(e => ({cls: e.className, tag: e.tagName,
                text: e.textContent.trim().slice(0,30),
                visible: e.getBoundingClientRect().width > 0}));
        }""")
        print("Preview/imgItem elements:", del_info)

        await page.screenshot(path=str(SHOTS / "diag_form1.png"))

        # --- Click 选择客户 and wait for dialog ---
        print("\n=== Clicking 选择客户 ===")
        cust_link = page.get_by_text("选择客户", exact=True).first
        cnt = await cust_link.count()
        print(f"'选择客户' elements found: {cnt}")
        if cnt:
            await cust_link.click(timeout=5_000)
            await asyncio.sleep(1.5)
            await page.screenshot(path=str(SHOTS / "diag_form2_after_cust_click.png"))

            # Check for radio inputs
            radios = await page.locator("input[type='radio']").count()
            print(f"Radio buttons found: {radios}")

            # Check for 公开 text
            pub_els = await page.evaluate("""() => {
                return [...document.querySelectorAll('*')].filter(e =>
                    e.childNodes.length &&
                    [...e.childNodes].some(n => n.nodeType===3 && n.textContent.trim()==='公开')
                ).map(e => ({tag:e.tagName, cls:e.className.slice(0,60),
                    visible: e.getBoundingClientRect().width>0}));
            }""")
            print("Elements with exact '公开' text:", pub_els)

            # Check dialog HTML
            dlg_html = await page.evaluate("""() => {
                const dlg = document.querySelector('.ww_dialog, [class*=dialog], [class*=modal]');
                return dlg ? dlg.outerHTML.slice(0, 600) : 'NOT FOUND';
            }""")
            print("Dialog HTML:", dlg_html[:300])


if __name__ == "__main__":
    asyncio.run(main())
