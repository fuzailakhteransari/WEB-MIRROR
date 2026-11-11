@echo off
setlocal
cd /d "%~dp0"
title WebDesk Remote - Share Screen (Remote Support)

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found in PATH.
    pause
    exit /b 1
)

python -m host.launcher share
pause
