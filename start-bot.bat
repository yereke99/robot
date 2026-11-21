@echo off
setlocal
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
cd /d "%~dp0" || exit /b 1

set "ATAGOS_LAUNCHER=%~f0"
powershell -NoProfile -Command "$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent()); if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { exit 0 }; Write-Host 'Requesting Administrator... Click Yes'; try { Start-Process -FilePath $env:ATAGOS_LAUNCHER -Verb RunAs -ErrorAction Stop; exit 2 } catch { Write-Error $_; exit 1 }"
set "ATAGOS_ELEVATION_RESULT=%ERRORLEVEL%"
if "%ATAGOS_ELEVATION_RESULT%"=="2" exit /b 0
if not "%ATAGOS_ELEVATION_RESULT%"=="0" (
  echo [start-bot] Could not start the Administrator console.
  set "ATAGOS_EXIT_CODE=1"
  goto :hold
)

title ATAGOS bot (Administrator)
echo.
echo === ATAGOS / ADMIN ===

call "%~dp0deploy\windows\launch-python.bat"
set "ATAGOS_EXIT_CODE=%ERRORLEVEL%"
echo.
echo Bot stopped. Exit code %ATAGOS_EXIT_CODE%.
if not "%ATAGOS_EXIT_CODE%"=="0" echo [start-bot] See the error above before restarting.

:hold
echo Press any key to close...
pause >nul
exit /b %ATAGOS_EXIT_CODE%
