# AGENTS.md — goszakup-bot gamma-bidder

Read this first. It's the map of what's built, what's verified, and what still
needs a live run.

## What this is

A Python bot that auto-submits bids on **goszakup.gov.kz** (`v3bl.goszakup.gov.kz`).
Architecture: async httpx + Kalkan Java GOST signer + asyncio pipeline + Telegram bot.
Supports **gamma-encrypted price offers** via TumarCSP.

It was reverse-engineered from a full real-submission HAR
(`v3bl.goszakup.gov.kz (1).har`, announce `17049098`) + `login_*.har`.

## Document set (generate → sign)

Tenders carry docs `{1253, 3359, 3334, 3327, 3335, 3328}`. See `DEFAULT_DOC_PLAN` in `flow.py`.
- `1253`, `3327` — generate → sign (2 POSTs: `generate=…`, then signed file)
- `3359` — **beneficiary / Приложение 1**: upload the ID PDF (`удлич.pdf`) →
  `/files/check_status/` → `/ru/beneficiary/ajax_save_info` → generate → sign
- `3334` — per-lot sign (`send=Сохранить&sign_files=&signature[fid]=…`)
- `3335` — bankruptcy (`bankruptcy=2&btn=docGen` → `btn=docSave`)
- `3328` — deposit (`typeDoc=3` → `?show=3 save_electronic_data=…`)

## Price = gamma-encrypted (the big one)

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
the portal's lower price bound. Telegram `/bid` and `/schedule` for repairs enter
80% of the planned unit price, rounded up to cents, when the result fits the
portal's range.

Signing (login XML, all docs, the gamma CMS) stays on the **Kalkan Java helper**
(`local_signer.py` + `jars/`) — no NCALayer needed.

## Layout

```
goszakup/
  client.py        all AJAX endpoints + 5-step ECP login.
  tumar.py         TumarCSP WebSocket client (SetAPIKey/GetVersion/EncryptOfferPrice).
  flow.py          submit_bid() orchestrator (doc plan + gamma price).
  hedge.py         HedgedRead: reads that overlap a stage-changing POST.
  presign.py       signs the tender's public тех.спец files before the opening.
  local_signer.py  Kalkan Java GOST signer.
  pipeline.py      reusable _extract_app_lot_mapping.
  parser.py, state.py, secrets.py   helpers.
  java/, jars/     Kalkan provider + KalkanSigner (needs OpenJDK 21+).
  bot/             Telegram bot (multi-tender) — see below.
cli.py             CLI: login / tumar-check / bid.
config.yaml        account profile + TumarCSP api_key (gitignored).
```

## Secrets (store: env → ~/.goszakup-bot/secrets.json)

