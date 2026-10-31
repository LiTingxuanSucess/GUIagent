@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
REM 首次使用先运行: py -3.11 login_chatgpt.py
py -3.11 test.py
pause
