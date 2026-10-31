@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "NO_PROXY=*"
set "PYTHONIOENCODING=utf-8"

echo ============================================================
echo  国内 AI 平台 — 一次性登录（CloakBrowser stealth CDP）
echo  端口: 9242（与 Chrome 9222 区分）
echo ============================================================
echo.
echo 首次运行会下载 Cloak Chromium，可能需要数分钟。
echo.

py -3.11 start_cloak_browser.py --port 9242
