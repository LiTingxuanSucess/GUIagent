@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "CDP_URL=http://127.0.0.1:9222"
set "SKIP_KILL_CHROME=1"
set "NO_PROXY=*"

REM Ensure Chrome with CDP is running (user should start manually or run start_chrome_cdp.bat first)
echo Run all sites: kimi, chatgpt, perplexity, gemini
echo Keyword: best GaN charger for MacBook Pro and iPhone travel
echo.
echo Make sure Chrome is running with CDP (port 9222) and you are logged in to all sites.
echo.
pause

for %%S in (kimi chatgpt perplexity gemini) do (
  echo.
  echo ========== Running site: %%S ==========
  set SITE=%%S
  py -3.11 test.py --site %%S
  if errorlevel 1 (
    echo [ERROR] Site %%S failed
  )
  echo.
  timeout /t 3 /nobreak >nul
)

echo.
echo ========== All sites done ==========
pause
