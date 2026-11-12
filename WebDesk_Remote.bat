@echo off
setlocal
cd /d "%~dp0"

set "PYTHON="
where python >nul 2>nul
if not errorlevel 1 set "PYTHON=python"
if not defined PYTHON (
    for /d %%D in ("%LocalAppData%\Programs\Python\Python*") do (
        if exist "%%~fD\python.exe" set "PYTHON=%%~fD\python.exe"
    )
)

if not defined PYTHON (
    echo [ERROR] Python 3.12+ was not found.
    echo Install it from https://www.python.org/downloads/windows/ then run this file again.
    pause
    exit /b 1
)

set "VENV_PY=%CD%\.venv\Scripts\python.exe"
set "VENV_PYW=%CD%\.venv\Scripts\pythonw.exe"
set "VENV_REQUIREMENTS=%CD%\.venv\requirements.txt"
set "NEED_SETUP="

if not exist "%VENV_PYW%" set "NEED_SETUP=1"
if not exist "%VENV_REQUIREMENTS%" set "NEED_SETUP=1"
if not defined NEED_SETUP (
    fc /b requirements.txt "%VENV_REQUIREMENTS%" >nul
    if errorlevel 1 set "NEED_SETUP=1"
)

if defined NEED_SETUP (
    echo.
    echo ============================================================
    echo   WebDesk Remote first-time setup
    echo   Creating a private Python environment and installing files...
    echo ============================================================
    echo.
    "%PYTHON%" -m venv .venv
    if errorlevel 1 goto :setup_failed
    "%VENV_PY%" -m pip install --upgrade pip
    if errorlevel 1 goto :setup_failed
    "%VENV_PY%" -m pip install -r requirements.txt
    if errorlevel 1 goto :setup_failed
    copy /y requirements.txt "%VENV_REQUIREMENTS%" >nul
)

start "" "%VENV_PYW%" -m host.desktop_app --start-background
exit /b 0

:setup_failed
echo.
echo [ERROR] WebDesk Remote setup did not finish.
echo Check your internet connection, then run this file again.
pause
exit /b 1
