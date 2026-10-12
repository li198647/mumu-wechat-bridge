@echo off
chcp 936 >nul
cd /d "%~dp0tools"

set PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe
if not exist "%PY%" set PY=python

echo ============================================
echo   WeChat Bridge  /  Weixin Qiao
echo ============================================
echo.
echo Starting... (a browser window may pop up for QR login)
echo Close this window to stop? No - use the tray icon menu to quit.
echo.

"%PY%" wx_bridge.py

echo.
echo Bridge stopped.
pause
