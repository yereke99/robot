"""atagos submission orchestrator.

Differs from temka's pipeline.py in two places (everything else — login,
create, lots, app_lot discovery, dry-run gating, state — is the same shape):

  * DOCUMENTS: this account's tenders carry doc set {1253, 3359, 3334, 3327,
    3335, 3328}. Most are generate→sign; doc 3359 is the beneficiary
    (Приложение 1) which needs the ID PDF uploaded + described before signing.

  * PRICE: gamma-encrypted. The price is sealed by TumarCSP (EncryptOfferPrice),
    not sent in plaintext. See tumar.py and client.gamma methods.

Flow (matches the captured HAR for announce 17049098):

    create_application (tax_payer_type=UL, consortium=0)
    add_lots / lots_next
    discover tender_lot -> app_lot
    for each doc in DOC_PLAN:  generate / upload+describe / sign
    docs_next
    GET priceoffers -> (app_lot, lp_id) pairs
    ajax_get_encr_info -> TumarCSP.EncryptOfferPrice -> ajax_add_encrypt
    Kalkan-sign encryptData -> ajax_save_gamma_signs
    ajax_priceoffers_next (price = encryptData)
    ajax_public_application                 <-- gated by dry_run
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from contextlib import AsyncExitStack, aclosing
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from typing import Awaitable, Callable, Optional, Sequence

import httpx
import structlog

from .client import AlreadyAtPriceStage, AlreadyPrepared, GoszakupClient, SessionExpired
from .local_signer import KalkanSigner, LocalSigner
from .parser import _dump, extract_download_targets, extract_prepared_doc_target
from .pipeline import _extract_app_lot_mapping  # reuse temka's robust mapper
from .state import StateStore, SubmissionState
from .timing import trace_event, trace_mark
from .tumar import EncrInfo, TumarCSP

log = structlog.get_logger("flow")


# ---- document plan (per tender type; defaults match announce 17049098) ----

@dataclass
class DocSpec:
    doc_id: int
    kind: str  # "generate_sign" | "beneficiary" | "lot_sign" | "bankruptcy" | "deposit"
    generate_fields: Optional[dict] = None  # for generate_sign / agreement docs


DEFAULT_DOC_PLAN: list[DocSpec] = [
    DocSpec(1253, "generate_sign", {"generate": "Сформировать документ"}),
    DocSpec(3359, "beneficiary", {"generate": "", "userfile[3359]": "", "save_form": ""}),
    DocSpec(3334, "lot_sign"),
    # Приложение 2 (Соглашение об участии в конкурсе). Portal renamed id
    # 3327 → 3715 on newer tenders; 3327 page exists but has no generate button.
    DocSpec(3715, "generate_sign", {"generate": "Принять соглашение"}),
    DocSpec(3335, "bankruptcy"),
    DocSpec(3328, "deposit"),
]

# Old/new ids for the same «Приложение 2 / Соглашение» slot.
_AGREEMENT_DOC_IDS = (3327, 3715)


def _adapt_doc_plan(plan: list[DocSpec], docs_html: str) -> list[DocSpec]:
    """Remap agreement doc id to whatever this application actually lists.

    Newer tenders expose Приложение 2 as 3715; older ones as 3327. The wrong
    id's show_doc page may still 200 with a title but without a generate button,
    so docs_next later fails with «Прикрепите … Приложение 2».
    """
    ag_id = None
    if docs_html:
        try:
            from lxml import html as lxml_html
            tree = lxml_html.fromstring(docs_html)
            for row in tree.xpath("//tr"):
                row_text = row.text_content().lower()
                if "приложение 2" in row_text or "соглашение" in row_text:
                    links = row.xpath(".//a[contains(@href, 'show_doc')]/@href")
                    for link in links:
                        m = re.search(r"/show_doc/\d+/\d+/(\d+)", link)
                        if m:
                            ag_id = int(m.group(1))
                            break
                    if ag_id:
                        break
        except Exception:
            pass

    ids = {int(x) for x in re.findall(r"/show_doc/\d+/\d+/(\d+)", docs_html or "")}
    if ag_id:
        ids.add(ag_id)

    if not ids and not ag_id:
        return list(plan)
    out: list[DocSpec] = []
    for spec in plan:
        if spec.doc_id in _AGREEMENT_DOC_IDS:
            chosen = ag_id or next((i for i in _AGREEMENT_DOC_IDS if i in ids), spec.doc_id)
            if chosen != spec.doc_id:
                log.info("flow.doc_plan.remap_agreement", old=spec.doc_id, new=chosen)
            out.append(DocSpec(chosen, spec.kind, spec.generate_fields))
        else:
            out.append(spec)
    return out


# docs_next polling. After the big Приложение 15 / Техническая спецификация files
# are signed, the server validates them ASYNCHRONOUSLY — for a few seconds it still
# answers "Подпишите ... Приложение 15" even though every file is signed. We poll
# docs_next until it confirms instead of firing once. Up to TRIES attempts spaced
# DELAY_S apart bounds the wait at ~(TRIES-1)*DELAY_S; it returns the instant the
# server accepts.
#
# First acceptance returns immediately — the delays only apply between rejects.
# The first polls are tight (a lag usually clears within a couple of seconds and
# every 100ms counts at T0); after that we back off but keep going for a while.
_DOCS_NEXT_TRIES = 10             # never give up before this many polls...
_DOCS_NEXT_MAX_WAIT_S = 30.0      # ...nor before this long
_DOCS_NEXT_MAX_TRIES = 60         # hard stop regardless of time
_DOCS_NEXT_DELAY_S = 0.25         # cadence for initial validation polls
_DOCS_NEXT_MIN_IDLE_S = 0.1       # idle between the first retries
_DOCS_NEXT_QUICK_RETRIES = 2      # only when the portal names the technical specification
_DOCS_NEXT_FAST_POLLS = 8
_DOCS_NEXT_SLOW_DELAY_S = 0.6     # afterwards

# A rejection naming the тех.спец is, far more often than not, that validation
# LAG and not a lost signature: nothing needs re-saving, the server just needs a
# moment. So we keep re-polling — for at least this many polls AND this many
# seconds — before re-committing anything. Re-saving on the very first such
# answer was the bug behind ~44s submissions: one lagging poll triggered a full
# serial re-commit of EVERY document plus a fresh download+sign of the тех.спец
# (~25s) for a problem that clears by itself.
_TECH_SPEC_PATIENCE = 4
_TECH_SPEC_PATIENCE_S = 6.0
# After a re-save the same lag applies again — let it settle (polls AND seconds)
# before escalating to the next, heavier re-save.
_SETTLE_POLLS_AFTER_RESAVE = 2
_SETTLE_S_AFTER_RESAVE = 6.0
# Hard cap on re-save rounds per docs_next loop (targeted first, then full).
_MAX_RESAVES = 3


def _clock() -> float:
    """Monotonic seconds for patience/timeout decisions (the event loop's clock,
    so tests and simulations can drive it)."""
    return asyncio.get_event_loop().time()


def _mark(state: "SubmissionState", key: str) -> float:
    """Record wall time for a pipeline phase; return elapsed since previous mark."""
    now = time.time()
    state.timestamps[key] = now
    trace_mark(key)
    # previous chronological mark (best-effort)
    prev = state.timestamps.get("_last_mark") or state.timestamps.get("start") or now
    state.timestamps["_last_mark"] = now
    elapsed = round(now - prev, 3)
    log.info("flow.timing", phase=key, dt_s=elapsed,
             since_start_s=round(now - state.timestamps.get("start", now), 3))
    return elapsed


def timing_breakdown(timestamps: dict) -> dict:
    """Per-phase seconds of one submission, from SubmissionState.timestamps.

    ``from_open`` is what matters competitively: tender detected open -> last
    step done. ``wait`` (time spent waiting for the tender to open) is reported
    separately — it used to be folded into the bot's "Время подачи", so a bid
    queued before the opening looked slow by however long it had waited.
    Keys are omitted when the run never reached that phase.
    """
    ts = timestamps or {}

    def span(a: str, b: str) -> Optional[float]:
        if a in ts and b in ts and ts[b] >= ts[a]:
            return round(ts[b] - ts[a], 1)
        return None

    end_key = next((k for k in ("public_done", "dry_run_complete", "signed", "gamma_done")
                    if k in ts), None)
    out: dict = {}
    wait = span("start", "t0")
    if wait is not None:
        out["wait"] = wait
    if end_key:
        total = span("t0", end_key)
        if total is not None:
            out["from_open"] = total
    for name, a, b in (
        ("create_lots", "t0", "lots_done"),
        ("docs", "lots_done", "docs_accepted"),
        ("price", "docs_accepted", "gamma_done"),
        ("price", "gamma_start", "gamma_done"),   # resume path (no docs)
        ("submit", "gamma_done", "public_done"),
        ("to_publish_request", "t0", "public_start"),
        ("publish_request", "public_start", "public_done"),
    ):
        v = span(a, b)
        if v is not None and name not in out:
            out[name] = v
    return out


async def _cancel_task(task) -> None:
    """Stop a background task on the way out and make sure its outcome is
    observed (no "exception was never retrieved" noise, nothing left running
    against clients that are about to close)."""
    if not task.done():
        task.cancel()
        await asyncio.wait([task], timeout=2.0)
    if task.done() and not task.cancelled():
        task.exception()


async def _keep_clients_warm(clients, stop: "asyncio.Event", interval_s: float = 45.0) -> None:
    """Hold one open connection per client until ``stop`` is set.

    httpx drops an idle connection after 60s, so re-touch every ``interval_s``
    while we wait for the tender. The touch is cookie-less (warm_connection), so
    it can neither rotate nor lock the cabinet session.
    """
    async def _one(client) -> None:
        warm = getattr(client, "warm_connection", None)
        if warm is None:
            return
        try:
            await warm()
        except Exception:  # noqa: BLE001 — pure optimisation
            pass

    t0 = time.time()
    while True:
        await asyncio.gather(*[_one(c) for c in clients])
        if t0:
            log.info("flow.doc_pool.connections_warm", n=len(clients),
                     elapsed_s=round(time.time() - t0, 2))
            t0 = 0.0
        if stop.is_set():
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
            return
        except asyncio.TimeoutError:
            continue


def _sync_session(gz, sib) -> None:
    """Make ``sib`` present exactly the cookies ``gz`` holds right now."""
    sync = getattr(gz, "sync_cookies_to", None)
    if callable(sync):
        sync(sib)
        return
    ci = gz.get_cookie("ci_session")
    if ci:
        sib.adopt_ci_session(ci)


def _clone_session(gz):
    """A sibling client on its own connection, carrying gz's current session.
    No login — the portal allows one session per account, so a second login
    would kill this one."""
    sib = gz.clone_unauthenticated()
    _sync_session(gz, sib)
    return sib


@dataclass
class BeneficiaryInfo:
    name: str
    citizenship: str
    res_country: str
    doc_number: str
    doc_date: str
    options: dict
    id_pdf_path: str
    # If set, reuse this already-uploaded (signed) file_id for the ID PDF and
    # skip the upload. The ID upload itself goes through NCALayer (signs the
    # local file, posts to upload_file_new) which we can't replay blind; but the
    # file_id is a server-side object, reusable across applications. Upload
    # удлич.pdf once in the browser, grab its file_id, set it here.
    id_file_id: Optional[int] = None


@dataclass
class AtagosBidParams:
    announce_id: str
    tender_lot_ids: list[int]
    # Bid price. If None, we bid the dumping floor — the exact minimum (the
    # operator's hard rule). Floor = server's d_sum if it returns one, else
    # ceil(pl_sum * dumping_floor_ratio, 2 decimals). See _parse_encr_info.
    price: Optional[float] = None
    dumping_floor_ratio: float = 0.80
    dry_run: bool = False
    # Prepare all documents concurrently (asyncio.gather) — generate/upload/
    # download/sign overlap, collapsing the read-heavy ~25s doc phase to ~6-8s.
    # Set False if the server starts rejecting under concurrency.
    parallel_docs: bool = True
    # How the signature-commit POSTs are issued. The server recomputes the
    # application's draft completeness on every save, so concurrent saves drop
    # each other's signatures (Приложение 1 / Приложение 15 "не подписано").
    #   "serial" (A, default, safe): one save at a time on the shared session with
    #     a GET barrier between — тех.спец last. Никаких гонок.
    #   "parallel_sessions" (B, experimental): each doc runs its full flow on its
    #     OWN logged-in session and saves in parallel (needs auto_login + signer).
    #     A docs_next miss is self-healed by a serial re-commit. Flip back to
    #     "serial" to roll back. See _save_parallel_sessions.
    doc_save_mode: str = "serial"
    doc_plan: list[DocSpec] = field(default_factory=lambda: list(DEFAULT_DOC_PLAN))
    # crypto / signer
    p12_path: Optional[str] = None
    ecp_password: Optional[str] = None
    signer: Optional[KalkanSigner] = None
    tumar: Optional[TumarCSP] = None  # injected long-lived TumarCSP (bot)
    tumar_api_key: Optional[str] = None
    tumar_ws_url: str = "wss://127.0.0.1:6127/tumarcsp/"
    # The Windows provider uses verified autofill (see popup_filler.py).
    autofill_price_popup: bool = False
    # Optional absolute-coordinate fallback for the popup (this machine only),
    # used when the window can't be enumerated. {field_xy, ok_xy, detect_xy,
    # detect_rgb} — see config tumarcsp.popup.*.
    popup_coords: Optional[dict] = None
    # login
    auto_login_account_password: Optional[str] = None
    # Set True (by scheduled warmup) to skip the initial gz.login() call in
    # submit_bid — the caller already has a fresh ci_session from warmup and a
    # new login would kill it (server allows only one session per account).
    # relogin_cb is still wired up for session recovery during the poll wait.
    skip_initial_login: bool = False
    # Максимальное время ожидания открытия тендера (poll_announce_ready).
    # 1800с для обычных ставок; меньше для /schedule где T0 известен.
    max_poll_wait_s: float = 1800.0
    # Обеспечение заявки (Приложение 19 / doc 3328): the typeDoc value the deposit
    # declaration POSTs. 3 = гарантийный денежный взнос (blocks cash on the
    # goszakup e-wallet — the default). Other codes select alternative forms
    # (e.g. банковская гарантия) but need their own upload flow — not yet built,
    # so setting a non-3 value only changes the declared type, not the upload.
    guarantee_type: int = 3
    # Optional async gate awaited RIGHT BEFORE ajax_public_application with a
    # summary dict {announce_id, app_id, guarantee_type, prices, total}. Return
    # False to abort the publish (the draft stays fully prepared, nothing is
    # submitted). None = publish automatically (the original behaviour).
    on_before_submit: Optional[Callable[[dict], Awaitable[bool]]] = field(
        default=None, compare=False)
    # Fast-T0 two-phase mode:
    #   stop_after_docs=True  → create+lots+docs+docs_next, then RETURN
    #                            (draft parked at /priceoffers/, step=docs_ready).
    #   resume_app_id=<id>    → skip create/lots/docs; go straight to gamma+submit.
    # Schedule uses prepare (stop_after_docs) as soon as the tender opens, then
    # at wall-clock T0 (or immediately if later) fires with resume_app_id → ~5-8s.
    stop_after_docs: bool = False
    resume_app_id: Optional[str] = None
    # GET of the тех.спец parent page after a sub-page save (the "commit barrier").
    #   "each" (default, proven): after every page.
    #   "last": once, after the final page.      "none": never.
    # EXPERIMENTAL. Every skipped barrier takes one full page round-trip (~1-1.6s)
    # off the critical path, but it is NOT known whether the portal needs it. Try
    # it in dry-run: if docs_next still accepts, keep it. If a page does get lost,
    # the docs_next self-heal re-saves it WITH barriers — a wrong guess costs
    # seconds, not the bid.
    lot_barrier: str = "each"


def _is_tech_spec_reject(msg: Optional[str]) -> bool:
    """True if a docs_next rejection is the 'Приложение 15 / Техническая
    спецификация unsigned' message — i.e. the тех.спец signatures didn't stick."""
    if not msg:
        return False
    low = msg.lower()
    return "приложение 15" in low or "техническ" in low


def _needs_resave(msg: Optional[str]) -> bool:
    """True if a docs_next rejection names a document that still needs signing —
    e.g. "Подпишите ... Приложение N" or any "приложение"/"подпиш" message. Broader
    than _is_tech_spec_reject so the self-heal covers Приложение 1, not just 15."""
    if not msg:
        return False
    low = msg.lower()
    return "подпиш" in low or "приложение" in low or "техническ" in low


_APPENDIX_RE = re.compile(r"приложени[еяю]\s*№?\s*(\d+)", re.IGNORECASE)


def _reject_key(msg: Optional[str]) -> str:
    """Stable identity of *what* a docs_next rejection complains about, so the
    patience/escalation counters follow one document (the lot number in the
    message varies; the appendix does not)."""
    low = (msg or "").lower()
    nums = sorted(set(_APPENDIX_RE.findall(low)), key=int)
    if nums:
        return "appendix:" + ",".join(nums)
    return "msg:" + re.sub(r"\d+", "#", low)[:80]


def _doc_titles(docs_html: str) -> dict[int, str]:
    """doc_id -> lower-cased row text of the application's docs page.

    Lets a rejection such as "Подпишите … Приложение 1 …" be matched to the exact
    doc id on THIS tender instead of guessing from a hard-coded table.
    """
    out: dict[int, str] = {}
    for row in re.findall(r"<tr\b.*?</tr>", docs_html or "", flags=re.DOTALL | re.IGNORECASE):
        ids = re.findall(r"/show_doc/\d+/\d+/(\d+)", row)
        if not ids:
            continue
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", row)).strip().lower()
        for i in dict.fromkeys(ids):
            out.setdefault(int(i), text)
    return out


def _docs_named_in_reject(msg: Optional[str], plan: Sequence["DocSpec"],
                          doc_titles: Optional[dict[int, str]] = None) -> set[int]:
    """Which planned doc ids does this rejection message point at? Empty set =
    can't tell (the caller then falls back to re-committing everything)."""
    if not msg:
        return set()
    low = msg.lower()
    nums = set(_APPENDIX_RE.findall(low))
    plan_ids = {s.doc_id for s in plan}
    out: set[int] = set()
    # 1. the live titles on this application's docs page (authoritative)
    if nums:
        for doc_id, title in (doc_titles or {}).items():
            if doc_id in plan_ids and nums & set(_APPENDIX_RE.findall(title)):
                out.add(doc_id)
    if out:
        return out
    # 2. the appendix numbers this tender type is known to use
    for s in plan:
        if s.kind == "lot_sign" and ("15" in nums or "техническ" in low):
            out.add(s.doc_id)
        elif s.kind == "beneficiary" and "1" in nums:
            out.add(s.doc_id)
        elif s.doc_id in _AGREEMENT_DOC_IDS and ("2" in nums or "соглашен" in low):
            out.add(s.doc_id)
        elif s.kind == "deposit" and ("19" in nums or "обеспечен" in low):
            out.add(s.doc_id)
        elif s.kind == "bankruptcy" and "банкрот" in low:
            out.add(s.doc_id)
    return out


