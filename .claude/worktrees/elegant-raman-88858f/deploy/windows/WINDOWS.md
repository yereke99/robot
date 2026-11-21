# Windows установка и запуск бота

Гайд для одноразовой настройки на офисном ноуте/мини-PC. Время первой
установки: **~15 минут**. Дальше всё запускается двойным кликом.

## 0. Что должно быть в наличии

- **Windows 10 21H1+ или Windows 11** (любая редакция, в том числе Home)
- Стабильный интернет
- ~3 GB свободного места на диске
- Права администратора **только на первую установку** (потом бот работает от
  обычного пользователя)
- ECP-сертификат (`.p12` файл) + пароль к нему
- Пароль от кабинета goszakup
- Telegram bot token (от [@BotFather](https://t.me/BotFather))
- Telegram user_id людей которым можно бидить (через [@userinfobot](https://t.me/userinfobot))

## 1. Установка одной командой (5 мин)

Открой **PowerShell от админа** (правый клик на иконке Пуск → «Windows
Terminal (Admin)» или «PowerShell (Admin)»). Запусти:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
$env:GOSZAKUP_REPO_URL = "https://github.com/evgenspm/goszakup-fast-bidder.git"
irm https://raw.githubusercontent.com/evgenspm/goszakup-fast-bidder/main/deploy/windows/install.ps1 | iex
```

Замени `evgenspm/goszakup-fast-bidder` на актуальный URL репозитория.

Этот скрипт сделает:

1. Поставит **Python 3.11** через winget
2. Поставит **OpenJDK 21** (Eclipse Temurin) через winget
3. Поставит **Git** через winget
4. Склонирует репозиторий в `C:\temka`
5. Создаст виртуальное окружение `.venv` и установит Python-зависимости

Альтернатива — поставить пакеты руками, потом склонировать:

```powershell
winget install Python.Python.3.11
winget install EclipseAdoptium.Temurin.21.JDK
winget install Git.Git
# закрой PowerShell, открой заново чтобы подхватить PATH
git clone https://github.com/evgenspm/goszakup-fast-bidder.git C:\temka
cd C:\temka
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

## 2. Конфигурация (5 мин, один раз)

После установки нужно сделать 3 вещи руками:

### 2.1 `config.yaml`

```powershell
cd C:\temka
copy config.example.yaml config.yaml
notepad config.yaml
```

Заполни:

- `company.bin` — ваш БИН
- `company.subject_address`, `iik`, `contact_phone` — данные из кабинета
- `ecp.p12_path` → `C:\temka\secrets\ecp.p12`
- `telegram.allowed_user_ids` — список Telegram user_id (числа)
- `search.statuses` и `search.methods` — фильтры тендеров (по умолчанию
  питание воспитанников + статусы «опубликован» и «приём заявок»)

### 2.2 ECP сертификат

```powershell
mkdir C:\temka\secrets
# скопировать ваш файл .p12 в C:\temka\secrets\ecp.p12 любым способом
# например через проводник
```

Ограничь доступ к папке (Свойства → Безопасность → оставить только
своего пользователя).

### 2.3 `.env` — секреты

```powershell
copy C:\temka\deploy\windows\.env.example C:\temka\.env
notepad C:\temka\.env
```

Заполни 3 значения:

```
TG_BOT_TOKEN=<токен от BotFather>
GOSZAKUP_GOSZAKUP_ECP_DEFAULT=<пароль от .p12>
GOSZAKUP_GOSZAKUP_ACC_DEFAULT=<пароль кабинета goszakup>
```

## 3. Первый запуск (проверка)

После того как `install.ps1` отработал, на **рабочем столе** появились
два ярлыка:

- 🚀 **Тендер-бот** — двойной клик запускает бота
- 🔄 **Тендер-бот — обновить** — двойной клик подтягивает новый код

Заказчик может смело **закрывать PowerShell** — больше он ему не нужен.
Дальше всё двойными кликами по ярлыкам.

Двойной клик по **Тендер-бот** на рабочем столе. В консоли должно появиться:

```
[info] kalkan.started pid=XXXX
[info] kalkan.loaded subject=...БИН120340012721...
opt4.gz_client.prewarm ms=...
bot ready: 1 whitelist users
```

Если видишь `bot ready` — пиши боту в Telegram `/start`. Должен ответить.

Чтобы остановить — закрой окно консоли (Ctrl+C тоже работает).

## 4. Autostart — бот сам запускается при включении компа

Самый простой способ — запустить `install-shortcuts.ps1` с флагом
`-AutoStart`:

```powershell
cd C:\temka
.\deploy\windows\install-shortcuts.ps1 -AutoStart
```

Это создаст задачу в Планировщике Windows (`GoszakupBot`), которая будет
запускать бота при каждой загрузке Windows. Перезагрузи комп один раз
для проверки — увидишь, что консоль с ботом появится сама.

Управление:

```powershell
schtasks /Run /TN "GoszakupBot"     # запустить
schtasks /End /TN "GoszakupBot"     # остановить
schtasks /Query /TN "GoszakupBot"   # проверить статус
schtasks /Delete /TN "GoszakupBot" /F   # удалить
```

### Альтернатива — NSSM (бот как настоящая Windows-служба)

Если хочется чтобы бот работал как **служба** (запускается раньше входа в
систему, не показывает консольное окно, автоматически рестартится при
крэше) — поставь [NSSM](https://nssm.cc):

```powershell
winget install nssm.nssm
nssm install GoszakupBot "C:\temka\.venv\Scripts\python.exe" `
    "-m" "goszakup.bot.main"
nssm set GoszakupBot AppDirectory "C:\temka"
nssm set GoszakupBot Start SERVICE_AUTO_START
nssm set GoszakupBot AppStdout "C:\temka\logs\bot.log"
nssm set GoszakupBot AppStderr "C:\temka\logs\bot.err.log"
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

Когда выходит новая версия — **двойной клик на ярлык «Тендер-бот — обновить»**
на рабочем столе.

Скрипт за кулисами:

1. `git pull --rebase --autostash` — забирает свежий код
2. `pip install -r requirements.txt` — ставит новые/обновлённые зависимости
3. Проверяет что бот импортируется без ошибок

После апдейта **перезапусти бот**:

- Если запущен через ярлык «Тендер-бот» — закрой окно консоли и тапни ярлык снова
- Если через Task Scheduler — `schtasks /End /TN GoszakupBot && schtasks /Run /TN GoszakupBot`
- Если через NSSM — `nssm restart GoszakupBot`

## 6. Что делать если упало

### Симптом: бот не отвечает в Telegram

1. Проверь что процесс жив:
   ```powershell
   Get-Process python -ErrorAction SilentlyContinue
   ```
2. Если процесс есть, но не отвечает — закрой и перезапусти.
3. Если процесса нет — запусти `run-bot.bat` руками и посмотри на ошибку
   в консоли.

### Симптом: «kalkan.started» не появляется

- Java не нашлась. Проверь `where java` в PowerShell.
- Если пусто — переустанови Adoptium: `winget install EclipseAdoptium.Temurin.21.JDK`.
- Или вручную пропиши путь в `.env`: `JAVA_BIN=C:\Program Files\Eclipse Adoptium\jdk-21.X.X-hotspot\bin\java.exe`

### Симптом: «ci_session протухла»

- Бот пишет «🔐 Cookie протухла» в Telegram.
- Тапни `/login` в чате — он обновит cookie через ЭЦП.

### Симптом: бот падает при загрузке ЭЦП

- Проверь пароль в `.env` (GOSZAKUP_GOSZAKUP_ECP_DEFAULT).
- Проверь что `.p12` файл лежит в `C:\temka\secrets\ecp.p12` (это тот же
  путь, что указан в `config.yaml` → `ecp.p12_path`).

### Логи

При запуске через `run-bot.bat` логи выводятся в консоль. При запуске
через NSSM пишутся в `C:\temka\logs\bot.log` и `bot.err.log`.

Полезные парсы:

```powershell
Get-Content C:\temka\logs\bot.log -Tail 50 -Wait    # как `tail -f`
Select-String -Path C:\temka\logs\bot.log -Pattern "ERROR|Traceback"
```

## 7. Удалить всё

```powershell
schtasks /Delete /TN "GoszakupBot" /F  # если был через Task Scheduler
nssm remove GoszakupBot confirm        # если был через NSSM
Remove-Item -Recurse -Force C:\temka   # удалить репо
# Python и Java оставь — пригодятся
```
