@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "CDP_USER_DATA=%LOCALAPPDATA%\Google\Chrome\User Data_CDP"
set "CDP_URL=http://127.0.0.1:9222"
set "NO_PROXY=*"
set "PYTHONIOENCODING=utf-8"

echo ============================================================
echo  CN AI platforms login - Chrome CDP
echo  DeepSeek / Doubao / Yiyan / Yuanbao / Qianwen / Kimi
echo ============================================================
echo.

netstat -ano 2>nul | findstr "9222" | findstr "LISTENING" >nul 2>&1
if %errorlevel% equ 0 (
  echo Chrome CDP already on 9222, opening tabs...
  goto open_tabs
)

echo Killing old Chrome...
taskkill /F /IM chrome.exe 2>nul
ping -n 3 127.0.0.1 >nul 2>&1

set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"

echo Starting Chrome profile: %CDP_USER_DATA%
start "" "%CHROME%" --remote-debugging-port=9222 --user-data-dir="%CDP_USER_DATA%" --no-first-run --no-default-browser-check --disable-extensions --disable-sync https://chat.deepseek.com

echo Waiting for CDP...
:wait_loop
netstat -ano 2>nul | findstr "9222" | findstr "LISTENING" >nul 2>&1
if %errorlevel% equ 0 goto open_tabs
ping -n 2 127.0.0.1 >nul 2>&1
goto wait_loop

:open_tabs
py -3.11 open_login_sites.py --cdp-url %CDP_URL%

echo.
echo Login in browser, then run batch_scrape_cn.py
pause