async def _docs_next_until_accepted(gz, ann, app_id, *, on_reject=None) -> tuple[str, bool, Optional[str]]:
    """POST docs_next, polling until the server confirms every doc is accepted.

    The server answers "Подпишите … Приложение N" for two very different reasons,
    and the right reaction differs:

      * validation LAG (the тех.спец case): every file IS signed, the server just
        hasn't finished checking the big files. Nothing to re-save — poll again.
      * a save that really didn't stick (concurrent saves clobbering each other —
        seen on Приложение 1): polling alone never fixes it; the doc must be
        re-committed.

    So the reaction is graduated instead of "re-commit everything on the first
    rejection" (which turned a 2-second lag into a ~25-second re-save):

      1. тех.спец named  -> keep re-polling first (_TECH_SPEC_PATIENCE polls
         and _TECH_SPEC_PATIENCE_S seconds, whichever is later).
         another doc named -> re-save right away (those don't lag; one save is cheap).
      2. ``on_reject(message, 0)`` — TARGETED: re-commit only the named document.
      3. still rejected after it settled -> ``on_reject(message, 1+)`` — FULL
         serial re-commit (the old behaviour), at most _MAX_RESAVES rounds total.

    A clean bid accepts on attempt 1 and never enters any of this.
    Returns (last_body, accepted, last_message).
    """
    import json as _json

    body = ""
    msg: Optional[str] = None
    # per complaint: [consecutive rejections, clock of the first one] since its last re-save
    strikes: dict[str, list] = {}
    resaved: dict[str, int] = {}   # re-save rounds already spent per complaint
    total_resaves = 0
    started = _clock()
    attempt = 0
    while True:
        attempt += 1
        poll_started = _clock()
        dn = await gz.docs_next(ann, app_id)
        poll_duration = _clock() - poll_started
        body = dn.text
        try:
            dj = _json.loads(body)
        except (ValueError, TypeError):
            dj = None
        accepted = isinstance(dj, dict) and str(dj.get("status")) in ("1", "ok")
        msg = dj.get("message") if isinstance(dj, dict) else None
        log.info("flow.docs_next", attempt=attempt, status=dn.status_code,
                 len=len(body), accepted=accepted, body=body[:300])
        if accepted:
            return body, True, msg
        waited = _clock() - started
        if attempt >= _DOCS_NEXT_MAX_TRIES or (
                attempt >= _DOCS_NEXT_TRIES and waited >= _DOCS_NEXT_MAX_WAIT_S):
            _dump(body, f"docs_next_rejected_{ann}")
            log.warning("flow.docs_next.rejected", attempts=attempt,
                        waited_s=round(waited, 1), message=(msg or body[:400]))
            break
        log.info("flow.docs_next.retry", attempt=attempt,
                 message=(msg or body[:200])[:200])
        tech_spec_reject = _is_tech_spec_reject(msg)
        if on_reject is not None and _needs_resave(msg) and total_resaves < _MAX_RESAVES:
            key = _reject_key(msg)
            now = _clock()
            strike = strikes.setdefault(key, [0, now])
            strike[0] += 1
            level = resaved.get(key, 0)
            if tech_spec_reject:
                need_polls = _TECH_SPEC_PATIENCE if level == 0 else _SETTLE_POLLS_AFTER_RESAVE
                need_s = _TECH_SPEC_PATIENCE_S if level == 0 else _SETTLE_S_AFTER_RESAVE
            else:
                need_polls, need_s = 1, 0.0
            if strike[0] >= need_polls and (now - strike[1]) >= need_s:
                resave_started = time.monotonic()
                try:
                    await on_reject(msg, level)
                    log.info("flow.docs_next.resaved", attempt=attempt, level=level)
                except Exception as e:  # noqa: BLE001 — re-save best-effort, keep polling
                    log.warning("flow.docs_next.resave_failed", attempt=attempt,
                                level=level, err=str(e))
                finally:
                    trace_event("repair", "Пересохранение документов по ответу портала", resave_started)
                resaved[key] = level + 1
                strikes.pop(key, None)
                total_resaves += 1
            else:
                log.info("flow.docs_next.settling", attempt=attempt, strike=strike[0],
                         waited_s=round(now - strike[1], 1))
        delay = _DOCS_NEXT_SLOW_DELAY_S
        if attempt < _DOCS_NEXT_FAST_POLLS:
            delay = _DOCS_NEXT_DELAY_S
            if attempt <= _DOCS_NEXT_QUICK_RETRIES and tech_spec_reject and not total_resaves:
                # Count time spent in the just-completed validation request.
                # Keep a pause even if the round trip exceeded the cadence.
                delay = max(_DOCS_NEXT_MIN_IDLE_S, delay - poll_duration)
        retry_started = time.monotonic()
        try:
            await asyncio.sleep(delay)
        finally:
            trace_event("wait", "Повтор проверки подписей", retry_started)
    return body, False, msg


