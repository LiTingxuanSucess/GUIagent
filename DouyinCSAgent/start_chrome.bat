@echo off
REM 用已有的 Chrome 用户数据（保留登录态），以 CDP 调试端口启动。
REM 首次运行会打开新 Chrome 窗口，手动登录 leads.cluerich.com 后即可使用。

set "PORT=9222"
set "URL=https://leads.cluerich.com/pc/cs/chat/session"
REM Chrome 136+ blocks remote debugging against the default user data dir.
REM Use a dedicated automation profile; log in once in this window.
set "USER_DATA=%~dp0.chrome-cdp-profile"

set "CHROME="
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
  set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
)
if not defined CHROME if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" (
  set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
)
if not defined CHROME (
  echo 未找到 chrome.exe，请安装 Google Chrome 或编辑本脚本中的路径。
  exit /b 1
)

echo 启动 Chrome (CDP 端口 %PORT%)...
echo   可执行文件: %CHROME%
echo   用户数据:   %USER_DATA%

start "" "%CHROME%" ^
  --remote-debugging-port=%PORT% ^
  --user-data-dir="%USER_DATA%" ^
  --no-first-run ^
  --no-default-browser-check ^
  %URL%

echo Chrome 已启动，等待页面加载并登录后，运行: py -3.11 douyin_watch.py
