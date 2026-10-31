@echo off
REM 可选：从主 Profile 同步 Cookies 到 CDP Profile
REM 主 Profile 登录态更新后可运行此脚本
set "SRC=%LOCALAPPDATA%\Google\Chrome\User Data\Default"
set "DST=%LOCALAPPDATA%\Google\Chrome\User Data_CDP\Default"

if not exist "%SRC%\Cookies" (
  echo Source Cookies not found
  exit /b 1
)

if not exist "%DST%" mkdir "%DST%"
copy /Y "%SRC%\Cookies" "%DST%\" 2>nul
copy /Y "%SRC%\Login Data" "%DST%\" 2>nul
echo Cookies synced. Close Chrome before running if it was open.
