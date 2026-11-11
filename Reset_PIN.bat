@echo off
setlocal
cd /d "%~dp0"
title WebDesk Remote - Reset PIN

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found in PATH.
    pause
    exit /b 1
)

python -m host.launcher reset-pin
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to reset PIN.
    pause
    exit /b 1
)

echo.
echo New PIN saved and portal opened.
timeout /t 3 /nobreak >nul
