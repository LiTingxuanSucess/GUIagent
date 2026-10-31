@echo off
REM Launch Chrome with CDP debug port 9223 for WeCom admin automation.
REM First run: log in to work.weixin.qq.com in the opened window.
REM Login state is saved in .chrome-admin-profile directory.

set "PORT=9223"
set "URL=https://work.weixin.qq.com/wework_admin/frame"
set "USER_DATA=%~dp0.chrome-admin-profile"

set "CHROME="
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
  set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
)
if not defined CHROME if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" (
  set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
)
if not defined CHROME (
  echo Chrome not found. Install Google Chrome or edit the path in this script.
  exit /b 1
)

echo Starting Chrome (CDP port %PORT%)...
echo   Executable : %CHROME%
echo   User data  : %USER_DATA%

start "" "%CHROME%" ^
  --remote-debugging-port=%PORT% ^
  --user-data-dir="%USER_DATA%" ^
  --no-first-run ^
  --no-default-browser-check ^
  %URL%

echo Chrome started. Log in to WeCom admin, then run:
echo   py -3.11 wxwork_pengyouquan.py
