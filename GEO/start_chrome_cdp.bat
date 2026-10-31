@echo off
setlocal
chcp 65001 >nul

REM 专用 CDP Profile，Chrome 可开启 remote-debugging-port
set "CDP_USER_DATA=%LOCALAPPDATA%\Google\Chrome\User Data_CDP"

REM 优先 x86 路径
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"

REM 不 kill，保留登录态。若 9222 已在监听则跳过
netstat -ano 2>nul | findstr "9222" | findstr "LISTENING" >nul 2>&1
if %errorlevel% equ 0 (
  echo Chrome CDP already running on 9222, skip start
  goto :end
)

echo Starting Chrome with CDP profile: %CDP_USER_DATA%
echo.
echo 首次使用需要手动登录 Kimi！
echo 登录后关闭 Chrome，下次即可自动复用
echo.

start "" "%CHROME%" ^
  --remote-debugging-port=9222 ^
  --user-data-dir="%CDP_USER_DATA%" ^
  --no-first-run ^
  --no-default-browser-check ^
  --disable-extensions ^
  --disable-sync ^
  https://kimi.moonshot.cn

echo Chrome started. Test CDP: curl http://127.0.0.1:9222/json/version
echo Then run: set CDP_URL=http://127.0.0.1:9222
echo py -3.11 test.py
echo.
:end
pause
