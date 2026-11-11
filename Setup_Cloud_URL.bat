@echo off
setlocal
cd /d "%~dp0"
title WebDesk Remote - Connect to Permanent Cloud URL (Zero-Scan Setup)

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found in PATH.
    pause
    exit /b 1
)

echo ==============================================================
echo   WebDesk Remote - Permanent Cloud URL Setup (Zero-Scan)
echo ==============================================================
echo.
echo Paste your Render URL (example: https://webdesk-remote.onrender.com)
echo Or type LOCAL to reset back to local + Cloudflare Quick Tunnel mode.
echo.
set /p CLOUD_URL="Fixed Cloud URL: "
if /i "%CLOUD_URL%"=="LOCAL" (
    python -m host.launcher set-cloud --local
    pause
    exit /b %errorlevel%
)

set /p WS_TOKEN="Workspace Token (press Enter to skip if not set on Render): "
if "%WS_TOKEN%"=="" (
    python -m host.launcher set-cloud --url "%CLOUD_URL%"
) else (
    python -m host.launcher set-cloud --url "%CLOUD_URL%" --token "%WS_TOKEN%"
)

if errorlevel 1 (
    echo.
    echo [ERROR] Setup failed. Check the URL and try again.
    pause
    exit /b 1
)

echo.
echo [OK] Your Windows PC is now auto-starting on boot and connecting to your fixed URL!
echo Bookmark that URL on your phone once - never scan a QR code again.
pause
