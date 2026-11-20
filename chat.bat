@echo off
:: GigaForge  CHAT shortcut - agentic REPL in the terminal
:: (the primary interface is the desktop window: start.bat)
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
"%PY%" -m gigaforge.cli chat
endlocal
