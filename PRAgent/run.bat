@echo off
REM PRAgent - Google News scraping
REM Set DASHSCOPE_API_KEY before running, or put api_key in config.yaml
set DASHSCOPE_API_KEY=%DASHSCOPE_API_KEY%
python main.py %*
