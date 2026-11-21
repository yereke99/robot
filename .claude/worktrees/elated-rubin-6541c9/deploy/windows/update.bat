@echo off
REM Pull the latest code + update Python deps. Run after every release.
REM Stops the bot first if it's running via the run-bot.bat console window.

chcp 65001 >nul
setlocal
cd /d "%~dp0..\.."

echo.
echo === git pull ===
git pull --rebase --autostash
if errorlevel 1 (
    echo [update] git pull failed. Resolve conflicts then re-run.
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
