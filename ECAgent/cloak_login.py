from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from cloakbrowser import launch_persistent_context_async

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


LOGIN_URLS = {
    "douyin": "https://leads.cluerich.com/pc/cs/chat/session",
    "taobao": "https://login.taobao.com/",
    "tmall": "https://login.tmall.com/",
    "jd": "https://passport.jd.com/new/login.aspx",
}


async def main_async() -> int:
    parser = argparse.ArgumentParser(description="Start CloakBrowser with a persistent EC login profile.")
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / ".cloak-profiles" / "ec-durex",
    )
    parser.add_argument("--sites", default="taobao,tmall,jd", help="Comma-separated: douyin,taobao,tmall,jd")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=1000)
    parser.add_argument("--cdp-port", type=int, default=0, help="Expose a Chrome DevTools Protocol port for external agents.")
    args = parser.parse_args()

    sites = [x.strip().lower() for x in args.sites.split(",") if x.strip()]
    urls = [LOGIN_URLS[x] for x in sites if x in LOGIN_URLS]
    if not urls:
        raise ValueError(f"no supported sites in --sites={args.sites!r}")

    args.profile_dir.mkdir(parents=True, exist_ok=True)
    print(f">>> CloakBrowser profile: {args.profile_dir}", flush=True)
    print(">>> Opening login pages. Log in manually, then leave this window open.", flush=True)

    browser_args = [
        f"--window-size={args.width},{args.height}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if args.cdp_port:
        browser_args.append(f"--remote-debugging-port={args.cdp_port}")

    context = await launch_persistent_context_async(
        args.profile_dir,
        headless=False,
        humanize=True,
        human_preset="careful",
        viewport=None,
        locale="zh-CN",
        timezone="Asia/Shanghai",
        args=browser_args,
    )
    try:
        first = True
        for url in urls:
            page = context.pages[0] if first and context.pages else await context.new_page()
            first = False
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            print(f">>> opened: {url}", flush=True)
            await page.wait_for_timeout(1000)
        print(">>> Ready. Press Ctrl+C in this terminal after login if you want to close it.", flush=True)
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        print(">>> closing CloakBrowser", flush=True)
        return 0
    finally:
        await context.close()


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
