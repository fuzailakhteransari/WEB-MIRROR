@echo off
setlocal
cd /d "%~dp0"
title WebDesk Remote - Instant Launcher

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found in PATH.
    pause
    exit /b 1
)

python -m host.launcher start
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to launch WebDesk Remote. See logs above.
    pause
    exit /b 1
)

echo.
echo Portal opened in your browser. Run Stop_WebDesk_Remote.bat to shut down background services.
timeout /t 3 /nobreak >nul
