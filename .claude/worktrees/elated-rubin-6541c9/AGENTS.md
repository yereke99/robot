# AGENTS.md — atagos goszakup gamma-bidder

Read this first. It's the map of what's built, what's verified, and what still
needs a live run.

## What this is

A Python bot that auto-submits bids on **goszakup.gov.kz** (`v3bl.goszakup.gov.kz`)
for the account **КОКУМБАЕВ МАРАТ БАЛГАБЕКОВИЧ** (ИИН `870215301344`). Ported
from the `temka` project (`~/Desktop/temka`) — same architecture (async httpx +
Kalkan Java GOST signer + asyncio pipeline + Telegram bot), adapted for this
account's document set and, critically, its **gamma-encrypted price offer**.

It was reverse-engineered from a full real-submission HAR
(`v3bl.goszakup.gov.kz (1).har`, announce `17049098`) + `login_*.har`.

## The two things that differ from temka

### 1. Document set (generate → sign)
This account's tenders carry docs `{1253, 3359, 3334, 3327, 3335, 3328}` instead
of temka's `{741, 705, 709, 3240, 719}`. See `DEFAULT_DOC_PLAN` in `flow.py`.
- `1253`, `3327` — generate → sign (2 POSTs: `generate=…`, then signed file)
- `3359` — **beneficiary / Приложение 1**: upload the ID PDF (`удлич.pdf`) →
  `/files/check_status/` → `/ru/beneficiary/ajax_save_info` → generate → sign
- `3334` — per-lot sign (`send=Сохранить&sign_files=&signature[fid]=…`)
- `3335` — bankruptcy (`bankruptcy=2&btn=docGen` → `btn=docSave`) — same as temka
- `3328` — deposit (`typeDoc=3` → `?show=3 save_electronic_data=…`) — same as temka

### 2. Price = gamma-encrypted (the big one)
The price is **sealed**, not sent in plaintext, by a proprietary local provider
**TumarCSP** (ТОО «Гамма Технологии»), over `wss://127.0.0.1:6127/tumarcsp/`.
This is **separate from NCALayer** (signing). Flow (`flow._gamma_price` + `tumar.py`):

```
ajax_get_encr_info(lpId, version)         -> {public_key (per-tender EFC cert),
                                              sign, salt, pl_sum, d_sum, id_priceoffer,
                                              d_messageUp/Down}
TumarCSP SetAPIKey -> GetVersion -> EncryptOfferPrice(pl_sum, d_sum, public_key,
                                       sign, salt, id_priceoffer, messages)
                                          -> {encryptData, encryptKey, sn, sign}
ajax_add_encrypt(itemID=lpId, encryptedData=encryptData, sessionKey=encryptKey,
                 salt=salt[in], info=sn, sign=sign[out])
Kalkan sign encryptData (CAdES-T + TSA)   -> CMS
ajax_save_gamma_signs(xmlData[lp]=encryptData, signData[lp]=CMS)
ajax_priceoffers_next(offer[app_lot][lp][price][]=encryptData)
ajax_public_application(...)              <- gated by dry_run
```

**We do NOT reimplement EncryptOfferPrice** — it's proprietary. We drive the real
TumarCSP. ⇒ **TumarCSP must be installed & running on the bot host.** `d_sum` is
the dumping floor (= `pl_sum × 0.8`) — the exact minimum we always bid (operator's
hard rule: any higher price loses the tender 100%).

Signing (login XML, all docs, the gamma CMS) stays on the **Kalkan Java helper**
(`local_signer.py` + `jars/`, copied from temka) — no NCALayer needed.

## Layout

```
goszakup/
  client.py        all AJAX endpoints + 5-step ECP login. ATAGOS ADDITIONS:
                   create_application(tax_payer_type, consortium); show_doc_generate;
                   show_doc_save_signed; sign_lot_doc; check_file_status;
                   beneficiary_save_info/show_info; upload_file; get_encr_info;
                   add_encrypt; save_gamma_signs; priceoffers_next_gamma.
  tumar.py         NEW. TumarCSP WebSocket client (SetAPIKey/GetVersion/EncryptOfferPrice).
  flow.py          NEW. submit_bid() orchestrator (atagos doc plan + gamma price).
  local_signer.py  Kalkan Java GOST signer (unchanged from temka).
  pipeline.py      temka's pipeline — kept for its reusable _extract_app_lot_mapping.
  parser.py, state.py, secrets.py   unchanged from temka.
  java/, jars/     Kalkan provider + KalkanSigner (unchanged; needs OpenJDK 21+).
  bot/             Telegram bot (multi-tender) — see below.
cli.py             CLI: login / tumar-check / bid.
config.yaml        this account's profile + TumarCSP api_key (gitignored).
```

## Secrets (cross-platform store: macOS Keychain → env → ~/.atagos/secrets.json)

```
service="atagos-ecp"     account="default"      -> .p12 (ECP) password
service="atagos-acc"     account="default"      -> goszakup cabinet password
service="alagos-cookie"  account="ci_session"   -> session cookie
```
`cli.py login` prompts + saves the first two if missing.

## How to run

```bash
.venv/bin/python cli.py tumar-check          # verify TumarCSP up (needs the desktop app)
.venv/bin/python cli.py login                # refresh ci_session via ECP
.venv/bin/python cli.py bid --announce <ANN> --lots <L1,L2> --dry-run --auto-login
```
`bid` without `--price` bids the dumping floor (the min). `--dry-run` stops before
`ajax_public_application` (draft stays "Проект", revocable). Drop `--dry-run` for a
real submit.

## Status — VERIFIED vs NEEDS LIVE RUN

✅ Decoded from HAR & implemented: full endpoint shapes, the entire gamma price
   protocol (TumarCSP frames are byte-exact), beneficiary fields, doc sequence,
   create/lots params. Imports clean; CLI + config load; TumarCSP fails gracefully.

🟡 Needs a live run to confirm (no live session/.p12 password/TumarCSP available
   to the build agent):
   - `ajax_get_encr_info` **response field names** — `_parse_encr_info` is tolerant
     (snake+camel) but the live JSON should be logged once and pinned.
   - `upload_file` endpoint/response — the ID-PDF upload (`/ru/files/upload_file_new`)
     wasn't a plain POST in the HAR (page uses a JS/WS uploader). Parse confirmed
     best-effort; verify the returned file_id shape live.
   - Doc discovery: `DEFAULT_DOC_PLAN` matches announce 17049098. Other tender
     types may carry a different doc set — the docs page wasn't captured. Parser
     dumps HTML to `~/.atagos/logs/` on a miss so it's debuggable.
   - `_extract_gamma_pairs` regex vs the live priceoffers HTML (offer_gamma attrs).
   - Whether the gamma CMS needs TSA (we use CAdES-T to match the HAR signData size).

## Gotchas (inherited from temka, still true)

- Cookie can be killed by using goszakup in Chrome between `login` and `bid` —
  prefer `--auto-login`.
- Drafts at `/preview/` can't be re-walked; `flow.py` raises `AlreadyPrepared`.
- Kalkan helper is single-threaded (asyncio.Lock); signing is ~50ms, HTTP is the
  bottleneck.
- Deploy: TumarCSP is a desktop provider. A VPS bot needs it reachable (run the
  bot where TumarCSP runs, or expose port 6127 over a tunnel). See deploy/.
```
