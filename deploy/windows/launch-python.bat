@echo off
setlocal
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
cd /d "%~dp0..\.." || exit /b 1

set "ATAGOS_PYTHON=%CD%\.venv\Scripts\python.exe"
set "ATAGOS_PYTHON_ARGS="
if exist "%ATAGOS_PYTHON%" goto :run

where py.exe >nul 2>&1
if not errorlevel 1 (
    set "ATAGOS_PYTHON=py.exe"
    set "ATAGOS_PYTHON_ARGS=-3"
    goto :run
)
where python.exe >nul 2>&1
if errorlevel 1 (
    echo [launcher] Python 3.11 or newer is required. Install Python for Windows.
    exit /b 1
)
set "ATAGOS_PYTHON=python.exe"

:run
echo Using launcher Python: "%ATAGOS_PYTHON%" %ATAGOS_PYTHON_ARGS%
"%ATAGOS_PYTHON%" %ATAGOS_PYTHON_ARGS% -c "import sys; sys.exit(sys.version_info < (3, 11))"
if errorlevel 1 (
    echo [launcher] Could not run Python 3.11 or newer.
    exit /b 1
)
"%ATAGOS_PYTHON%" %ATAGOS_PYTHON_ARGS% -m goszakup.bot.windows_run %*
exit /b %ERRORLEVEL%
