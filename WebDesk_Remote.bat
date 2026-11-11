@echo off
setlocal
cd /d "%~dp0"

where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw -m host.desktop_app
    exit /b 0
)

where python >nul 2>nul
if not errorlevel 1 (
    start "" python -m host.desktop_app
    exit /b 0
)

echo [ERROR] Python was not found in PATH.
pause
exit /b 1
