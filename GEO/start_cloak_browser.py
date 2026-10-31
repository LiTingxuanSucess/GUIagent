# -*- coding: utf-8 -*-
"""
启动 CloakBrowser（stealth Chromium）并暴露 CDP，再打开全部国内 AI 登录页。

首次运行会下载 Cloak Chromium 二进制（约 200MB），请耐心等待。

用法:
  py -3.11 start_cloak_browser.py
  py -3.11 start_cloak_browser.py --port 9242
  py -3.11 start_cloak_browser.py --no-open-tabs   # 仅启动 Cloak CDP
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

GEO_DIR = Path(__file__).resolve().parent
DEFAULT_PROFILE = Path(os.environ.get("LOCALAPPDATA", "")) / "CloakBrowser" / "User Data_CN_AI"


def ensure_cloakbrowser() -> None:
    try:
        import cloakbrowser  # noqa: F401
    except ImportError:
        print("未安装 cloakbrowser，正在安装…")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "cloakbrowser", "-q"],
        )
        print("cloakbrowser 安装完成。")


async def start_cloak_cdp(profile_dir: Path, port: int, headless: bool) -> object:
    from cloakbrowser import launch_async

    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    print(f"Cloak profile: {profile_dir}")
    print(f"CDP port: {port}")
    browser = await launch_async(headless=headless, args=args)
    return browser


def wait_cdp(port: int, timeout: float = 120) -> bool:
    import urllib.request

    url = f"http://127.0.0.1:{port}/json/version"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def open_login_tabs(port: int, sites: str | None) -> None:
    env = os.environ.copy()
    env["CDP_URL"] = f"http://127.0.0.1:{port}"
    env["NO_PROXY"] = "*"
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [sys.executable, str(GEO_DIR / "open_login_sites.py")]
    if sites:
        cmd.extend(["--sites", sites])
    subprocess.run(cmd, cwd=str(GEO_DIR), env=env, check=False)


async def run_async(
    profile_dir: Path,
    port: int,
    headless: bool,
    open_tabs: bool,
    sites: str | None,
) -> None:
    ensure_cloakbrowser()
    browser = await start_cloak_cdp(profile_dir, port, headless)

    if not wait_cdp(port):
        print(f"警告: {port} 端口 CDP 未在 120s 内就绪，仍尝试打开标签页…")

    if open_tabs:
        open_login_tabs(port, sites)

    print()
    print("CloakBrowser 已启动。")
    print(f"  CDP_URL=http://127.0.0.1:{port}")
    print("  登录完成后可执行:")
    print(f"    set CDP_URL=http://127.0.0.1:{port}")
    print("    set SKIP_KILL_CHROME=1")
    print("    py -3.11 batch_scrape_cn.py   # 待实现")
    print()
    print("按 Ctrl+C 结束 Cloak 浏览器…")

    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        print("正在关闭 Cloak…")
        try:
            await browser.close()
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 CloakBrowser + 打开登录页")
    parser.add_argument("--port", type=int, default=9242, help="CDP 端口（默认 9242，与 Chrome 9222 区分）")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE, help="用户数据目录")
    parser.add_argument("--headless", action="store_true", help="无头模式（登录请用默认有界面）")
    parser.add_argument("--no-open-tabs", action="store_true", help="不自动打开各平台标签")
    parser.add_argument("--sites", help="逗号分隔站点 id")
    args = parser.parse_args()

    asyncio.run(
        run_async(
            profile_dir=args.profile,
            port=args.port,
            headless=args.headless,
            open_tabs=not args.no_open_tabs,
            sites=args.sites,
        )
    )


if __name__ == "__main__":
    main()