async def _heal_rejected_docs(gz, ns, params, app_id, app_lot_map, base_url,
                              save_ops, msg, level, doc_titles=None) -> None:
    """Re-commit after docs_next reported a document unsigned (see
    _docs_next_until_accepted for when this is called).

    level 0 — TARGETED: re-POST only the named document's prepared signatures
      (serially, on the main session, GET barrier after each). One appendix costs
      ~1 save instead of re-committing all of them.
    level 1+ — FULL: re-POST every non-lot document, then redo the тех.спец from
      scratch (fresh download + sign, bypassing the signature cache) — covers a
      stale cached signature as well as a lost save.
    """
    ann = params.announce_id
    lot_doc_ids = {s.doc_id for s in params.doc_plan if s.kind == "lot_sign"}
    if level == 0:
        targets = _docs_named_in_reject(msg, params.doc_plan, doc_titles)
        ops = [op for op in save_ops if op.doc_id in targets]
        if ops:
            log.info("flow.docs_next.resave.targeted", docs=sorted(targets), n=len(ops))
            await _save_serial(gz, ann, app_id, ops)
            return
        log.info("flow.docs_next.resave.untargeted",
                 reason="could not map the message to a prepared document",
                 message=(msg or "")[:160])
    log.info("flow.docs_next.resave.full", level=level)
    await _save_serial(gz, ann, app_id,
                       [op for op in save_ops if op.doc_id not in lot_doc_ids])
    await _resave_tech_spec(gz, ns, params, app_id, app_lot_map, base_url)


async def submit_bid(
    *,
    params: AtagosBidParams,
    ci_session: str,
    lp_id_cache: dict | None = None,    # optional output: announce_id -> [lp_id]
    reuse_doc_pool: list | None = None, # готовые сессии из прошлой ставки
    tz_sig_cache: dict | None = None,   # sha256 -> gost_sig для ТЗ файлов
    company: dict,
    beneficiary: BeneficiaryInfo,
    state_store: StateStore,
    base_url: str = "https://v3bl.goszakup.gov.kz",
    agreement_cookie: Optional[str] = None,
    pre_t0_poll: bool = True,
    poll_interval_s: float = 0.1,
    proxy: Optional[str] = None,
    gz_client: Optional[GoszakupClient] = None,
    on_open=None,
) -> SubmissionState:
    # Each run is a fresh attempt (create_application makes a NEW app id), so
    # start clean — don't let a previous attempt's error/step/app_id bleed in
    # (the state file is keyed by announce_id and reused across runs).
    state = SubmissionState(announce_id=params.announce_id)
    state.timestamps["start"] = time.time()
    state_store.save(state)

    async with AsyncExitStack() as stack:
        if gz_client is not None:
            gz = gz_client
        else:
            initial_cookie = None if params.auto_login_account_password else ci_session
            gz = await stack.enter_async_context(GoszakupClient(
                ci_session=initial_cookie, agreement_cookie=agreement_cookie,
                base_url=base_url, proxy=proxy,
            ))

        # Signer: Kalkan (Java). Reused long-lived in the bot; one-shot for CLI.
        if params.signer is not None:
            ns = await stack.enter_async_context(
                LocalSigner(kalkan_signer=params.signer, http_client=gz._http)
            )
        else:
            if not params.p12_path or params.ecp_password is None:
                raise ValueError("need signer or (p12_path, ecp_password)")
            ns = await stack.enter_async_context(LocalSigner(
                p12_path=params.p12_path, password=params.ecp_password,
                http_client=gz._http,
            ))

        # Re-login callback: the cabinet ci_session expires after ~5 min, so a
        # long pre-T0 wait can outlive it. With stored creds we can re-sign and
        # refresh in place; poll_announce_ready uses this to survive a session
        # expiry while waiting instead of crashing. None (manual ci_session) ->
        # poll raises SessionExpired as before.
        relogin_cb = None
        if params.auto_login_account_password:
            if not params.skip_initial_login:
                # Initial login is race-free (no probes running yet) — do it
                # directly on gz so the shared jar holds the authenticated session.
                await gz.login(
                    account_password=params.auto_login_account_password,
                    kalkan_signer=ns,
                )

            # In-poll relogin MUST be isolated: while the 6-step login runs, the
            # poll loop keeps firing probes on gz._http; a probe 302-ing to
            # /user/login Set-Cookies a fresh ci_session and clobbers the login
            # mid-flow → the password step 302s to /error_report/access_denied.
            # relogin_isolated logs in on a private jar and only adopts the result.
            async def relogin_cb():  # noqa: E306
                await gz.relogin_isolated(
                    account_password=params.auto_login_account_password,
                    kalkan_signer=ns,
                )

        # Doc-pool prewarm is ONLY needed when we will sign documents.
        # resume_app_id (fast-T0 phase-2) skips docs — prewarming here used to
        # burn ~6s on the critical path for nothing.
        doc_pool: list = []
        # The pool is plain cookie clones (no logins), so it does NOT need the
        # cabinet password — gating it on one meant a bot running on a pasted
        # cookie got no pre-opened connections and no early documents.
        need_doc_pool = (
            not params.resume_app_id
            and params.doc_save_mode == "parallel_sessions"
            and params.signer is not None
        )
        if need_doc_pool:
            if reuse_doc_pool:
                doc_pool = reuse_doc_pool
                log.info("flow.doc_pool.reused", n=len(doc_pool))
            else:
                doc_pool = await _prewarm_doc_pool(gz, params, stack)
        elif params.resume_app_id:
            log.info("flow.doc_pool.skipped", reason="resume_app_id fast path")

        # Warm Tumar while the portal opens and the documents are signed.
        # The portal can create the application before the provider is ready.
        tumar_task = None
        if not params.stop_after_docs and params.tumar is None and params.tumar_api_key:
            tumar_task = asyncio.create_task(_open_tumar(params, stack))
            stack.push_async_callback(_cancel_task, tumar_task)
            log.info("flow.tumar.prewarm.start")

        # Open the per-document connections NOW, while we wait for the tender:
        # each sibling otherwise pays its own TCP+TLS handshake on its first
        # document request — right in the middle of the post-T0 burst. Kept alive
        # until the tender opens; cookie-less, so the session is never touched.
        warm_stop = asyncio.Event()
        if doc_pool:
            warm_task = asyncio.ensure_future(
                _keep_clients_warm([sib for sib, _ in doc_pool], warm_stop))
            stack.push_async_callback(_cancel_task, warm_task)

        try:
            if pre_t0_poll:
                await gz.poll_announce_ready(
                    params.announce_id, interval_s=poll_interval_s,
                    max_wait_s=getattr(params, "max_poll_wait_s", 1800.0),
                    relogin=relogin_cb)
        finally:
            warm_stop.set()   # stop re-pinging; a handshake already in flight finishes

        _mark(state, "t0")
        state_store.save(state)
        # Tender just opened — let the caller announce it (e.g. Telegram "тендер
        # открыт"). Fired in the BACKGROUND: awaiting a Telegram round-trip here
        # held up create_application by ~0.3-1s at the exact moment speed matters.
        if on_open is not None:
            async def _announce_open():
                try:
                    await on_open()
                except Exception:  # noqa: BLE001 — a notification must never sink a bid
                    log.warning("flow.on_open.failed", announce=params.announce_id)
            stack.push_async_callback(_cancel_task, asyncio.ensure_future(_announce_open()))

        # doc_id -> title on this application's docs page (filled once the page
        # loads); lets the docs_next self-heal re-save only the named document.
        docs_titles: dict[int, str] = {}

        # === FAST PATH: resume from /priceoffers/ (docs already done) ===
        # Used by /schedule phase-2 after stop_after_docs prepare. Skips
        # create/lots/docs entirely — critical path is gamma + public only.
        early_futures: dict = {}
        skip_docs = False
        app_lot_map: dict = {}
        if params.resume_app_id:
            app_id = str(params.resume_app_id)
            state.application_id = app_id
            state.step = "resume_price"
            state_store.save(state)
            skip_docs = True
            log.info("flow.resume_app_id", app_id=app_id,
                     reason="skip create/lots/docs → gamma+submit")
        else:
            # === create / lots ===
            app_id = await gz.create_application(
                params.announce_id,
                subject_address=company["subject_address"],
                iik=company["iik"],
                contact_phone=company.get("contact_phone", ""),
                tax_payer_type=company.get("tax_payer_type", "UL"),
                consortium=company.get("consortium", 0),
            )
            if not app_id:
                raise RuntimeError("create_application did not return application_id")
            state.application_id = app_id
            state.step = "created"
            _mark(state, "created")
            state_store.save(state)
            log.info("flow.created", app_id=app_id)

            # === add_lots + lots_next FIRST (server requires them before doc generation) ===
            await gz.add_lots(params.announce_id, app_id, params.tender_lot_ids)
            _mark(state, "lots_selected")
            await gz.lots_next(params.announce_id, app_id)
            state.step = "lots_added"
            _mark(state, "lots_done")
            state_store.save(state)

            # CI may rotate cookies on create/lots — siblings cloned before the
            # wait must carry what the main client holds NOW before they POST.
            if doc_pool:
                for _sib, _ in doc_pool:
                    _sync_session(gz, _sib)

            # The docs list is needed for two things only: the Приложение 2 id
            # (3327↔3715) and the doc titles for a targeted self-heal. Neither
            # blocks app_lot discovery or the other documents, so it runs
            # ALONGSIDE them instead of as its own serial round-trip in front.
            async def _load_docs_page() -> Optional[str]:
                try:
                    docs_html = await gz._get_page(
                        f"/ru/application/docs/{params.announce_id}/{app_id}"
                    )
                    params.doc_plan = _adapt_doc_plan(params.doc_plan, docs_html or "")
                    docs_titles.update(_doc_titles(docs_html or ""))
                    return docs_html
                except Exception as e:  # noqa: BLE001 — keep DEFAULT_DOC_PLAN
                    log.warning("flow.doc_plan.adapt_failed", err=str(e)[:120])
                    return None

            docs_page_task = asyncio.create_task(_load_docs_page())
            stack.push_async_callback(_cancel_task, docs_page_task)
            app_lots_task = asyncio.create_task(
                _discover_app_lots(gz, params, app_id, docs_page_task=docs_page_task)
            )
            stack.push_async_callback(_cancel_task, app_lots_task)

            async def _early_doc(i: int, spec: DocSpec, sib, sib_ns):
                checked_beneficiary_file = False
                if spec.kind == "beneficiary" and beneficiary.id_file_id:
                    # This check needs only the existing file ID. The attachment
                    # still waits for the application's lot IDs.
                    try:
                        checked = await sib.check_file_status([int(beneficiary.id_file_id)])
                        checked_beneficiary_file = checked.is_success
                    except Exception as e:  # retry in the normal document flow
                        log.warning("flow.beneficiary.check_prefetch_failed", err=str(e)[:120])
                if spec.doc_id in _AGREEMENT_DOC_IDS:
                    # only this doc depends on the docs page (its id may be remapped)
                    await asyncio.shield(docs_page_task)
                    spec = params.doc_plan[i]
                lot_map = (
                    await asyncio.shield(app_lots_task)
                    if spec.kind in ("lot_sign", "beneficiary", "deposit") else {}
                )
                return await _run_doc_in_session(
                    gz, sib, sib_ns, params, app_id,
                    app_lot_map=lot_map,
                    beneficiary=beneficiary, base_url=base_url, spec=spec,
                    sig_cache=tz_sig_cache,
                    checked_beneficiary_file=checked_beneficiary_file,
                )

            # Keep each document on the same sibling used in _save_parallel_sessions.
            for i, (spec, (sib, sib_ns)) in enumerate(zip(params.doc_plan, doc_pool)):
                early_futures[i] = asyncio.create_task(_early_doc(i, spec, sib, sib_ns))
                stack.push_async_callback(_cancel_task, early_futures[i])
                log.info("flow.early_doc.started", doc=spec.doc_id, kind=spec.kind)

            try:
                app_lot_map = await app_lots_task
                log.info("flow.app_lot_map", mapping=app_lot_map)
                # the plan (agreement id) must be final before the doc phase reads it
                await asyncio.shield(docs_page_task)
            except AlreadyAtPriceStage:
                # Draft already at the price stage (docs done on a prior run) — skip
                # the whole doc phase and resume at the gamma price. Cancel any early
                # docs that started before discovery (they'd fail on the advanced draft).
                log.warning("flow.resume_at_price", app_id=app_id,
                            reason="draft already at /priceoffers/; skipping doc phase")
                skip_docs = True
                app_lot_map = {}
                pending_docs = [*early_futures.values(), docs_page_task]
                for task in pending_docs:
                    task.cancel()
                await asyncio.gather(*pending_docs, return_exceptions=True)
                early_futures = {}

        if not skip_docs:
            save_ops = await _process_documents(
                gz, ns, params, app_id, app_lot_map, beneficiary, base_url,
                doc_pool=doc_pool, early_futures=early_futures,
                sig_cache=tz_sig_cache,
            )
            state.step = "docs_signed"
            _mark(state, "docs_signed")
            state_store.save(state)

            async def _resave_cb(msg, level):
                await _heal_rejected_docs(
                    gz, ns, params, app_id, app_lot_map, base_url,
                    save_ops, msg, level, docs_titles)

            body, accepted, message = await _docs_next_until_accepted(
                gz, params.announce_id, app_id, on_reject=_resave_cb)
            if not accepted:
                state.step = "docs_rejected"
                state.error = message or (body[:400] if body else "docs_next rejected")
                state_store.save(state)
                raise RuntimeError(f"docs_next rejected: {(message or body or 'unknown')[:300]}")
            _mark(state, "docs_accepted")

        if params.stop_after_docs:
            state.step = "docs_ready"
            _mark(state, "docs_ready")
            state_store.save(state)
            return state

        _mark(state, "gamma_start")
        discovered_lp_ids, prices_by_lp = await _gamma_price(
            gz, ns, params, app_id, stack, tumar_task=tumar_task,
        )
        _mark(state, "gamma_done")
        if lp_id_cache is not None and discovered_lp_ids:
            lp_id_cache[params.announce_id] = discovered_lp_ids
        state.step = "signed"
        _mark(state, "signed")
        state_store.save(state)

        # === final submit (gated) ===
        # Safety: ATAGOS_FORCE_DRY_RUN=1 in .env blocks publish even if dry_run=false.
        import os as _os
        force_dry = _os.getenv("ATAGOS_FORCE_DRY_RUN", "").strip().lower() in (
            "1", "true", "yes", "on",
        )
        if params.dry_run or force_dry:
            log.warning(
                "DRY_RUN: skipping ajax_public_application",
                app_id=app_id,
                dry_run=params.dry_run,
                force_env=force_dry,
            )
            state.step = "dry_run_complete"
            _mark(state, "dry_run_complete")
            state_store.save(state)
            log.info(
                "flow.timing.summary",
                announce=params.announce_id,
                app_id=app_id,
                resume=bool(params.resume_app_id),
                total_s=round(
                    time.time() - state.timestamps.get("t0", state.timestamps["start"]), 3
                ),
                step=state.step,
            )
            return state

        # Optional human approval right before the irreversible publish. The whole
        # draft is prepared and priced; on_before_submit shows the summary and
        # returns False to abort (draft stays ready, nothing submitted).
        if params.on_before_submit is not None:
            summary = {
                "announce_id": params.announce_id,
                "app_id": app_id,
                "guarantee_type": params.guarantee_type,
                "prices": prices_by_lp,
                "total": round(sum(prices_by_lp.values()), 2),
            }
            approved = await params.on_before_submit(summary)
            if not approved:
                log.warning("flow.submit.declined", app_id=app_id, summary=summary)
                state.step = "submit_declined"
                state_store.save(state)
                return state

        _mark(state, "public_start")
        result = await gz.public_application(params.announce_id, app_id)
        log.info("flow.public_application.result", result=result)
        state.step = "public_ok" if result.get("status") in ("ok", 1, "1") else "public_error"
        if state.step == "public_error":
            state.error = result.get("message") or str(result)[:500]
        _mark(state, "public_done")
        state_store.save(state)
        log.info(
            "flow.timing.summary",
            announce=params.announce_id,
            app_id=app_id,
            resume=bool(params.resume_app_id),
            total_s=round(
                time.time() - state.timestamps.get("t0", state.timestamps["start"]), 3
            ),
            step=state.step,
        )

    return state


