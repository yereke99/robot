# One-shot installer for the goszakup bot on a fresh Windows 10/11 box.
#
# Two ways to run:
#
# A) Repo not yet cloned (private repo — Git will prompt for GitHub login):
#       Set-ExecutionPolicy -Scope Process Bypass -Force
#       winget install Python.Python.3.11 EclipseAdoptium.Temurin.21.JDK Git.Git
#       # close + reopen PowerShell so PATH refreshes
#       git clone https://github.com/evgenspm/goszakup-fast-bidder.git C:\temka
#       cd C:\temka
#       .\deploy\windows\install.ps1
#
# B) Repo already at C:\temka — just bootstrap venv + deps:
#       cd C:\temka
#       .\deploy\windows\install.ps1
#
# Idempotent — re-runs upgrade/skip cleanly.

$ErrorActionPreference = "Stop"

function Has-Command($name) {
    return ($null -ne (Get-Command $name -ErrorAction SilentlyContinue))
}

# When the script is run from inside an already-cloned repo, $PSScriptRoot is
# .../deploy/windows. Resolve the repo root from that — falls back to C:\temka.
$ScriptDir = $PSScriptRoot
if (-not $ScriptDir) { $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path

if (-not (Test-Path (Join-Path $RepoRoot "requirements.txt"))) {
    Write-Host "Cannot find requirements.txt under $RepoRoot" -ForegroundColor Red
    Write-Host "Run this script from inside the cloned repo (cd C:\temka first)." -ForegroundColor Red
    exit 1
}
Set-Location $RepoRoot

# Refresh PATH in case Python/Java/Git were just winget-installed in another
# elevated session and this one hasn't picked them up yet.
$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" `
          + [System.Environment]::GetEnvironmentVariable("Path", "User")

# Verify prereqs (informative — installer doesn't try to fetch them itself).
foreach ($tool in @("python", "git", "java")) {
    if (-not (Has-Command $tool)) {
        Write-Host "$tool not on PATH. Run these from an admin PowerShell first:" -ForegroundColor Yellow
        Write-Host "  winget install Python.Python.3.11" -ForegroundColor Yellow
        Write-Host "  winget install EclipseAdoptium.Temurin.21.JDK" -ForegroundColor Yellow
        Write-Host "  winget install Git.Git" -ForegroundColor Yellow
        Write-Host "Close + reopen PowerShell to refresh PATH, then re-run install.ps1." -ForegroundColor Yellow
        exit 1
    }
}

Write-Host "==> Creating Python virtualenv" -ForegroundColor Cyan
if (-not (Test-Path ".venv")) {
    py -3.11 -m venv .venv
}

Write-Host "==> Installing Python deps (~1 min)" -ForegroundColor Cyan
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\pip.exe install -r requirements.txt

# Lay down the desktop shortcuts so the customer never opens PowerShell again.
& "$RepoRoot\deploy\windows\install-shortcuts.ps1"

Write-Host ""
Write-Host "==> Bootstrap complete." -ForegroundColor Green
Write-Host ""
Write-Host "Next steps (manual, one-time):" -ForegroundColor Yellow
Write-Host "  1. Copy   config.example.yaml -> config.yaml ; edit BIN, allowed_user_ids, search filters"
Write-Host "  2. Copy   deploy\windows\.env.example -> .env at repo root ; fill in tokens/passwords"
Write-Host "  3. Place  ECP .p12 file at  $RepoRoot\secrets\ecp.p12  (create the folder)"
Write-Host "  4. Test:  double-click 'Тендер-бот' on the desktop"
Write-Host "  5. (optional) autostart at boot:"
Write-Host "         .\deploy\windows\install-shortcuts.ps1 -AutoStart"
Write-Host ""
