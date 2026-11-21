# Windows установка и запуск бота

Гайд для одноразовой настройки на офисном ноуте/мини-PC. Время первой
установки: **~15 минут**. Дальше всё запускается двойным кликом.

## 0. Что должно быть в наличии

- **Windows 10 21H1+ или Windows 11** (любая редакция, в том числе Home)
- Стабильный интернет
- ~3 GB свободного места на диске
- Права администратора для установки и интерактивного запуска с автозаполнением
  окна TumarCSP через `start-bot.bat`
- ECP-сертификат (`.p12` файл) + пароль к нему
- Пароль от кабинета goszakup
- Telegram bot token (от [@BotFather](https://t.me/BotFather))
- Telegram user_id людей которым можно бидить (через [@userinfobot](https://t.me/userinfobot))
- Учётная запись GitHub с правом записи в ветку main репозитория origin для отправки отчётов

## 1. Установка (5 мин)

Открой **PowerShell от админа** (правый клик на иконке Пуск → «Windows
Terminal (Admin)» или «PowerShell (Admin)»). Запусти:

```powershell
winget install Python.Python.3.11
winget install EclipseAdoptium.Temurin.21.JDK
winget install Git.Git
# закрой PowerShell, открой заново чтобы подхватить PATH
git clone <URL_РЕПОЗИТОРИЯ> C:\goszakup-bot
cd C:\goszakup-bot
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

## 2. Конфигурация (5 мин, один раз)

После установки нужно сделать 3 вещи руками:

### 2.1 `config.yaml`

```powershell
cd C:\goszakup-bot
copy config.example.yaml config.yaml
notepad config.yaml
```

Заполни:

- `company.bin` — ваш БИН
- `company.subject_address`, `iik`, `contact_phone` — данные из кабинета
- `ecp.p12_path` → `C:\goszakup-bot\secrets\ecp.p12`
- `telegram.allowed_user_ids` — список Telegram user_id (числа)
- `telegram.report_chat_ids` — один личный Telegram ID для отчётов о работе и
  файлов замеров после подачи. Если ID уже указан, сохрани его при переносе
  `config.yaml` на Windows. `GOSZAKUP_REPORT_CHAT_ID` в `.env` переопределяет его.
- `search.statuses` и `search.methods` — фильтры тендеров (по умолчанию
  питание воспитанников + статусы «опубликован» и «приём заявок»)

### 2.2 ECP сертификат

```powershell
mkdir C:\goszakup-bot\secrets
# скопировать ваш файл .p12 в C:\goszakup-bot\secrets\ecp.p12 любым способом
# например через проводник
```

Ограничь доступ к папке (Свойства → Безопасность → оставить только
своего пользователя).

### 2.3 `.env` — секреты

```powershell
copy C:\goszakup-bot\deploy\windows\.env.example C:\goszakup-bot\.env
notepad C:\goszakup-bot\.env
```

Заполни 3 значения:

```
TG_BOT_TOKEN=<токен от BotFather>
GOSZAKUP_GOSZAKUP_ECP_DEFAULT=<пароль от .p12>
GOSZAKUP_GOSZAKUP_ACC_DEFAULT=<пароль кабинета goszakup>
```

## 3. Первый запуск (проверка)

До первого запуска проверь доступ к GitHub из учётной записи Windows, под
которой будет работать бот. Для входа через Git Credential Manager выполни
`git push --dry-run origin main` из папки проекта и заверши вход в браузере,
если GitHub его запросит.

Для подачи с автозаполнением окна TumarCSP запусти `start-bot.bat` из корня
проекта и подтверди запрос Windows на запуск от администратора. Скрипт
использует `.venv\Scripts\python.exe`, если виртуальное окружение создано.
Каждый запуск сначала выполняет `git pull --ff-only --no-rebase origin main`.
Бот стартует после успешного обновления чистой ветки `main`. При ошибке Git,
изменениях в отслеживаемых файлах или локальных коммитах, которых нет в
`origin/main`, скрипт остановится и покажет причину.

После успешного запуска в консоли появятся строки

```
origin/main updated: ...
[info] bot.starting pid=XXXX
[info] bot.signer_ready elapsed_ms=...
[info] bot.portal_prewarm elapsed_ms=...
[info] bot.ready count=1
```

После строки `bot.ready` отправь `/start` и дождись ответа в Telegram. Это
подтвердит, что бот получает обновления. Получатель отчётов должен один раз
открыть личный чат с ботом и нажать **Старт**, чтобы бот мог присылать файлы.

Чтобы остановить интерактивный запуск, нажми Ctrl+C в окне этого бота.
Дождись завершения, чтобы бот успел отправить итоговый отчёт.
Запускай одну копию на токен. Если используется Планировщик задач или служба,
останови её перед запуском через `start-bot.bat`.

## 4. Autostart — бот сам запускается при включении компа

```powershell
cd C:\goszakup-bot
.\deploy\windows\install-shortcuts.ps1 -AutoStart
```

Это создаст задачу в Планировщике Windows (`GoszakupBot`), которая будет
запускать бота при каждой загрузке Windows.
Задача должна работать под учётной записью Windows с настроенным доступом к
GitHub. Ярлык и задача используют `run-bot.bat` с тем же порядком обновления.
Для автозаполнения окна TumarCSP нужен открытый интерактивный сеанс Windows,
в котором работает TumarCSP. Для такой подачи используй `start-bot.bat` из
этого сеанса.

Управление:

```powershell
schtasks /Run /TN "GoszakupBot"     # запустить
schtasks /End /TN "GoszakupBot"     # остановить
schtasks /Query /TN "GoszakupBot"   # проверить статус
schtasks /Delete /TN "GoszakupBot" /F   # удалить
```

### Альтернатива — NSSM (бот как настоящая Windows-служба)

В свойствах службы укажи учётную запись Windows с доступом к репозиторию
на вкладке **Вход в систему**.

```powershell
winget install nssm.nssm
nssm install GoszakupBot "C:\goszakup-bot\.venv\Scripts\python.exe" `
    "-m" "goszakup.bot.windows_run"
nssm set GoszakupBot AppDirectory "C:\goszakup-bot"
nssm set GoszakupBot Start SERVICE_AUTO_START
nssm set GoszakupBot AppStopMethodConsole 60000
nssm set GoszakupBot AppStdout "C:\goszakup-bot\logs\bot.log"
nssm set GoszakupBot AppStderr "C:\goszakup-bot\logs\bot.err.log"
nssm start GoszakupBot
```

Управление NSSM-сервисом:

```powershell
nssm status GoszakupBot
nssm stop GoszakupBot
nssm start GoszakupBot
nssm restart GoszakupBot
nssm remove GoszakupBot confirm   # удалить службу
```

## 5. Обновление бота

При обычном запуске скрипт сам забирает изменения из `origin/main`.
Если поменялся `requirements.txt`, останови бота, запусти ярлык
«Тендер-бот — обновить» и затем снова запусти бота. Ярлык сначала обновляет
код, затем устанавливает зависимости.

После апдейта **перезапусти бот**:

- При ручном запуске нажми Ctrl+C в окне бота и снова запусти `start-bot.bat`.
- Для Планировщика задач выполни `schtasks /End /TN GoszakupBot`, затем
  `schtasks /Run /TN GoszakupBot`.
- Если через NSSM — `nssm restart GoszakupBot`

## 6. Что делать если упало

### Симптом «git не распознано»

Для первого ручного обновления выполни эти команды PowerShell из папки проекта.

```powershell
$gitExe = "$env:ProgramFiles\Git\cmd\git.exe"
& $gitExe --version
& $gitExe pull --ff-only origin main
```

Если файла по этому пути нет, найди папку установки Git в реестре

```powershell
Get-ItemProperty 'HKLM:\SOFTWARE\GitForWindows','HKCU:\SOFTWARE\GitForWindows' -Name InstallPath -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty InstallPath
```

Подставь этот путь с суффиксом `\cmd\git.exe` в `$gitExe`.
Для проверки установки можно выполнить `winget list --id Git.Git --exact`.
Скрипт запуска сам ищет Git в `PATH`, реестре и стандартных папках установки.

### Симптом: бот не отвечает в Telegram

Если бот сообщает о запущенной копии с тем же токеном или в журнале появилось
`telegram.polling_error` с `error_type=other_poller`, проверь запуски из консоли,
Планировщика задач и NSSM на этом ПК, а также серверы и другие ПК с этим
токеном. В журнале Telegram конфликт также может выглядеть как `terminated by
other getUpdates request`. Консольные и фоновые процессы бота на Windows можно
найти по команде запуска:

```powershell
Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -match 'goszakup\.bot\.(main|atagos_bot)' } |
    Select-Object ProcessId, ExecutablePath
```

Останови лишний запуск через его окно, службу или задачу. Отправь `/start`
оставшейся копии и проверь ответ.

Если в журнале есть `error_type=dns` или ошибка подключения к Telegram,
проверь с ПК бота, разрешается ли имя и доступен ли HTTPS:

```powershell
Resolve-DnsName api.telegram.org
Test-NetConnection api.telegram.org -Port 443
```

### Симптом: «kalkan.started» не появляется

- Java не нашлась. Проверь `where java` в PowerShell.
- Если пусто — переустанови Adoptium: `winget install EclipseAdoptium.Temurin.21.JDK`.
- Или вручную пропиши путь в `.env`: `JAVA_BIN=C:\Program Files\Eclipse Adoptium\jdk-21.X.X-hotspot\bin\java.exe`

### Симптом: «ci_session протухла»

- Бот пишет «🔐 Cookie протухла» в Telegram.
- Тапни `/login` в чате — он обновит cookie через ЭЦП.

### Симптом: бот падает при загрузке ЭЦП

- Проверь пароль в `.env` (GOSZAKUP_GOSZAKUP_ECP_DEFAULT).
- Проверь что `.p12` файл лежит в `C:\goszakup-bot\secrets\ecp.p12`.

### Логи

Журнал запуска и ошибок Telegram сохраняется в `runtime.log`. Предыдущие
файлы журнала имеют суффиксы `.1`, `.2` и далее. Каталог берётся из
`defaults.log_dir` в `config.yaml`, а `GOSZAKUP_LOG_DIR` переопределяет его.
Шаблон использует `%USERPROFILE%\.atagos\logs`. Если путь не задан, бот
использует `%USERPROFILE%\.goszakup\logs`.

```powershell
$logDir = "$env:USERPROFILE\.atagos\logs"  # Укажи свой каталог журналов
Select-String -Path (Join-Path $logDir 'runtime.log*') -Pattern 'telegram.polling_error'
Get-Content (Join-Path $logDir 'runtime.log') -Tail 50 -Wait
```

При запуске через Windows-ярлык бот присылает личному получателю файл о старте,
а после штатной остановки — файл с итоговым временем работы. Локальная копия
хранится в `runs/`. Отчёт включает до 3000 записей журнала текущего запуска.
В GitHub в `reports/windows/run_*.txt` сохраняются замеры на старте, после
попыток подачи, каждые 15 минут работы и при штатной остановке.

После каждой попытки подачи бот сохраняет отдельный `timing_*.txt` в
`timing/` и отправляет его личному получателю. Последний файл доступен
командой `/timing`. В нём указано имя соответствующего файла
`reports/windows/bid_*.txt` в ветке `origin/main`. В GitHub попадают
длительности этапов, запросы, коды ответа и типы сетевых ошибок. Номера
тендеров, заявок, файлов и данные доступа исключаются.

Windows-бот отправляет замеры фоновым `git push` после завершения заявки.
Если отправка не удалась, файлы хранятся в `github_pending/`
и повторно отправляются во время работы или при следующем запуске. Причина
сбоя записывается в `runtime.log` как `bot.run_report_push_failed`.

Время до начала запроса публикации и длительность ответа портала показаны
отдельно. Для проверки ускоренной загрузки техспецификаций можно добавить
`ATAGOS_SPEC_RANGE_DOWNLOAD=1` в `.env` и перезапустить бота. Режим применяется
к файлам от 4 МиБ, когда портал подтверждает поддержку загрузки по частям и
версию файла через `ETag`. При несовпадении или медленном втором ответе бот
использует полный файл из исходного запроса. Сравнивайте время загрузки в `/timing`.

## 7. Удалить всё

```powershell
schtasks /Delete /TN "GoszakupBot" /F  # если был через Task Scheduler
nssm remove GoszakupBot confirm        # если был через NSSM
Remove-Item -Recurse -Force C:\goszakup-bot   # удалить репо
```
