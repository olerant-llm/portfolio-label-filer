@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "LABEL_FILER_PYTHON=.venv\Scripts\python.exe"
    goto CHECK_VERSION
)

if exist "python_portable\.setup_complete" (
    set "LABEL_FILER_PYTHON=python_portable\python.exe"
    goto CHECK_VERSION
)

echo Label Filer first-time setup...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
if errorlevel 1 (
    echo Setup failed. Check the internet connection and try again.
    pause
    exit /b 1
)
set "LABEL_FILER_PYTHON=python_portable\python.exe"

:CHECK_VERSION
"%LABEL_FILER_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 14) else 1)"
if errorlevel 1 (
    echo Label Filer requires Python 3.14 components.
    pause
    exit /b 1
)

echo Preparing the image recognition model. The first download is about 1 GB.
"%LABEL_FILER_PYTHON%" app\main.py
if errorlevel 1 (
    echo Label Filer exited with an error.
    pause
    exit /b 1
)

endlocal
