@echo off
REM Update the checkout and Python dependencies after stopping the bot.

chcp 65001 >nul
setlocal
cd /d "%~dp0..\.."

echo.
echo === updating origin/main and Python dependencies ===
call "%~dp0launch-python.bat" --update-only
if errorlevel 1 (
    echo [update] Update failed. Review the message above before retrying.
    pause
    exit /b 1
)

echo.
echo Update done. Restart the bot to apply:
echo   - If running in console: close the run-bot.bat window, then re-run it.
echo   - If running via Task Scheduler: schtasks /End /TN "GoszakupBot" then /Run.
echo.
pause
