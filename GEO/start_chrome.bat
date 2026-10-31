@echo off
REM 手动启动带 CDP 的 Chrome，供 test.py 连接
REM 优先 x86 路径
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"

REM 设置 SKIP_KILL=1 可跳过 kill，保留已有 Chrome 登录态（需先手动关闭 Chrome 再启动）
if "%SKIP_KILL%"=="1" (
  echo SKIP_KILL=1, skip taskkill
) else (
  taskkill /F /IM chrome.exe 2>nul
  ping 127.0.0.1 -n 3 >nul 2>&1
)

start "" "%CHROME%" ^
  --remote-debugging-port=9222 ^
  --user-data-dir="%LOCALAPPDATA%\Google\Chrome\User Data" ^
  --profile-directory=Default ^
  --no-first-run ^
  --no-default-browser-check ^
  https://kimi.moonshot.cn

echo Chrome started. Test CDP: curl http://127.0.0.1:9222/json/version
echo Then run: set CDP_URL=http://127.0.0.1:9222 ^&^& py -3.11 test.py