async def _discover_app_lots(gz, params, app_id, *, docs_page_task=None) -> dict[int, int]:
    # Try the per-lot doc page first — empirically it carries the
    # tender_lot -> app_lot mapping and resolves in ONE GET (the /docs and /lots
    # pages don't, and /lots just 302s back to /docs — pure waste). Fall through
    # to the others only as a belt-and-braces fallback.
    lot_doc = next((s.doc_id for s in params.doc_plan if s.kind == "lot_sign"), 3334)
    ann = params.announce_id
    docs_path = f"/ru/application/docs/{ann}/{app_id}"
    candidates = (
        f"/ru/application/show_doc/{ann}/{app_id}/{lot_doc}",
        docs_path,
        f"/ru/application/lots/{ann}/{app_id}",
        f"/ru/application/create/{ann}",
    )
    app_lot_map: dict[int, int] = {}
    html_text = ""
    for cand in candidates:
        html_text = None
        if cand == docs_path and docs_page_task is not None:
            html_text = await asyncio.shield(docs_page_task)
        if html_text is None:
            html_text = await gz._get_page(cand)
        # A redirect to an already-advanced draft skips discovery. Stage markers
        # are explicit, unlike links to preview/priceoffers on other pages.
        if 'current_method = "preview"' in html_text:
            raise AlreadyPrepared(f"draft {app_id} already at /preview/ stage", app_id=app_id)
        if 'current_method = "priceoffers"' in html_text:
            raise AlreadyAtPriceStage(
                f"draft {app_id} already at /priceoffers/ stage", app_id=app_id
            )
        got = _extract_app_lot_mapping(html_text, params.tender_lot_ids)
        if got:
            app_lot_map.update(got)
            if len(app_lot_map) == len(params.tender_lot_ids):
                return app_lot_map
    if len(app_lot_map) != len(params.tender_lot_ids):
        if "/application/preview/" in html_text:
            raise AlreadyPrepared(
                f"draft {app_id} already at /preview/ stage", app_id=app_id
            )
        # Draft advanced to the price stage on a prior run (docs done, price not
        # submitted): create/lots/docs/show_doc all 302 to /priceoffers/, so the
        # tender_lot→app_lot mapping isn't on any page we fetched. Signal the
        # caller to skip the doc phase and resume at the gamma price directly.
        if "/application/priceoffers/" in html_text or _extract_gamma_pairs(html_text):
            raise AlreadyAtPriceStage(
                f"draft {app_id} already at /priceoffers/ stage", app_id=app_id
            )
        _dump(html_text, f"app_lot_miss_{params.announce_id}")
        raise RuntimeError(
            f"could not resolve app_lot for all tender lots (got {app_lot_map}); "
            "HTML dumped under ~/.atagos/logs/"
        )
    return app_lot_map


def _first_download_target(html_text: str) -> Optional[tuple[int, str]]:
    """Non-raising (file_id, download_path) lookup."""
    try:
        targets = extract_download_targets(html_text, quiet=True)
    except ValueError:
        return None
    return targets[0] if targets else None


class DocumentGenerationUnconfirmed(RuntimeError):
    def __init__(self, app_id: str, doc_id: int):
        super().__init__(
            f"Связь прервалась при формировании документа {doc_id}. "
            f"Проверь черновик {app_id} в кабинете."
        )
        self.app_id = app_id
        self.doc_id = doc_id


async def _recover_generated_doc_target(gz, ann, app_id, doc_id) -> tuple[int, str] | None:
    path = f"/ru/application/show_doc/{ann}/{app_id}/{doc_id}"
    # A POST with a lost response may already have changed the draft. Consult
    # only the same document and allow a short delay for its state to appear.
    try:
        async with asyncio.timeout(5.0):
            for attempt in (1, 2):
                try:
                    page = await gz._get_page(path, follow_redirects=False)
                except (SessionExpired, httpx.HTTPStatusError):
                    return None
                except httpx.TransportError:
                    page = ""
                target = extract_prepared_doc_target(page, doc_id)
                if target is not None:
                    log.info("flow.doc.generate_recovered", doc=doc_id, attempt=attempt)
                    return target
                if attempt == 1:
                    await asyncio.sleep(0.25)
    except TimeoutError:
        pass
    return None


async def _generate_doc_target(gz, ann, app_id, doc_id, fields, *, hint: str) -> tuple[int, str]:
    """Generate a doc and return (file_id, download_path).

    The browser reads the new file link from the generate-POST **response** (the
    page re-renders with download_file/<id>/<token> + the sign UI). A fresh GET
    shows the pre-generate state — which is why parsing a separate GET found
    nothing. So: parse the POST response first, fall back to a GET, and only then
    dump+raise (on the larger page).

    ``download_path`` carries BOTH segments (file_id + access token); downloading
    with only the file_id 403s "Доступ к документу запрещен".
    """
    try:
        r = await gz.show_doc_generate(ann, app_id, doc_id, fields=fields)
    except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ReadTimeout,
            httpx.WriteError, httpx.WriteTimeout) as exc:
        log.warning("flow.doc.generate_response_lost", doc=doc_id, error_type=type(exc).__name__)
        target = await _recover_generated_doc_target(gz, ann, app_id, doc_id)
        if target is not None:
            return target
        raise DocumentGenerationUnconfirmed(app_id, doc_id) from exc
    tgt = _first_download_target(r.text)
    if tgt is not None:
        return tgt
    html = await gz.get_show_doc(ann, app_id, doc_id)
    tgt = _first_download_target(html)
    if tgt is not None:
        return tgt

    # Fallback for agreement doc (3327 / 3715): try alternate generate labels
    if doc_id in (3327, 3715):
        curr_gen = (fields or {}).get("generate", "")
        alt_label = "Сформировать документ" if curr_gen == "Принять соглашение" else "Принять соглашение"
        log.info("flow.doc_target.try_alt_generate", doc_id=doc_id, alt=alt_label)
        try:
            r_alt = await gz.show_doc_generate(ann, app_id, doc_id, fields={"generate": alt_label})
            tgt = _first_download_target(r_alt.text)
            if tgt is not None:
                return tgt
            html_alt = await gz.get_show_doc(ann, app_id, doc_id)
            tgt = _first_download_target(html_alt)
            if tgt is not None:
                return tgt
        except Exception as e:
            log.warning("flow.doc_target.alt_generate_failed", doc_id=doc_id, err=str(e))

    # neither had it — dump the more complete page so we can teach the parser
    best = r.text if len(r.text) >= len(html) else html
    extract_download_targets(best, hint=hint)  # raises ValueError + dumps HTML
    raise AssertionError("unreachable")


