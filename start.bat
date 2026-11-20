@echo off
:: =====================================================================
::  GigaForge  Start
::
::  Launches the GigaForge DESKTOP APPLICATION - a standalone window,
::  like any other program.  No browser, no terminal required.
::
::  What this script does before the window appears:
::    1. find Python (3.11+)
::    2. prefer a local .venv when one exists
::    3. first-time editable install if `gigaforge` is not importable
::    4. bootstrap .env from env.example
::    5. start the window with pythonw (no console window left behind)
::
::  The browser control room is a FEATURE of that window (sidebar ->
::  "Browser UI"), not a separate program.  The terminal launcher with the
::  interactive mode menu is menu.bat.
:: =====================================================================
setlocal EnableDelayedExpansion
chcp 65001 >nul

set "GF_ROOT=%~dp0"
if "%GF_ROOT:~-1%"=="\" set "GF_ROOT=%GF_ROOT:~0,-1%"
set "GF_ARGS=%*"
set "GF_UI=qml"
echo %GF_ARGS% | findstr /i /c:"--ui tk" /c:"--ui=tk" >nul
if not errorlevel 1 set "GF_UI=tk"

echo.
echo   === GigaForge Start ===
echo.

:: --- 1. python --------------------------------------------------------
where python >nul 2>&1
if errorlevel 1 (
    echo   [X] Python not found on PATH.
    echo       Install Python 3.11+ from https://python.org/downloads/
    echo       Tick "Add Python to PATH" during the install.
    echo.
    pause
    exit /b 1
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 (
    echo   [X] Python 3.11 or newer is required by this GigaForge version.
    echo       Install Python 3.11+ from https://python.org/downloads/
    echo.
    pause
    exit /b 1
)
for /f "tokens=1" %%v in ('python -c "import sys; print(sys.version.split()[0])"') do set "GF_PYVER=%%v"
echo   [OK] python !GF_PYVER!

:: --- 2. venv ----------------------------------------------------------
set "GF_PYTHON=python"
if exist "%GF_ROOT%\.venv\Scripts\python.exe" (
    set "GF_PYTHON=%GF_ROOT%\.venv\Scripts\python.exe"
    echo   [OK] venv  !GF_PYTHON!
) else (
    echo   [..] creating an isolated project environment
    python -m venv "%GF_ROOT%\.venv"
    if errorlevel 1 (
        echo   [!] could not create .venv - falling back to system python
    ) else (
        set "GF_PYTHON=%GF_ROOT%\.venv\Scripts\python.exe"
        echo   [OK] created venv  !GF_PYTHON!
    )
)

:: Derive the windowed interpreter from the chosen python by name substitution,
:: then fall back to a console build if pythonw is genuinely absent.  Guessing a
:: path here is how a launcher ends up showing a modal "file not found" dialog and
:: hanging forever.
set "GF_PYTHONW=!GF_PYTHON:python.exe=pythonw.exe!"
if /i "!GF_PYTHON!"=="python" set "GF_PYTHONW=pythonw"
if /i not "!GF_PYTHONW!"=="pythonw" (
    if not exist "!GF_PYTHONW!" set "GF_PYTHONW=!GF_PYTHON!"
) else (
    where pythonw >nul 2>&1 || set "GF_PYTHONW=!GF_PYTHON!"
)

:: --- 3. package -------------------------------------------------------
!GF_PYTHON! -c "import gigaforge" >nul 2>&1
if errorlevel 1 (
    echo   [..] gigaforge not importable - first-time pip install -e .
    !GF_PYTHON! -m pip install -e "%GF_ROOT%" --quiet
    if errorlevel 1 (
        echo   [X] pip install failed. Check your network or run as Administrator.
        pause
        exit /b 1
    )
    echo   [OK] gigaforge installed
) else (
    echo   [OK] gigaforge importable
)

:: Check the full declared runtime set. This repairs editable installs created
:: before a dependency was added to the base package.
!GF_PYTHON! -c "import httpx, pydantic, typer, rich, tree_sitter, tree_sitter_python, tree_sitter_javascript, requests, playwright, pytest" >nul 2>&1
if errorlevel 1 (
    echo   [..] runtime dependency missing - updating GigaForge dependencies
    !GF_PYTHON! -m pip install -e "%GF_ROOT%" --quiet
    if errorlevel 1 (
        echo   [!] dependency update failed; the app will still open and retry setup at startup
    ) else (
        echo   [OK] runtime dependencies installed
    )
)

:: --- 4. .env ----------------------------------------------------------
if exist "%GF_ROOT%\.env" (
    echo   [OK] .env found
) else (
    if exist "%GF_ROOT%\env.example" (
        copy /Y "%GF_ROOT%\env.example" "%GF_ROOT%\.env" >nul
        echo   [..] .env created - add GIGAFORGE_AUTH_KEY to enable runs ^(the window still opens^)
    )
)

:: --- 5. selected UI ---------------------------------------------------
if /i "%GF_UI%"=="qml" goto qml_runtime
!GF_PYTHON! -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo   [X] this Python has no Tkinter, so the desktop window cannot open.
    echo       Install the full python.org build, or use:  web.bat  ^(browser UI^)
    echo.
    pause
    exit /b 1
)
echo   [OK] tkinter available
goto launch_window

:qml_runtime
echo   [..] preparing the Qt Quick desktop surface
!GF_PYTHON! -c "from gigaforge.runtime_dependencies import ensure_qt_quick_runtime; raise SystemExit(0 if ensure_qt_quick_runtime(auto_install=False).available else 1)" >nul 2>&1
if errorlevel 1 (
    echo   [..] Qt Quick is missing - installing the desktop runtime
    !GF_PYTHON! -m pip install "PySide6>=6.8,<7" --quiet
    if errorlevel 1 (
        echo   [X] Qt Quick installation failed. Check network access and Python permissions.
        pause
        exit /b 1
    )
    echo   [OK] Qt Quick runtime installed
) else (
    echo   [OK] Qt Quick runtime already available
)
!GF_PYTHON! -c "from gigaforge.runtime_dependencies import ensure_qt_quick_runtime; s=ensure_qt_quick_runtime(auto_install=False); raise SystemExit(0 if s.available else 1)" >nul 2>&1
if errorlevel 1 (
    echo   [X] Qt Quick is still unavailable after setup.
    pause
    exit /b 1
)
echo   [OK] Qt Quick available

:: --- 6. launch the window --------------------------------------------
:launch_window
echo.
echo   Starting the GigaForge window with !GF_PYTHONW!
cd /d "%GF_ROOT%"
start "GigaForge" "!GF_PYTHONW!" -m gigaforge.desktop %GF_ARGS%
:: Give a console build a moment to fail loudly before this window closes.
if /i "!GF_PYTHONW!"=="!GF_PYTHON!" timeout /t 3 >nul
exit /b 0
