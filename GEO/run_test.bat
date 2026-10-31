@echo off
chcp 65001 >nul
cd /d "%~dp0"

set CDP_URL=http://127.0.0.1:9222
echo CDP_URL=%CDP_URL%
echo.
echo 确保 Chrome 已用 start_chrome.bat 启动且 9222 在监听
echo 在 Cursor 内运行可能连不上 localhost，请用本 bat 在资源管理器双击运行
echo.
py -3.11 test.py
pause