@dataclass
class _SaveOp:
    """One deferred signature-commit POST. ``run(client)`` issues the save on the
    given client — the shared gz (serial) or a per-doc sibling (parallel sessions).
    Deferring the commit is what lets us serialize it (GET barrier between saves) or
    isolate it per session, instead of racing the saves inside the prepare batch."""
    label: str
    doc_id: int
    run: Callable[["GoszakupClient"], Awaitable]


async def _prepare_doc(gz, ns, params, app_id, app_lot_map, beneficiary, base_url, spec,
                       sig_cache: dict | None = None, on_ready=None,
                       checked_beneficiary_file: bool = False):
    """Do everything for one doc EXCEPT the final signature-commit save: generate /
    upload / describe / download / GOST-sign. Returns the deferred _SaveOp(s).

    Runs on whichever (client, signer) pair is passed — gz/ns for the shared-session
    prepare, or a sibling/sibling-signer for a parallel-session full flow.

    Runs concurrently across the doc pool in parallel_sessions mode (each doc on
    its own sibling session). No per-dispatch lock: the 403 that once motivated
    one was actually the two-segment download URL (see sign_path), not a session
    race — so prepare runs fully parallel again.

    ``on_ready`` (lot_sign only): awaited with each page's _SaveOp the moment that
    page is signed, in page order — lets the caller commit page 1 while page 2's
    multi-MB files are still downloading instead of waiting for the slowest page.
    The ops are still all returned (for the docs_next self-heal)."""
    ann = params.announce_id

    async def sign_path(download_path: str) -> str:
        # download_path carries BOTH segments (file_id + access token); the
        # single-segment form 403s "Доступ к документу запрещен".
        referer = f"{base_url}/ru/application/show_doc/{ann}/{app_id}/{spec.doc_id}"
        return await ns.sign_file(f"{base_url}{download_path}", referer=referer)

    if spec.kind == "generate_sign":
        fid, dl = await _generate_doc_target(
            gz, ann, app_id, spec.doc_id, spec.generate_fields, hint=f"doc_{spec.doc_id}")
        sig = await sign_path(dl)
        log.info("flow.doc.prepared", doc=spec.doc_id, fid=fid, dl=dl)
        return [_SaveOp(
            f"doc_{spec.doc_id}", spec.doc_id,
            lambda c, d=spec.doc_id, f=fid, s=sig:
                c.show_doc_save_signed(ann, app_id, d, file_id=f, signature_b64=s))]

    if spec.kind == "beneficiary":
        signed_fid, sig = await _beneficiary_prepare(
            gz, ns, params, app_id, spec, app_lot_map, beneficiary, base_url,
            checked_beneficiary_file=checked_beneficiary_file)
        return [_SaveOp(
            f"beneficiary_{spec.doc_id}", spec.doc_id,
            lambda c, d=spec.doc_id, f=signed_fid, s=sig:
                c.show_doc_save_signed(ann, app_id, d, file_id=f, signature_b64=s))]

    if spec.kind == "bankruptcy":
        r = await gz.bankruptcy_gen(ann, app_id, doc_id=spec.doc_id)
        # Like the generate docs: when the POST answers 200 with the re-rendered
        # page, the new file link is already in it — no extra GET needed. A 30x
        # (empty body) or a page without a link falls back to the GET.
        tgt = None
        if getattr(r, "status_code", None) == 200:
            tgt = _first_download_target(getattr(r, "text", "") or "")
        if tgt is None:
            html = await gz.get_show_doc(ann, app_id, spec.doc_id)
            tgt = extract_download_targets(html, hint="bankruptcy")[0]
        fid, dl = tgt
        sig = await sign_path(dl)
        log.info("flow.bankruptcy.prepared", doc=spec.doc_id, fid=fid, dl=dl)
        return [_SaveOp(
            f"bankruptcy_{spec.doc_id}", spec.doc_id,
            lambda c, d=spec.doc_id, f=fid, s=sig:
                c.bankruptcy_save(ann, app_id, doc_id=d, file_id=f, signature_b64=s))]

    if spec.kind == "deposit":
        ops: list[_SaveOp] = []
        gtype = params.guarantee_type
        for app_lot in app_lot_map.values():
            await gz.deposit_add(ann, app_id, doc_id=spec.doc_id, lot_id=app_lot,
                                 type_doc=gtype)
            ops.append(_SaveOp(
                f"deposit_{spec.doc_id}_{app_lot}", spec.doc_id,
                lambda c, d=spec.doc_id, lot=app_lot, gt=gtype:
                    c.deposit_save(ann, app_id, doc_id=d, lot_id=lot, type_doc=gt)))
        log.info("flow.deposit.prepared", doc=spec.doc_id,
                 lots=list(app_lot_map.values()), guarantee_type=gtype)
        return ops

    if spec.kind == "lot_sign":
        # тех.спец / Приложение 15: GET + download + GOST-sign each sub-doc page
        # (read-only, parallel inside), defer each page's save POST.
        ops = []
        lots = list(app_lot_map.values())

        def pages_for(app_lot):
            return aclosing(_lot_sign_pages(
                gz, ns, ann, app_id, spec.doc_id, app_lot, base_url, sig_cache=sig_cache))

        pending = None
        async with AsyncExitStack() as lookahead:
            for i, app_lot in enumerate(lots):
                if pending is None:
                    pages = await lookahead.enter_async_context(pages_for(app_lot))
                    page = await anext(pages, None)
                else:
                    pages, first = pending
                    page = await first
                    pending = None

                if page is not None and on_ready is not None and i + 1 < len(lots):
                    # Start the next lot's bounded read window while this lot
                    # commits. Saves wait for all preceding save/GET barriers.
                    next_pages = await lookahead.enter_async_context(pages_for(lots[i + 1]))
                    first = asyncio.create_task(anext(next_pages, None))
                    lookahead.push_async_callback(_cancel_task, first)
                    pending = next_pages, first

                while page is not None:
                    num, sigs = page
                    op = _SaveOp(
                        f"lot_{spec.doc_id}_{app_lot}_{num}", spec.doc_id,
                        lambda c, d=spec.doc_id, lot=app_lot, n=num, s=sigs:
                            c.sign_lot_doc(ann, app_id, d, lot, n, signatures=s))
                    ops.append(op)
                    if on_ready is not None:
                        await on_ready(op)
                    page = await anext(pages, None)
        return ops

    raise ValueError(f"unknown doc kind: {spec.kind!r}")


async def _process_documents(gz, ns, params, app_id, app_lot_map, beneficiary, base_url,
                             doc_pool=None, early_futures=None, sig_cache=None):
    """Sign + commit every document; return the prepared _SaveOp list (so the
    docs_next self-heal can re-commit serially on the main session).

    PREPARE (concurrent): generate / upload / download / GOST-sign every doc —
      read-heavy, independent, overlaps the big тех.спец downloads. ~25s → ~6-8s.
    SAVE (params.doc_save_mode): the commit POSTs are where the server recomputes
      the application's draft completeness, so a concurrent save drops another's
      just-saved signature (Приложение 1 / Приложение 15 "не подписано").
        "parallel_sessions" (B, default — fast): each doc on its own sibling
          session (from the pre-warmed `doc_pool`), prepared+saved in parallel.
        "serial" (A, safe fallback): one save at a time on gz, GET barrier
          between, тех.спец last — никаких гонок, но ~8с медленнее.
    """
    ann = params.announce_id
    mode = params.doc_save_mode

    if mode == "parallel_sessions":
        if params.signer is not None:
            return await _save_parallel_sessions(
                gz, params, app_id, app_lot_map, beneficiary, base_url, doc_pool or [],
                early_futures=early_futures or {}, sig_cache=sig_cache)
        log.warning("flow.doc_save_mode.parallel_unavailable",
                    reason="needs signer; using serial")

    # A — prepare all concurrently, commit serially (тех.спец last).
    results: dict[int, list[_SaveOp]] = {}

    async def _prep(i: int, spec: DocSpec) -> None:
        results[i] = await _prepare_doc(
            gz, ns, params, app_id, app_lot_map, beneficiary, base_url, spec,
            sig_cache=sig_cache)

    if params.parallel_docs:
        t0 = time.time()
        log.info("flow.docs.prepare.start", n=len(params.doc_plan))
        await asyncio.gather(*(_prep(i, s) for i, s in enumerate(params.doc_plan)))
        log.info("flow.docs.prepare.done", elapsed_s=round(time.time() - t0, 2))
    else:
        for i, s in enumerate(params.doc_plan):
            await _prep(i, s)

    other = [i for i, s in enumerate(params.doc_plan) if s.kind != "lot_sign"]
    lot = [i for i, s in enumerate(params.doc_plan) if s.kind == "lot_sign"]
    save_ops = [op for i in other + lot for op in results.get(i, [])]

    log.info("flow.docs.save.serial.start", n=len(save_ops))
    await _save_serial(gz, ann, app_id, save_ops)
    return save_ops


async def _save_serial(gz, ann, app_id, save_ops) -> None:
    """Commit each save one at a time, re-GET the doc between (the commit barrier
    the manual capture uses) so the server finalizes draft completeness before the
    next write. The proven, race-free order."""
    for op in save_ops:
        await op.run(gz)
        log.info("flow.doc.saved", label=op.label, doc=op.doc_id)
        try:
            await gz.get_show_doc(ann, app_id, op.doc_id)  # commit barrier
        except Exception:  # noqa: BLE001 — best-effort, never fatal
            pass


async def _prewarm_doc_pool(gz, params, stack):
    """Clone the main gz session into N sibling clients (one per document).
    No separate logins — the server allows only one active session per account,
    so additional logins would kill the main session (and the browser session).
    Each sibling inherits gz's current ci_session; if it expires mid-bid the
    existing auto-relogin path in _run_doc_in_session handles recovery."""
    n = len(params.doc_plan)

    def _clone() -> tuple:
        sib = _clone_session(gz)
        sib_ns = LocalSigner(kalkan_signer=params.signer, http_client=sib._http)
        return (sib, sib_ns)

    t0 = time.time()
    log.info("flow.doc_pool.prewarm.start", n=n)
    pool = [_clone() for _ in range(n)]
    for sib, _ in pool:
        await stack.enter_async_context(sib)
    log.info("flow.doc_pool.prewarm.done", n=len(pool), elapsed_s=round(time.time() - t0, 2))
    return pool


def _lot_barrier_mode(params) -> str:
    """params.lot_barrier, normalised; anything unknown means the proven "each"."""
    mode = str(getattr(params, "lot_barrier", "each") or "each").strip().lower()
    return mode if mode in ("each", "last", "none") else "each"


