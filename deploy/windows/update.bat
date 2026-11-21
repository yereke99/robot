@echo off
REM Update the checkout and Python dependencies after stopping the bot.

chcp 65001 >nul
setlocal
cd /d "%~dp0..\.."

echo.
echo === git pull --ff-only origin main ===
".venv\Scripts\python.exe" -m goszakup.bot.windows_run --update-only
if errorlevel 1 (
    echo [update] Update failed. Review the message above before retrying.
    pause
    exit /b 1
)

echo.
echo === installing/updating Python deps ===
.venv\Scripts\pip.exe install -r requirements.txt
if errorlevel 1 (
    echo [update] pip install failed. Check the message above.
    pause
    exit /b 1
)

echo.
echo === smoke check (just imports) ===
.venv\Scripts\python.exe -c "from goszakup.bot import main; print('imports OK')"
if errorlevel 1 (
    echo [update] post-update import check failed.
    pause
    exit /b 1
)

echo.
echo Update done. Restart the bot to apply:
echo   - If running in console: close the run-bot.bat window, then re-run it.
echo   - If running via Task Scheduler: schtasks /End /TN "GoszakupBot" then /Run.
echo.
pause
