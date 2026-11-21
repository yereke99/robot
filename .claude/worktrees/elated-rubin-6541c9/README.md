# atagos — goszakup gamma fast-bidder

Auto-submits bids on **goszakup.gov.kz** for КОКУМБАЕВ МАРАТ (ИИН 870215301344),
including the **gamma-encrypted price offer**. Telegram bot holds and fills many
tenders at once. Ported from `~/Desktop/temka`. Full design in **AGENTS.md**.

## Requirements

- **Python 3.11+** and **OpenJDK 21+** (the Kalkan GOST signer runs on the JVM).
- **TumarCSP** (ТОО «Гамма Технологии») installed & running on the bot host —
  it seals the price offer over `wss://127.0.0.1:6127/tumarcsp/`. **Without it,
  gamma price offers cannot be submitted** (everything else still works).
- The ECP `.p12` and the goszakup cabinet password.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# config.yaml already filled for this account; review it.
.venv/bin/python cli.py login          # prompts + saves ECP/cabinet passwords, gets ci_session
.venv/bin/python cli.py tumar-check    # confirms TumarCSP is up
```

## CLI

```bash
# Safe dry-run (stops before the final submit; draft stays "Проект", revocable):
.venv/bin/python cli.py bid --announce 17049098 --lots 41938202 --dry-run --auto-login

# Real submit (bids the dumping floor / minimum unless --price given):
.venv/bin/python cli.py bid --announce 17049098 --lots 41938202 --auto-login
```

## Telegram bot (multi-tender)

```bash
export TG_BOT_TOKEN=123:abc        # from @BotFather
.venv/bin/python -m goszakup.bot.atagos_bot
```

Then, in the chat:

```
/bid 17049098 41938202            # watch + bid (waits for open, bids the minimum)
/bid 17050001 41940000 1850000    # several at once — they wait & fire in parallel
/drybid 17049098 41938202         # dry-run variant
/watching                         # live state of every watched tender
/cancel 17049098                  # stop watching one
/status                           # signer / TumarCSP / cookie health
```

The bot holds 7–8 (or more) tenders concurrently: each waits for its own opening
moment and runs the full signed+encrypted submission without blocking the others.

## Layout

`goszakup/{client,flow,tumar,local_signer,parser,state,secrets}.py`, the Kalkan
`jars/`, the multi-tender bot in `goszakup/bot/atagos_bot.py`, CLI in `cli.py`.
See **AGENTS.md** for the protocol details and the "verified vs needs-live-run"
checklist.
