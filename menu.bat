@echo off
:: =====================================================================
::  GigaForge  Terminal menu
::
::  The cyber menu (banner + numbered actions) for people who
::  prefer the terminal.  The primary entry point is start.bat, which
::  opens the desktop window.
:: =====================================================================
setlocal EnableDelayedExpansion
chcp 65001 >nul

set "GF_ROOT=%~dp0"
if "%GF_ROOT:~-1%"=="\" set "GF_ROOT=%GF_ROOT:~0,-1%"

set "GF_PYTHON=python"
if exist "%GF_ROOT%\.venv\Scripts\python.exe" set "GF_PYTHON=%GF_ROOT%\.venv\Scripts\python.exe"

cd /d "%GF_ROOT%"
"!GF_PYTHON!" "%GF_ROOT%\cyber_ui.py" repl
set "GF_RC=!errorlevel!"
exit /b !GF_RC!
