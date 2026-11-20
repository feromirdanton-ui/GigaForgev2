@echo off
:: GigaForge  TUI shortcut - live terminal dashboard
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
"%PY%" -m gigaforge.cli ui --watch --interval 1
endlocal
