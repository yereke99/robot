# Creates two desktop shortcuts so a non-technical user never has to open
# PowerShell again:
#
#   "Тендер-бот.lnk"            — двойной клик запускает бота
#   "Тендер-бот — обновить.lnk" — двойной клик подтягивает свежий код
#
# Also (optional) registers Task Scheduler entry so the bot autostarts
# at PC boot — set $AutoStart = $true below or pass -AutoStart.

param(
    [switch]$AutoStart
)

$ErrorActionPreference = "Stop"

$RepoRoot   = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$RunBat     = "$RepoRoot\deploy\windows\run-bot.bat"
$UpdateBat  = "$RepoRoot\deploy\windows\update.bat"
$Desktop    = [Environment]::GetFolderPath("Desktop")

if (-not (Test-Path $RunBat)) {
    Write-Host "[!] Не нахожу $RunBat — сначала прогони install.ps1" -ForegroundColor Red
    exit 1
}

function New-Shortcut($Name, $Target, $Icon, $Description) {
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut("$Desktop\$Name.lnk")
    $lnk.TargetPath       = $Target
    $lnk.WorkingDirectory = $RepoRoot
    $lnk.IconLocation     = $Icon
    $lnk.Description      = $Description
    $lnk.WindowStyle      = 1     # normal window
    $lnk.Save()
    Write-Host "  ✓ $Name.lnk" -ForegroundColor Green
}

Write-Host "==> Создаю ярлыки на рабочем столе" -ForegroundColor Cyan

# imageres.dll,109 — жёлтая молния "fast"
New-Shortcut "Тендер-бот" $RunBat "imageres.dll,109" `
    "Запустить бота подачи заявок на goszakup"

# imageres.dll,225 — стрелка с обновлением
New-Shortcut "Тендер-бот — обновить" $UpdateBat "imageres.dll,225" `
    "Скачать свежий код бота и переустановить зависимости"

Write-Host ""
Write-Host "Готово. На рабочем столе появились два ярлыка." -ForegroundColor Green
Write-Host ""

if ($AutoStart) {
    Write-Host "==> Включаю автостарт через Планировщик задач" -ForegroundColor Cyan
    $taskName = "GoszakupBot"
    # /F on Create already overwrites any existing task; no need to
    # pre-delete (which errors with "task not found" on a clean box and
    # trips $ErrorActionPreference='Stop'). One call does it all.
    & schtasks /Create /TN $taskName `
        /TR "`"$RunBat`"" `
        /SC ONSTART `
        /RL HIGHEST `
        /F | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ! schtasks вернул код $LASTEXITCODE. Проверь права (нужен запуск от админа)." -ForegroundColor Red
        exit 1
    }
    Write-Host "  ✓ Задача '$taskName' будет запускаться при загрузке Windows" -ForegroundColor Green
    Write-Host ""
    Write-Host "Управление:" -ForegroundColor Yellow
    Write-Host "  Запустить:    schtasks /Run /TN $taskName"
    Write-Host "  Остановить:   schtasks /End /TN $taskName"
    Write-Host "  Проверить:    schtasks /Query /TN $taskName"
    Write-Host "  Удалить:      schtasks /Delete /TN $taskName /F"
} else {
    Write-Host "Чтобы бот запускался сам при включении компа — запусти ещё раз:" -ForegroundColor Yellow
    Write-Host "  .\deploy\windows\install-shortcuts.ps1 -AutoStart" -ForegroundColor Yellow
}
