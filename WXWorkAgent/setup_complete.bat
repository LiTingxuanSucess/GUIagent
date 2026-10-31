@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/2] pip install...
py -3.11 -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
echo.
echo [2/2] 预下载 OCR 模型（首次较慢，请等待结束）...
echo 若曾多开 Python 卡死，先关掉所有 python 后执行:
echo   py -3.11 prefetch_ocr.py --clear-paddlex-locks
set HF_ENDPOINT=https://hf-mirror.com
py -3.11 prefetch_ocr.py
if errorlevel 1 exit /b 1
echo.
echo 全部就绪。启动监听:
echo   py -3.11 wxwork_watch.py --contact "联系人"
