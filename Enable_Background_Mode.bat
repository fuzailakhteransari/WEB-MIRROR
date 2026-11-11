@echo off
setlocal
cd /d "%~dp0"
title WebDesk Remote - Enable Always-On Background Mode

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found in PATH.
    pause
    exit /b 1
)

python -m host.launcher enable-startup --start
if errorlevel 1 (
    echo.
    echo [ERROR] Could not enable background auto-start.
    pause
    exit /b 1
)

echo.
echo [OK] WebDesk Remote is now running silently in the background AND registered for Windows startup!
echo It will auto-start every time you turn on / sign into your PC (with HTTPS Phone Tunnel).
echo Run Disable_Background_Mode.bat anytime to stop and remove auto-start.
pause
