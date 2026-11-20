@echo off
:: GigaForge  WEB shortcut - the browser control room on its own
:: (inside the app this is sidebar -> "Browser UI")
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
"%PY%" -m gigaforge.cli web --serve --port 8787 --open
endlocal
