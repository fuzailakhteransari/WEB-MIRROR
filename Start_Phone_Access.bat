@echo off
setlocal
cd /d "%~dp0"
title WebDesk Remote - Free Phone Access (Cloudflare Tunnel)

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found in PATH.
    pause
    exit /b 1
)

python -m host.launcher phone
if errorlevel 1 (
    echo.
    echo [ERROR] Could not start phone tunnel. Check logs in %USERPROFILE%\.webdesk-remote\
    pause
    exit /b 1
)

echo.
echo Open the HTTPS URL printed above on your phone (or copy it from http://127.0.0.1:8080).
echo Run Stop_WebDesk_Remote.bat whenever you want to close access.
pause
