@echo off
REM Run the goszakup Telegram bot on Windows.
REM Updates origin/main, then starts Python with the working machine's .env.
REM Double-click to start, or use as the Task Scheduler trigger for autostart.

REM Switch console codepage to UTF-8 so Russian text in logs doesn't garble.
chcp 65001 >nul
setlocal

REM cd to repo root regardless of where the .bat is double-clicked from.
cd /d "%~dp0..\.." || exit /b 1

call "%~dp0launch-python.bat"
set "ATAGOS_EXIT_CODE=%ERRORLEVEL%"
echo [run-bot] Bot stopped. Exit code %ATAGOS_EXIT_CODE%.

pause
exit /b %ATAGOS_EXIT_CODE%
