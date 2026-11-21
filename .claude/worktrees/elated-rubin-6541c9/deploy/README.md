# Deploying the goszakup bot to a VPS

One-time setup of the Telegram bot on a fresh Ubuntu 22.04+ VPS. The bot
runs in Docker, pulls updates from Telegram via long-polling (no inbound
ports), and uses a single ECP (.p12) certificate to sign on behalf of one
company. Access is gated by a Telegram user_id whitelist.

## 1. Install Docker on the VPS

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# log out and back in (or run `newgrp docker`) so the group takes effect
```

## 2. Create a bot with @BotFather

In Telegram, message [@BotFather](https://t.me/BotFather):

```
/newbot
<bot display name, e.g. "Goszakup Quick Bid">
<bot username, must end in "bot", e.g. goszakup_quickbid_bot>
```

BotFather replies with an **HTTP API token** that looks like
`1234567890:AAA...`. Keep it secret — anyone with this token controls the bot.

Optional: send `/setprivacy` → "Disable" so the bot can see all chat
messages (only matters if you ever add it to a group).

## 3. Clone the repo and configure it

```bash
git clone <your-git-url> /opt/temka
cd /opt/temka

# Bot config: edit BIN, address, allowed_user_ids, search keywords.
cp config.example.yaml config.yaml
$EDITOR config.yaml

# Secrets file used by docker-compose.
cp .env.example .env
$EDITOR .env                 # paste the bot token + ECP/account passwords
chmod 600 .env

# Upload the ECP certificate (run this from your laptop, NOT the VPS):
mkdir -p secrets
chmod 700 secrets
scp ~/.ecp/your-cert.p12 <user>@<vps>:/opt/temka/secrets/ecp.p12
ssh <user>@<vps> 'chmod 600 /opt/temka/secrets/ecp.p12'
```

In `config.yaml` set `ecp.p12_path: /secrets/ecp.p12` (this is the path
inside the container — the host bind mount is set in `docker-compose.yml`).

### Finding your Telegram user_id

In Telegram, message [@userinfobot](https://t.me/userinfobot). It replies
with your numeric user_id. Add the number to `telegram.allowed_user_ids`
in `config.yaml`.

## 4. First run

```bash
cd /opt/temka
docker compose build
docker compose up -d
docker compose logs -f
```

Healthy startup looks like:

```
kalkan.started pid=...
kalkan.loaded subject=...
bot ready: 1 whitelist users
```

The bot is now polling Telegram. In your Telegram client, send `/start`.

## 5. Day-to-day usage

```bash
docker compose ps              # status
docker compose logs -f --tail=200
docker compose restart         # after editing config.yaml
docker compose down && docker compose up -d --build  # after `git pull`
```

State lives in a named Docker volume `temka_bot-data`:

* `/data/state/<user_id>/<announce_id>.json` — pipeline checkpoints
* `/data/bot_history.db` — SQLite history (visible in `/history`)
* `/data/logs/` — html dumps when the parser misses
* `/data/secrets.json` — secrets the bot writes back (e.g. fresh ci_session)

Backup tip:
```bash
docker run --rm -v temka_bot-data:/data -v $PWD:/out alpine \
    tar -czf /out/bot-data-$(date +%F).tar.gz -C /data .
```

## 6. Troubleshooting

**Bot doesn't respond.** Check `docker compose logs -f`. Most common: bad
token (`telegram.error.InvalidToken`) or `kalkan.loaded` never appeared
(wrong ECP password — fix `GOSZAKUP_GOSZAKUP_ECP_DEFAULT` in `.env`).

**"❌ Access denied" when you /start.** Your Telegram user_id is not in
`telegram.allowed_user_ids` in `config.yaml`. Edit it, then `docker
compose restart`.

**`/status` shows cookie ❌ протухла.** Tap "🔄 Обновить cookie" or send
`/login`. The bot signs an ECP challenge and the new `ci_session` is
written to `/data/secrets.json`.

**Bid hangs at "📝 Подписываю документы…".** The Java subprocess died.
`docker compose logs -f` shows `helper_died`. Restart the container.

**Want to update the bot.**
```bash
cd /opt/temka
git pull
docker compose up -d --build
```

## 7. Security notes

* `.env` and `secrets/ecp.p12` are on the host filesystem. Anyone with
  shell access on the VPS can read them. Treat the VPS as **equivalent to
  the ECP cert** — same trust level. Don't share SSH access casually.
* The bot has no inbound ports. Firewall the SSH port.
* Do not log into the same goszakup cabinet from Chrome while the bot is
  running — Chrome's login can invalidate the bot's `ci_session` mid-bid.