async def _run_doc_in_session(
    gz, sib, sib_ns, params, app_id, app_lot_map, beneficiary, base_url, spec: DocSpec,
    sig_cache: dict | None = None, checked_beneficiary_file: bool = False,
) -> list[_SaveOp]:
    """Prepare + save one document in an isolated session.

    GET barrier kept only for lot_sign (doc_3334 has multiple sub-page saves).
    Single-save docs skip it — the 302 response IS the server confirmation.

    The тех.спец pages are committed as each one becomes ready (page 1 is saved
    while page 2 is still downloading) — same save order and the same barrier
    between saves, just without idling until the slowest page has been fetched.
    """
    ann = params.announce_id
    needs_barrier = spec.kind == "lot_sign"
    barrier_mode = _lot_barrier_mode(params) if needs_barrier else "none"
    committed: set[int] = set()

    async def _barrier() -> None:
        try:
            await sib.get_show_doc(ann, app_id, spec.doc_id)  # commit barrier
        except Exception:  # noqa: BLE001 — best-effort, never fatal
            pass

    async def _commit(op: _SaveOp) -> None:
        await op.run(sib)
        committed.add(id(op))
        log.info("flow.doc.saved.isolated", label=op.label, doc=op.doc_id)
        if barrier_mode == "each":
            await _barrier()

    ops = await _prepare_doc(
        sib, sib_ns, params, app_id, app_lot_map, beneficiary, base_url, spec,
        sig_cache=sig_cache, on_ready=_commit if needs_barrier else None,
        checked_beneficiary_file=checked_beneficiary_file)
    for op in ops:
        if id(op) not in committed:
            await _commit(op)
    if barrier_mode == "last" and ops:
        await _barrier()
    return ops


async def _save_parallel_sessions(gz, params, app_id, app_lot_map, beneficiary, base_url, pool,
                                  early_futures: dict | None = None,
                                  sig_cache: dict | None = None):
    """B (experimental): one logged-in session per document, run fully in parallel.

    Each session runs its doc's WHOLE flow (generate→sign→save), not just the save:
    a sibling can't commit a signature for a file another session generated unless
    the server shares it cross-session — the same app-level sharing that makes
    concurrent saves clobber. So full per-doc isolation is the only internally
    consistent form of B; it mirrors signing each appendix in its own browser tab.

    Sessions come from the pre-warmed pool (logged in during the pre-T0 wait → zero
    login latency here). A pool session that expired during a long wait is
    re-logged-in on use; any shortfall (pool smaller than the doc plan) is spawned
    on the spot. Returns the union of prepared _SaveOps for the docs_next self-heal.

    `early_futures`: {spec_index -> asyncio.Task} for documents started during
    app-lot discovery and document list loading.
    """
    ann = params.announce_id
    acc_pw = params.auto_login_account_password
    specs = list(params.doc_plan)
    pool = list(pool or [])
    early_futures = early_futures or {}
    all_ops: list[_SaveOp] = []
    lock = asyncio.Lock()
    spawned: list = []
    # Session-expiry recovery is SINGLE-FLIGHT. The portal keeps one session per
    # account, so if several documents hit SessionExpired at once and each logged
    # in on its own, every login would kill the session the previous one had just
    # obtained. One document re-logs in; the others wait and reuse its cookie.
    relogin_lock = asyncio.Lock()
    relogin_gen = 0

    async def _session_for(i: int) -> tuple:
        if i < len(pool):
            return pool[i]
        # Clone session instead of a new login — server allows only one
        # active session per account.
        sib = _clone_session(gz)
        sib_ns = LocalSigner(kalkan_signer=params.signer, http_client=sib._http)
        spawned.append(sib)
        return (sib, sib_ns)

    async def _relogin_once(sib, sib_ns, seen_gen: int) -> None:
        nonlocal relogin_gen
        async with relogin_lock:
            if relogin_gen != seen_gen:
                return  # someone already refreshed the session while we waited
            fresh_ci = await sib.login(account_password=acc_pw, kalkan_signer=sib_ns)
            # Propagate the fresh session to gz and every other sibling.
            gz.adopt_ci_session(fresh_ci)
            for other_sib in [s for s, _ in pool] + spawned:
                if other_sib is not sib:
                    other_sib.adopt_ci_session(fresh_ci)
            relogin_gen += 1

    async def _one(i: int, spec: DocSpec) -> None:
        sib, sib_ns = await _session_for(i)
        seen_gen = relogin_gen
        try:
            if i in early_futures:
                ops = await early_futures[i]
                log.info("flow.early_doc.joined", doc=spec.doc_id)
            else:
                ops = await _run_doc_in_session(
                    gz, sib, sib_ns, params, app_id, app_lot_map, beneficiary, base_url, spec,
                    sig_cache=sig_cache)
        except SessionExpired:
            if not acc_pw:
                raise
            log.warning("flow.doc_session.expired.relogin", doc=spec.doc_id)
            await _relogin_once(sib, sib_ns, seen_gen)
            ops = await _run_doc_in_session(
                gz, sib, sib_ns, params, app_id, app_lot_map, beneficiary, base_url, spec,
                sig_cache=sig_cache)
        async with lock:
            all_ops.extend(ops)

    t0 = time.time()
    log.info("flow.docs.parallel_sessions.start", n=len(specs), pooled=len(pool),
             early=len(early_futures))
    try:
        await asyncio.gather(*(_one(i, s) for i, s in enumerate(specs)))
    finally:
        for sib in spawned:
            await sib.aclose()
    log.info("flow.docs.parallel_sessions.done", elapsed_s=round(time.time() - t0, 2))
    return all_ops


async def _beneficiary_prepare(gz, ns, params, app_id, spec, app_lot_map, beneficiary, base_url,
                               *, checked_beneficiary_file: bool = False):
    """Приложение 1: attach the ID PDF, describe the beneficiary, generate + sign —
    everything but the final commit save. Returns (signed_file_id, signature) for
    the deferred _SaveOp."""
    ann = params.announce_id
    # 1. obtain the ID PDF's file_id.
    if beneficiary.id_file_id:
        # Reuse a file uploaded once via the browser (the upload itself goes
        # through NCALayer's signed-upload, which we can't replay blind).
        file_id = int(beneficiary.id_file_id)
        log.info("flow.beneficiary.reuse_file_id", file_id=file_id)
    else:
        # Best-effort direct upload (works only if the page exposes a plain
        # upload; gamma/beneficiary pages require NCALayer-signed upload).
        first_lot = next(iter(app_lot_map.values()))
        page = ""
        for suffix in (f"/{first_lot}", ""):
            try:
                page = await gz.get_show_doc(ann, app_id, spec.doc_id, suffix=suffix)
                if "form_uploading" in page or 'type="file"' in page or "data-file-token" in page:
                    break
            except Exception:
                continue
        referer = f"{base_url}/ru/application/show_doc/{ann}/{app_id}/{spec.doc_id}"
        file_id = await gz.upload_file(beneficiary.id_pdf_path, page_html=page, referer=referer)
        if not file_id:
            dump = _dump(page, f"beneficiary_upload_form_{spec.doc_id}")
            raise RuntimeError(
                "ID PDF upload needs an NCALayer-signed upload (server returned no "
                "file_id). Upload удлич.pdf once in the browser, then set "
                "beneficiary.id_file_id in config.yaml to reuse it. "
                f"(page dumped to {dump})"
            )
    # 2. confirm it passed checks
    if not checked_beneficiary_file:
        await gz.check_file_status([file_id])
    # 3. describe + attach for each app_lot (HAR shows per-lot beneficiary save)
    for app_lot in app_lot_map.values():
        await gz.beneficiary_save_info(
            name=beneficiary.name,
            citizenship=beneficiary.citizenship,
            res_country=beneficiary.res_country,
            doc_number=beneficiary.doc_number,
            doc_date=beneficiary.doc_date,
            options=beneficiary.options,
            app_lot_id=app_lot,
            file_ids=[file_id],
        )
    # 4. generate the appendix doc + sign the produced file (commit deferred)
    fid, dl = await _generate_doc_target(
        gz, ann, app_id, spec.doc_id, spec.generate_fields,
        hint=f"beneficiary_doc_{spec.doc_id}",
    )
    referer = f"{base_url}/ru/application/show_doc/{ann}/{app_id}/{spec.doc_id}"
    sig = await ns.sign_file(f"{base_url}{dl}", referer=referer)
    log.info("flow.beneficiary.prepared", doc=spec.doc_id, id_file=file_id, signed_file=fid)
    return fid, sig


def _describe_unsignable_page(html_text: str) -> dict:
    """Summarise a lot sub-doc page that has NO attached file, so the missing
    step (generate / upload / agree) is obvious in the log. A *substantial* page
    with no file_id is a real sub-document needing an action — not the end of the
    list (e.g. Приложение 15 / Техническая спецификация must be generated or
    uploaded before there is anything to sign)."""
    low = html_text.lower()
    title = ""
    m = re.search(r"Приложение\s*\d+[^<]{0,120}|Техническ[а-я]+\s+специфик[а-я]+", html_text)
    if m:
        title = re.sub(r"\s+", " ", m.group(0)).strip()[:140]
    buttons = re.findall(r'value="(Сформир[^"]+|Принять[^"]+|Согласи[^"]+|Сохран[^"]+)"', html_text)
    return {
        "len": len(html_text),
        "substantial": len(html_text) > 5000,
        "title": title,
        "has_generate": "сформировать" in low,
        "has_upload": ('type="file"' in low or "form_uploading" in low),
        "has_agree": ("принять соглаш" in low or "согласие" in low),
        "buttons": list(dict.fromkeys(buttons))[:5],
    }


def _lot_subdoc_actionable(sig: dict) -> bool:
    """True only when a fileless lot sub-doc page offers an action the bot can
    auto-complete into a file to sign: generate (Сформировать) or accept an
    agreement (Принять соглашение).

    A standing file-upload widget is NOT such a signal. The doc 3334 sub-doc
    pages carry an "attach a document" form even when no upload is required, so
    has_upload is always-on noise. A captured complete bid (HAR announce
    17049098) reached final submission (ajax_public_application) having signed
    only the pages with auto-attached files and left the upload-only page
    untouched — i.e. that page is the optional "attach your own document" slot,
    the natural end of the contiguous list. Treating has_upload as actionable
    turned that benign terminator into 4 useless retries + a false
    flow.lot_doc.needs_action alarm, while docs_next returned ok regardless.
    """
    return sig["has_generate"] or sig["has_agree"]


async def _probe_lot_page(gz, ns, ann, app_id, doc_id, app_lot, num, base_url, *,
                          with_retry: bool, sig_cache: dict | None = None,
                          pages_with_files: set[int] | None = None):
    """GET one тех.спец sub-doc page; if it carries auto-attached files, download +
    sign them. Returns (kind, payload):
        ("file", {fid: sig})      signable page (signatures computed, NOT saved)
        ("actionable", sig_dict)  fileless but offers generate/agree (maybe lagging)
        ("end", sig_dict)         fileless upload-only/blank -> end of the list
        ("unreachable", None)     the GET failed
    `with_retry` re-fetches an actionable-but-fileless page a few times (a freshly
    generated file can lag); the concurrent first pass uses with_retry=False so a
    lagging page doesn't hold the whole batch."""
    attempts = 4 if with_retry else 1
    last: tuple[str, dict] = ("end", {})
    for attempt in range(1, attempts + 1):
        try:
            html = await gz.get_show_doc(ann, app_id, doc_id, suffix=f"/{app_lot}/{num}")
        except SessionExpired:
            raise  # let the document session re-authenticate and retry
        except Exception:
            return "unreachable", None
        try:
            # quiet: a fileless page here is the EXPECTED end-of-list terminator,
            # not a failure — don't spam [error] + HTML dumps. Targets carry the
            # full two-segment download path (file_id + access token).
            targets = extract_download_targets(
                html, hint=f"lot_doc_{doc_id}_{app_lot}_{num}", quiet=True)
        except ValueError:
            sig = _describe_unsignable_page(html)
            if _lot_subdoc_actionable(sig):
                last = ("actionable", sig)
                if attempt < attempts:
                    log.info("flow.lot_doc.retry", doc=doc_id, app_lot=app_lot,
                             num=num, attempt=attempt, **sig)
                    retry_started = time.monotonic()
                    try:
                        await asyncio.sleep(1.5)
                    finally:
                        trace_event("wait", "Повтор чтения техспецификации", retry_started)
                    continue
                return last
            if sig["has_upload"]:
                # optional "attach your own document" slot, empty by default —
                # traceable but NOT a blocker (a complete bid submits without it).
                log.info("flow.lot_doc.upload_slot_skipped", doc=doc_id,
                         app_lot=app_lot, num=num, title=sig["title"])
            return "end", sig
        if pages_with_files is not None:
            pages_with_files.add(num)
        # Download + sign this page's files CONCURRENTLY — a multi-file page (the
        # тех.спец num=2 carries the 1.7MB + 2.2MB files) otherwise fetches them
        # one after another. The Kalkan signer serializes the actual GOST op
        # (~50ms); the file downloads are what overlap.
        referer = f"{base_url}/ru/application/show_doc/{ann}/{app_id}/{doc_id}/{app_lot}/{num}"
        # cache_key: the customer's тех.спец file is immutable per file_id, so a
        # signature made for it before (an earlier attempt / dry-run on this
        # tender) is reused WITHOUT downloading the file again.
        sig_list = await asyncio.gather(*[
            ns.sign_file(f"{base_url}{dl}", sig_cache=sig_cache, referer=referer,
                         cache_key=f"fid:{_fid}")
            for _fid, dl in targets
        ])
        sigs = {fid: s for (fid, _dl), s in zip(targets, sig_list)}

        log.info("flow.lot_doc.prepared", doc=doc_id, app_lot=app_lot, num=num,
                 files=[fid for fid, _dl in targets])
        return "file", sigs
    return last


