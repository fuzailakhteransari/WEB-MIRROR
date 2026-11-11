@echo off
setlocal
cd /d "%~dp0"
title Stop WebDesk Remote

python -m host.launcher stop
timeout /t 2 /nobreak >nul