```
service="goszakup-ecp"     account="default"      -> .p12 (ECP) password
service="goszakup-acc"     account="default"      -> goszakup cabinet password
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

## Fast-T0 (`/schedule`, `defaults.fast_t0: true`)

Two-phase path to cut wall-clock T0→submit from ~21s to ~5–8s:

1. **Warmup** (T0−5min): login, doc session pool, TumarCSP  
2. **Prepare** (as soon as tender opens): `stop_after_docs=True` → draft at `/priceoffers/`  
3. **Fire** (wall-clock T0): `resume_app_id=<app>` → gamma encrypt + `public_application` only  

`docs_next`: a rejection naming the тех.спец is validation LAG — polled through (0.25s steps, ≥6s) before anything is re-saved, the named document first (see `_docs_next_until_accepted`). Disable: `fast_t0: false`.

## Status — VERIFIED vs NEEDS LIVE RUN

✅ Decoded from HAR & implemented: full endpoint shapes, the entire gamma price
   protocol (TumarCSP frames are byte-exact), beneficiary fields, doc sequence,
   create/lots params. Imports clean; CLI + config load; TumarCSP fails gracefully.

🟡 Needs a live run to confirm:
   - `ajax_get_encr_info` **response field names** — `_parse_encr_info` is tolerant
     (snake+camel) but the live JSON should be logged once and pinned.
   - `upload_file` endpoint/response — the ID-PDF upload (`/ru/files/upload_file_new`)
     wasn't a plain POST in the HAR (page uses a JS/WS uploader). Parse confirmed
     best-effort; verify the returned file_id shape live.
   - Doc discovery: `DEFAULT_DOC_PLAN` matches announce 17049098. Other tender
     types may carry a different doc set — the docs page wasn't captured.
   - `_extract_gamma_pairs` regex vs the live priceoffers HTML (offer_gamma attrs).
   - Whether the gamma CMS needs TSA (we use CAdES-T to match the HAR signData size).

## Gotchas

- Cookie can be killed by using goszakup in Chrome between `login` and `bid` —
  prefer `--auto-login`.
- Drafts at `/preview/` can't be re-walked; `flow.py` raises `AlreadyPrepared`.
- Kalkan helper is single-threaded (asyncio.Lock); signing is ~50ms, HTTP is the
  bottleneck.
- Deploy: TumarCSP is a desktop provider. A VPS bot needs it reachable (run the
  bot where TumarCSP runs, or expose port 6127 over a tunnel). See deploy/.

## Speed work (15.11.2026) — read before touching the hot path

Full write-up, numbers and the open list: `docs/PERFORMANCE_AUDIT.md`.

- T0 → submit is ~17 SEQUENTIAL portal round-trips (HTTP/1.1, ~0.6-1.6s each);
  the тех.спец chain (discover → pages → save+barrier per page) is the longest.
- `tools/sim_portal.py` + `tests/test_e2e_sim.py`: the real client and
  `submit_bid` against a stateful fake portal on a virtual clock. It checks
  request ORDER as well as time — run it after any change to `flow.py`.
- Every bid records a per-request trace (`timing.BidTrace`); the bot sends it as
  a file after the bid and on `/timing`, and saves it to `<log_dir>/timing/`.
- Not verified on the live portal yet. `defaults.lot_barrier` (each|last|none)
  is an opt-in experiment to be tried in dry-run.

## Speed switches (21.11.2026) — trace #17947316

`flow.speed_options(defaults)` feeds every entry point. `early_app_lots`
(default ON) reads the тех.спец parent page while `lots_next` is in flight;
`lot_save_parallel` and `gamma_parallel_saves` are opt-in experiments with a
fallback each. The simulator models every portal reaction (`early_show_doc`,
`ts_overlap`, `signs_before_encrypt`). Even with all of them the model gives
~20 s at the live latencies: the rest is portal time (≥0.6 s per PHP request
even off-peak). Details and the dry-run checklist: `docs/PERFORMANCE_AUDIT.md`.

## Speed work (25.11.2026) — trace #17983518, 22.1 s

Details, the route-by-route model and the next-report checklist:
`docs/PERFORMANCE_AUDIT.md`, section «Трасса #17983518».

- The mandatory WRITES alone (create → add_lots → lots_next → generate →
  download → save → docs_next → encr_info → popup → add_encrypt →
  priceoffers_next → public) take 14.5–15 s at that trace's latencies. No
  reordering reaches 10 s; only a faster portal (~0.6–0.7 s/request) does.
  Never overlap writes to get there — the portal checks their order.
- `hedged_reads` (default ON, `hedge.py`): the page a stage POST unlocks (draft
  id during create, app_lot map + document list during lots_next, price page
  during docs_next) is read on a 0.3–0.45 s cadence WHILE that POST is in
  flight. Reads only. Rules that must hold for every such probe:
  * it goes through `GoszakupClient.peek` — AJAX-flagged (CodeIgniter never
    regenerates the session id on those) and its Set-Cookie is discarded (an
    anonymous answer must not replace the live `ci_session`);
  * its result is accepted only when complete and of THIS application
    (`_probe_app_lots`, `_is_full_docs_page`, `_verified_price_page`);
  * an early draft id counts only if the baseline read sent with the create
    POST showed no draft (`_EarlyApplicationId`); `add_lots` still waits for
    the create answer.
- `presign_tech_spec` (default ON, `presign.py`): the announcement's file list
  (`/ru/announce/actionAjaxModalShowFiles/<ann>/3334`) and its files are public
  before the opening; the application holds byte-identical COPIES under new
  file ids. They are signed during the wait, WITHOUT the cabinet cookie. A page
  of doc 3334 whose link texts name those files is committed at once; every
  copy is still downloaded and compared by sha256 before docs_next
  (`_PresignLedger`), a mismatch is re-signed from the real bytes and re-saved.
- The file server ignores `Range` (200 + full length) —
  `ATAGOS_SPEC_RANGE_DOWNLOAD` cannot help on this portal.
- Report diagnostics: `reply=` (key names + short codes of an AJAX answer,
  `client.reply_shape`) and `ctx=` (the attribute/URL an id sits in,
  `flow._id_context`). New fields must be added to the allowlists in
  `bot/log_capture.py` or the report drops them.
- OPEN: `flow.gamma.save_signs verdict=error` on a bid that was accepted. Until
  the next report shows that answer's `reply=`, keep `gamma_parallel_saves` off.
- `/config <switch> on|off` changes `lot_save_parallel`, `gamma_parallel_saves`,
  `lot_barrier`, `hedged_reads`, `presign_tech_spec` at runtime (until restart).
- Simulator knobs added for all of this: `read_at` / `commit_at` (when a request
  looks at the draft / when its write lands, as fractions of its latency),
  `lat` (per-route latencies, to replay a live trace), `listing`, `ts_names`,
  `ts_copy_differs`, `verify_signatures`, `stale_app`. The live trace is pinned
  as `test_replay_of_the_live_trace_17983518`.
- Next lever: create by POST at the opening second instead of polling the
  form (−1…−1.5 s of real time, invisible in the "from detection" metric). Not
  built: it needs the portal's answer to a create POST BEFORE the opening.
  `python -m tools.probe_preopen --post <announce>` sends exactly one such POST
  (refuses unless the create page says "not open", reports any draft left
  behind) — run by the operator; wait for that output before building it. The
  opening time is on the announcement card to the second.
- Dev note: this checkout's directories may be read-only (archive extraction);
  `chmod u+w` the directory before adding a file. `.venv` here is a Windows
  venv — run tests with a Python 3.11 environment of your own.