async def _lot_sign_pages(gz, ns, ann, app_id, doc_id, app_lot, base_url,
                          max_nums: int = 10, probe_width: int = 3,
                          sig_cache: dict | None = None):
    """тех.спец (doc 3334): yield ``(num, {fid: sig})`` for every signable sub-doc
    page, in page order, AS SOON AS each one is downloaded + signed.

    The first ``probe_width`` pages are fetched CONCURRENTLY (num=2's big files
    download while num=1's do). Pages are contiguous: the first fileless
    upload-only/blank page ends the list. Yielding in order — instead of waiting
    for the whole window — lets the caller commit page 1 while a later page is
    still downloading. Once the pending pages are known to have files, start
    probing the next page without waiting for their downloads and signatures.
    """
    width = max(1, min(probe_width, max_nums))

    pages_with_files: set[int] = set()

    def _probe(n: int, *, with_retry: bool, use_cache: bool = True):
        return _probe_lot_page(gz, ns, ann, app_id, doc_id, app_lot, n, base_url,
                               with_retry=with_retry,
                               sig_cache=sig_cache if use_cache else None,
                               pages_with_files=pages_with_files)

    ahead = {n: asyncio.ensure_future(_probe(n, with_retry=False))
             for n in range(1, width + 1)}
    try:
        num = 1
        while num <= max_nums:
            if num in ahead:
                kind, payload = await ahead.pop(num)
                if kind == "actionable":
                    # probed without retry — re-probe WITH retry (lagging file)
                    kind, payload = await _probe(num, with_retry=True, use_cache=False)
            else:
                kind, payload = await _probe(num, with_retry=True)
            if kind == "file":
                nxt = max(ahead, default=num) + 1
                if nxt <= max_nums and pages_with_files.issuperset(ahead):
                    ahead[nxt] = asyncio.ensure_future(_probe(nxt, with_retry=False))
                yield num, payload
            elif kind == "actionable":
                # an action page that never produced a file — surface, then keep going
                log.error("flow.lot_doc.needs_action", doc=doc_id, app_lot=app_lot,
                          num=num, **payload)
            else:
                break  # "end" or "unreachable" -> past the last contiguous sub-doc
            num += 1
    finally:
        for task in ahead.values():
            if not task.done():
                task.cancel()
        # Observe failures from pages beyond the optional end-of-list slot.
        await asyncio.gather(*ahead.values(), return_exceptions=True)


async def _lot_sign_prepare(gz, ns, ann, app_id, doc_id, app_lot, base_url,
                            max_nums: int = 10, probe_width: int = 3,
                            sig_cache: dict | None = None):
    """тех.спец (doc 3334): GET + download + sign every sub-doc page, but DON'T
    save. Returns [(num, {fid: sig})] for _lot_sign_save to POST later.

    Why split: the save POST must be the last, uninterleaved write before docs_next
    (a concurrent save of another doc clobbers the тех.спец one). But the SLOW part
    — downloading the two multi-MB files and GOST-signing — is read-only.
    See _lot_sign_pages for the probing strategy.
    """
    saves: list[tuple[int, dict]] = []
    async with aclosing(_lot_sign_pages(
            gz, ns, ann, app_id, doc_id, app_lot, base_url,
            max_nums=max_nums, probe_width=probe_width, sig_cache=sig_cache)) as pages:
        async for num, payload in pages:
            saves.append((num, payload))
    return saves


async def _lot_sign_save(gz, ann, app_id, doc_id, app_lot, saves):
    """POST the prepared тех.спец signatures — sequential and LAST, so the save is
    the final uninterleaved write before docs_next (see _lot_sign_prepare).

    After each page's save we re-GET the тех.спец parent (/{doc_id}), mirroring the
    working manual capture (saved_signs_2: save page → GET /3334 → next page). The
    server recomputes draft completeness off that GET; saving the next page WITHOUT
    it was observed to drop the just-saved page's signatures (last-writer-wins) →
    docs_next then reports Приложение 15 unsigned. The GET is the commit barrier."""
    for num, sigs in saves:
        await gz.sign_lot_doc(ann, app_id, doc_id, app_lot, num, signatures=sigs)
        log.info("flow.lot_doc.signed", doc=doc_id, app_lot=app_lot, num=num, files=list(sigs))
        # commit barrier between saves (and before docs_next) — see docstring
        try:
            await gz.get_show_doc(ann, app_id, doc_id)
        except Exception:  # noqa: BLE001 — best-effort refresh, never fatal
            pass


async def _resave_tech_spec(gz, ns, params, app_id, app_lot_map, base_url):
    """Re-sign + re-save every тех.спец (lot_sign) doc, freshly. Invoked when
    docs_next reports the Приложение 15 still unsigned — the prior save didn't
    stick, so we redo the read→sign→save for each sub-doc page (now with no other
    writes racing it). Idempotent: re-signing an already-signed file just commits
    it again, so this is safe to run on every rejection."""
    ann = params.announce_id
    for spec in params.doc_plan:
        if spec.kind != "lot_sign":
            continue
        for app_lot in app_lot_map.values():
            saves = await _lot_sign_prepare(gz, ns, ann, app_id, spec.doc_id, app_lot, base_url)
            if saves:
                log.info("flow.lot_sign.resave.start", doc=spec.doc_id, app_lot=app_lot, n=len(saves))
                await _lot_sign_save(gz, ann, app_id, spec.doc_id, app_lot, saves)


def _is_valid_encr_info(raw: Optional[dict]) -> bool:
    """True if ajax_get_encr_info response contains actual tender encryption parameters."""
    if not raw or not isinstance(raw, dict):
        return False
    if raw.get("status") in ("unknown", "error", "fail"):
        return False
    return any(k in raw for k in (
        "pl_sum", "plSum", "planned_sum", "plannedSum", "plnSum",
        "d_sum", "dSum", "dumping_sum", "dumpingSum", "minPrice", "min_price",
        "info", "public_key", "publicKey", "salt", "sign"
    ))


async def _open_tumar(params, stack):
    """Build the popup auto-filler, then open + handshake the TumarCSP WS.

    Pulled out so it can be PRE-WARMED concurrently with document signing — the
    connect + SetAPIKey + GetVersion is ~2s that otherwise sits on the critical
    path right before the price popup. Returns the connected TumarCSP (entered
    into `stack` for cleanup)."""
    if not params.tumar_api_key:
        raise RuntimeError("gamma price needs tumar_api_key (or an injected TumarCSP)")
    autofill = None
    # On Windows the built-in filler enters the price into the provider's dialog.
    from .popup_filler import is_supported, make_autofiller
    if is_supported():
        pc = params.popup_coords or {}
        autofill = make_autofiller(
            field_xy=pc.get("field_xy"), ok_xy=pc.get("ok_xy"),
            field_offset=pc.get("field_offset"), ok_offset=pc.get("ok_offset"),
            detect_xy=pc.get("detect_xy"), detect_rgb=pc.get("detect_rgb"),
        )
        log.info("flow.gamma.autofill.enabled", forced=True,
                 config_flag=params.autofill_price_popup)
    else:
        log.warning("flow.gamma.autofill.unsupported",
                    hint="manual entry is available outside the Windows autofill path")
    return await stack.enter_async_context(
        TumarCSP(api_key=params.tumar_api_key, ws_url=params.tumar_ws_url, autofill=autofill)
    )


