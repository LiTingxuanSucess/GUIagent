@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "CDP_USER_DATA=%LOCALAPPDATA%\Google\Chrome\User Data_CDP"
set "CDP_URL=http://127.0.0.1:9222"

REM SITE env: kimi|chatgpt|perplexity|gemini - sets initial URL and test target
if "%SITE%"=="" set "SITE=kimi"
set "INIT_URL=https://kimi.moonshot.cn"
if "%SITE%"=="chatgpt" set "INIT_URL=https://chat.openai.com"
if "%SITE%"=="perplexity" set "INIT_URL=https://www.perplexity.ai"
if "%SITE%"=="gemini" set "INIT_URL=https://gemini.google.com"

REM 先 kill 旧进程，避免 CDP 连接卡死（登录态在 User Data_CDP 磁盘中）
echo Killing old Chrome processes...
taskkill /F /IM chrome.exe 2>nul
ping -n 3 127.0.0.1 >nul 2>&1

echo Starting Chrome with CDP profile: %CDP_USER_DATA%
echo Initial URL: %INIT_URL% (SITE=%SITE%)
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
start "" "%CHROME%" --remote-debugging-port=9222 --user-data-dir="%CDP_USER_DATA%" --no-first-run --no-default-browser-check --disable-extensions --disable-sync %INIT_URL%
echo Waiting 12s for Chrome to start...
ping -n 13 127.0.0.1 >nul 2>&1

REM 等待 Chrome CDP 就绪（若刚启动）
:wait_loop
netstat -ano 2>nul | findstr "9222" | findstr "LISTENING" >nul 2>&1
if %errorlevel% equ 0 goto :run_test
ping -n 2 127.0.0.1 >nul 2>&1
goto :wait_loop

:run_test
py -3.11 test.py --site %SITE%
