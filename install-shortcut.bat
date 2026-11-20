@echo off
:: =====================================================================
::  GigaForge  Install shortcut
::
::  Creates "GigaForge" on the Desktop and in the Start Menu, pointing at
::  pythonw -m gigaforge.desktop with the generated icon, so the harness
::  launches like any other application.
::
::    install-shortcut.bat            install
::    install-shortcut.bat /remove    remove the shortcuts
:: =====================================================================
setlocal EnableDelayedExpansion
chcp 65001 >nul

set "GF_ROOT=%~dp0"
if "%GF_ROOT:~-1%"=="\" set "GF_ROOT=%GF_ROOT:~0,-1%"

:: --- windowed interpreter --------------------------------------------
set "GF_PYTHON=python"
if exist "%GF_ROOT%\.venv\Scripts\python.exe" set "GF_PYTHON=%GF_ROOT%\.venv\Scripts\python.exe"
set "GF_PYTHONW=!GF_PYTHON:python.exe=pythonw.exe!"
if /i "!GF_PYTHON!"=="python" set "GF_PYTHONW=pythonw"
if /i not "!GF_PYTHONW!"=="pythonw" (
    if not exist "!GF_PYTHONW!" set "GF_PYTHONW=!GF_PYTHON!"
) else (
    where pythonw >nul 2>&1 || set "GF_PYTHONW=!GF_PYTHON!"
)

echo   pythonw : !GF_PYTHONW!
echo   root    : %GF_ROOT%
echo.

:: --- icon -------------------------------------------------------------
if not exist "%GF_ROOT%\assets\gigaforge.ico" (
    echo   [..] icon missing - generating it
    "!GF_PYTHON!" "%GF_ROOT%\scripts\make_icon.py"
)

:: --- shortcuts --------------------------------------------------------
set "GF_FLAG="
if /i "%~1"=="/remove" set "GF_FLAG=-Uninstall"
if /i "%~1"=="--remove" set "GF_FLAG=-Uninstall"

powershell -NoProfile -ExecutionPolicy Bypass -File "%GF_ROOT%\scripts\install_shortcut.ps1" -Root "%GF_ROOT%" -PythonW "!GF_PYTHONW!" !GF_FLAG!
set "GF_RC=!errorlevel!"

echo.
if not "!GF_RC!"=="0" (
    echo   [X] shortcut install failed with exit code !GF_RC!
) else (
    echo   Done.
)
pause
exit /b !GF_RC!