async def _gamma_price(gz, ns, params, app_id, stack, *, tumar_task=None):
    """Encrypt and sign each price for the current application."""
    ann = params.announce_id
    tumar = params.tumar
    reconnect_task = None
    if isinstance(tumar, TumarCSP) and not tumar.connected:
        # The local handshake can run alongside the portal's price-page read.
        reconnect_task = asyncio.create_task(tumar.ensure_connected())
        stack.push_async_callback(_cancel_task, reconnect_task)

    price_path = f"/ru/application/priceoffers/{ann}/{app_id}"
    try:
        po_html = await gz._get_page(price_path)
    except (httpx.TransportError, httpx.HTTPStatusError) as exc:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
            raise
        log.warning("flow.priceoffers.fetch_retry", detail=type(exc).__name__)
        po_html = await gz._get_page(price_path)
    # Bring-up only: the inline <script> shows how get_encr_info maps to the
    # TumarCSP Param. Off the hot path now (set ATAGOS_DEBUG_DUMPS=1 to re-enable).
    if os.environ.get("ATAGOS_DEBUG_DUMPS"):
        _dump(po_html, f"priceoffers_{ann}")
    pairs = _extract_gamma_pairs(po_html)
    if not pairs and 'current_method = "docs"' in po_html:
        # Some responses still point to the prior stage just after docs_next.
        po_html = await gz._get_page(price_path)
        pairs = _extract_gamma_pairs(po_html)
    if not pairs:
        # Common cause: still on /docs/ (docs_next never accepted) — the GET
        # to /priceoffers/ 302s there. Dump HTML and say so explicitly.
        dump = _dump(po_html, f"priceoffers_no_gamma_{ann}")
        on_docs = (
            "/application/docs/" in (po_html[:2000] if po_html else "")
            or 'current_method = "docs"' in (po_html or "")
            or "Подпишите" in (po_html or "")
        )
        if on_docs:
            raise RuntimeError(
                "priceoffers redirected to docs — documents still incomplete "
                f"(dump={dump})"
            )
        raise RuntimeError(
            f"no gamma (app_lot, lp_id) pairs on priceoffers page (dump={dump})"
        )
    log.info("flow.gamma.pairs", pairs=pairs)
    discovered_lp_ids = [lp_id for (_, lp_id) in pairs]
    t_gamma0 = time.time()

    # TumarCSP: injected (bot) > pre-warmed during docs > opened now.
    if tumar is not None:
        if getattr(tumar, "_autofill", None) is None:
            from .popup_filler import is_supported, make_autofiller
            if is_supported():
                pc = params.popup_coords or {}
                tumar._autofill = make_autofiller(
                    field_xy=pc.get("field_xy"), ok_xy=pc.get("ok_xy"),
                    field_offset=pc.get("field_offset"), ok_offset=pc.get("ok_offset"),
                    detect_xy=pc.get("detect_xy"), detect_rgb=pc.get("detect_rgb"),
                )
                log.info("flow.gamma.autofill.attached_to_injected_tumar")
    elif tumar_task is not None:
        try:
            tumar = await tumar_task            # connected while docs were signing
        except Exception as e:  # noqa: BLE001 — pre-warm failed; open it inline now
            log.warning("flow.gamma.prewarm_failed", err=str(e))
            tumar = await _open_tumar(params, stack)
    else:
        tumar = await _open_tumar(params, stack)
    if isinstance(tumar, TumarCSP):
        if reconnect_task is not None:
            await reconnect_task
        await tumar.ensure_connected()

    # Fallback source of pl_sum if ajax_get_encr_info doesn't carry it: the
    # planned price rendered next to each .offer_gamma input on this page.
    page_pl = _extract_planned_sums(po_html)

    # public_key (the per-tender EFC cert) lives in a hidden input on the price
    # page — ajax_get_encr_info doesn't return it, but EncryptOfferPrice needs it.
    pkm = re.search(r'id=["\']public_key["\'][^>]*\bvalue=["\']([^"\']+)["\']', po_html)
    page_public_key = pkm.group(1) if pkm else None
    if not page_public_key:
        log.warning("flow.gamma.no_public_key_on_page", ann=ann)

    async def _fetch_encr_info(lp_id: int) -> dict:
        version = tumar.version or "1.0.13.2285"
        raw = await gz.get_encr_info(ann, app_id, lp_id=lp_id, version=version)
        if not _is_valid_encr_info(raw):
            started = time.monotonic()
            await asyncio.sleep(0.5)
            trace_event("wait", "Повтор получения параметров шифрования", started)
            raw = await gz.get_encr_info(ann, app_id, lp_id=lp_id, version=version)
        return raw

    encrypted_by_pair: dict[tuple[int, int], str] = {}
    prices_by_lp: dict[int, float] = {}
    signatures = {}
    async with AsyncExitStack() as tasks:
        # A salt belongs to this application and price request. Fetch it once
        # per lot, after the portal accepts the documents, using the live version.
        encr_info = {}
        for lp_id in dict.fromkeys(discovered_lp_ids):
            task = asyncio.create_task(_fetch_encr_info(lp_id))
            tasks.push_async_callback(_cancel_task, task)
            encr_info[lp_id] = task

        for app_lot, lp_id in pairs:
            raw = await encr_info[lp_id]
            log.info("flow.get_encr_info.received", lp_id=lp_id, keys=sorted(raw))
            info = _parse_encr_info(
                raw, lp_id,
                override_price=params.price,
                floor_ratio=params.dumping_floor_ratio,
                page_pl_sum=page_pl.get(lp_id, 0.0),
                page_public_key=page_public_key,
            )
            prices_by_lp[lp_id] = info.d_sum
            enc = await tumar.encrypt_offer_price(info)
            encrypted_by_pair[(app_lot, lp_id)] = enc.encrypted_data

            # Sign alongside add_encrypt and, for multiple lots, the next popup.
            # Register each lot in order, then wait for every signature before
            # advancing the price stage.
            signing = asyncio.create_task(ns.sign_xml_batch({lp_id: enc.encrypted_data}))
            tasks.push_async_callback(_cancel_task, signing)
            signatures[lp_id] = signing
            await gz.add_encrypt(
                ann, app_id, item_id=lp_id,
                encrypted_data=enc.encrypted_data, session_key=enc.session_key,
                salt=info.salt, info=enc.info, sign=enc.sign,
            )
            if signing.done():
                signing.result()

        signs_by_lp = {
            lp_id: (encrypted, (await signatures[lp_id])[lp_id])
            for (_, lp_id), encrypted in encrypted_by_pair.items()
        }

    # 5. save all gamma signatures, then advance with the encrypted prices
    await gz.save_gamma_signs(ann, app_id, signs_by_lp=signs_by_lp)
    await gz.priceoffers_next_gamma(ann, app_id, prices=encrypted_by_pair)
    log.info("flow.gamma.done", dt_s=round(time.time() - t_gamma0, 3),
             n_lots=len(pairs))
    return discovered_lp_ids, prices_by_lp


def _extract_gamma_pairs(html_text: str) -> list[tuple[int, int]]:
    """Find (app_lot_id, buy_lot_point_id) pairs on the gamma priceoffers page.

    The .offer_gamma inputs carry app_lot_id + buy_lot_point_id attributes
    (see plugin_gamma.js). lp_id == buy_lot_point_id. Fall back to the old
    offer[lot][point] hidden-field pattern if present.
    """
    pairs: list[tuple[int, int]] = []
    # <input class="offer_gamma" app_lot_id="85015211" buy_lot_point_id="41391061" ...>
    for m in re.finditer(
        r'app_lot_id="(\d+)"[^>]*buy_lot_point_id="(\d+)"'
        r'|buy_lot_point_id="(\d+)"[^>]*app_lot_id="(\d+)"',
        html_text,
    ):
        if m.group(1):
            pairs.append((int(m.group(1)), int(m.group(2))))
        else:
            pairs.append((int(m.group(4)), int(m.group(3))))
    if not pairs:
        for a, b in re.findall(r"offer\[(\d+)\]\[(\d+)\]", html_text):
            pairs.append((int(a), int(b)))
    # dedupe, preserve order
    seen = set()
    return [p for p in pairs if not (p in seen or seen.add(p))]


def _num(v) -> float:
    """Parse a money value tolerant of spaces / NBSP / decimal comma."""
    if v in (None, ""):
        return 0.0
    s = str(v).replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def dumping_floor(pl_sum: float, ratio: float = 0.80) -> float:
    """The minimum allowed (anti-dumping) price = pl_sum * ratio, rounded UP to
    hundredths (kopecks). Round-UP (ceiling), NOT nearest — so we can never land
    below the real threshold and get disqualified.

    Verified against the live HAR: 2221307.14 * 0.80 = 1777045.712 -> 1777045.72.
    And the operator's example: 100.09 * 0.80 = 80.072 -> 80.08.
    """
    val = Decimal(str(pl_sum)) * Decimal(str(ratio))
    return float(val.quantize(Decimal("0.01"), rounding=ROUND_CEILING))


def _extract_planned_sums(html_text: str) -> dict[int, float]:
    """Best-effort: map buy_lot_point_id -> planned price from the price page.

    The .offer_gamma inputs carry the planned amount in a data attribute
    (data-planed-price / data-planned-price).
    """
    out: dict[int, float] = {}
    if not html_text:
        return out
    for m in re.finditer(
        r'buy_lot_point_id="(\d+)"[^>]*?data-plan+[a-z_-]*="([\d.,\s\xa0]+)"'
        r'|data-plan+[a-z_-]*="([\d.,\s\xa0]+)"[^>]*?buy_lot_point_id="(\d+)"',
        html_text,
    ):
        if m.group(1):
            out[int(m.group(1))] = _num(m.group(2))
        else:
            out[int(m.group(4))] = _num(m.group(3))

    for row in re.finditer(r'<tr[^>]*>(.*?)</tr>', html_text, re.DOTALL):
        row_html = row.group(1)
        lp_m = re.search(r'buy_lot_point_id="(\d+)"', row_html)
        if lp_m:
            lp_id = int(lp_m.group(1))
            if lp_id not in out or out[lp_id] <= 0.0:
                for td in re.finditer(r'<td[^>]*>([\d\s\xa0]+(?:[.,]\d{2})?)\s*(?:тг|тнг)?</td>', row_html):
                    val = _num(td.group(1))
                    if val > 1.0:
                        out[lp_id] = val
                        break
    return out


def _parse_encr_info(
    raw: dict,
    lp_id: int,
    *,
    override_price: Optional[float],
    floor_ratio: float = 0.80,
    page_pl_sum: float = 0.0,
    page_public_key: Optional[str] = None,
) -> EncrInfo:
    """Map the ajax_get_encr_info JSON to EncrInfo, deciding the price we bid.

    Field names mirror the EncryptOfferPrice Param the page forwards
    (public_key, sign, salt, pl_sum, d_sum, id_priceoffer, d_messageUp/Down);
    tolerant of camelCase.

    Price policy (operator's hard rule — bid the exact minimum, never above):
      * prefer the server's ``d_sum`` (authoritative — it's the value the real
        browser submission used);
      * else compute ceil(pl_sum * floor_ratio, 2dp) ourselves;
      * cross-check the two and log loudly on mismatch;
      * an explicit override_price is honoured only if inside [floor, pl_sum].
    """
    def pick(*keys, default=None):
        for k in keys:
            if k in raw and raw[k] not in (None, ""):
                return raw[k]
        return default

    # id_priceoffer / info field
    info_str = str(pick("info", default="") or "")
    info_parts = [p.strip() for p in info_str.split(";") if p.strip()]
    info_pl = _num(info_parts[1]) if len(info_parts) >= 2 else 0.0
    info_d = _num(info_parts[2]) if len(info_parts) >= 3 else 0.0

    # Real field names from a live ajax_get_encr_info: plnSum (ceiling),
    # minPrice (dumping floor), salt, sign, info ("<id>;<pl>;<min>;").
    pl_sum = _num(pick("pl_sum", "plSum", "planned_sum", "plannedSum", "plnSum")) or page_pl_sum or info_pl
    server_d = _num(pick("d_sum", "dSum", "dumping_sum", "dumpingSum", "minPrice", "min_price")) or info_d
    computed = dumping_floor(pl_sum, floor_ratio) if pl_sum else 0.0

    floor = server_d or computed
    if server_d and computed and abs(server_d - computed) > 0.005:
        log.warning("flow.floor.mismatch", lp_id=lp_id, server_d=server_d,
                    computed=computed, pl_sum=pl_sum, ratio=floor_ratio)
    if not floor:
        raise RuntimeError(
            f"could not determine dumping floor for lp {lp_id} "
            f"(pl_sum={pl_sum}, server_d={server_d}); ajax_get_encr_info={str(raw)[:300]}"
        )

    price = floor
    if override_price is not None:
        if floor <= float(override_price) <= (pl_sum or float(override_price)):
            price = float(override_price)
        else:
            log.warning("flow.override_out_of_range", override=override_price,
                        floor=floor, pl_sum=pl_sum, using=floor)
    log.info("flow.price.chosen", lp_id=lp_id, pl_sum=pl_sum, server_d=server_d,
             computed_floor=computed, bidding=price)

    # id_priceoffer: explicit field, else the first ";"-segment of `info`
    # ("566386_41384731;7537682.00;6030145.60;" -> "566386_41384731"), else
    # a fallback built from the lp id.
    info_str = str(pick("info", default="") or "")
    id_priceoffer = pick("id_priceoffer", "idPriceoffer")
    if not id_priceoffer and info_str:
        id_priceoffer = info_str.split(";", 1)[0]
    if not id_priceoffer:
        id_priceoffer = f"_{lp_id}"

    # Messages: synthesize in the goszakup format if the server didn't send them
    # (TumarCSP uses them for its range guard). minPrice is the floor we bid.
    msg_up = pick("d_messageUp", "messageUp") or (
        f"Введенное значение превышает плановую сумму {pl_sum} тнг" if pl_sum else "")
    msg_down = pick("d_messageDown", "messageDown") or (
        f"Введенное значение меньше демпинговой сумму {server_d or floor} тнг")

    # public_key: ajax_get_encr_info no longer returns it — it's a hidden input
    # on the price page (passed in as page_public_key). EncryptOfferPrice requires it.
    pub = pick("public_key", "publicKey", "cert", "certificate") or page_public_key

    return EncrInfo(
        sign=pick("sign", "signature", default=""),
        salt=pick("salt", default=""),
        pl_sum=pl_sum,
        d_sum=price,
        id_priceoffer=str(id_priceoffer),
        info=info_str,
        public_key=pub,
        message_up=msg_up,
        message_down=msg_down,
    )
