@echo off
:: GigaForge  PANEL shortcut - render a portable HTML dashboard file
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
"%PY%" -m gigaforge.cli web --out "%~dp0gigaforge-panel.html"
if exist "%~dp0gigaforge-panel.html" (
    echo Opening the panel in the default browser...
    explorer "%~dp0gigaforge-panel.html"
)
endlocal
