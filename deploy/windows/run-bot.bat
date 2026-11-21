@echo off
REM Run the goszakup Telegram bot on Windows.
REM Loads .env (TG_BOT_TOKEN + ECP/ACCOUNT passwords) and execs python.
REM Double-click to start, or use as the Task Scheduler trigger for autostart.

REM Switch console codepage to UTF-8 so Russian text in logs doesn't garble.
chcp 65001 >nul
setlocal
set "ATAGOS_EXIT_CODE=1"

REM cd to repo root regardless of where the .bat is double-clicked from.
cd /d "%~dp0..\.." || exit /b 1

REM Sanity checks — surface the most common first-run mistakes early.
if not exist ".venv\Scripts\python.exe" (
    echo [run-bot] virtualenv missing — run install.ps1 first.
    goto :hold
)
if not exist "config.yaml" (
    echo [run-bot] config.yaml missing — copy config.example.yaml and edit it.
    goto :hold
)
if not exist ".env" (
    echo [run-bot] .env missing — copy deploy\windows\.env.example to .env and fill in.
    goto :hold
)

REM Python's stdio uses cp1251/cp866 by default on Windows; force UTF-8.
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

REM python-dotenv (loaded inside goszakup.bot.main) parses the .env itself.
".venv\Scripts\python.exe" -m goszakup.bot.main
set "ATAGOS_EXIT_CODE=%ERRORLEVEL%"
echo [run-bot] Bot stopped. Exit code %ATAGOS_EXIT_CODE%.

:hold
pause
exit /b %ATAGOS_EXIT_CODE%
