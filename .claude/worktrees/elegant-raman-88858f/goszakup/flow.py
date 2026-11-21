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
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from typing import Awaitable, Callable, Optional, Sequence

import structlog

from .client import AlreadyPrepared, GoszakupClient, SessionExpired
from .local_signer import KalkanSigner, LocalSigner
from .parser import extract_all_file_ids, extract_file_id, _dump
from .pipeline import _extract_app_lot_mapping  # reuse temka's robust mapper
from .state import StateStore, SubmissionState
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
    DocSpec(3327, "generate_sign", {"generate": "Принять соглашение"}),
    DocSpec(3335, "bankruptcy"),
    DocSpec(3328, "deposit"),
]

# docs_next polling. After the big Приложение 15 / Техническая спецификация files
# are signed, the server validates them ASYNCHRONOUSLY — for a few seconds it still
# answers "Подпишите ... Приложение 15" even though every file is signed. We poll
# docs_next until it confirms instead of firing once. Up to TRIES attempts spaced
# DELAY_S apart bounds the wait at ~(TRIES-1)*DELAY_S; it returns the instant the
# server accepts. (The old page-3 retry loop bought this settle time incidentally;
# removing it shrank the docs phase 17.7s->10s and exposed the race — identical
# client traces, status:error one run and status:ok the next.)
_DOCS_NEXT_TRIES = 6
_DOCS_NEXT_DELAY_S = 2.5


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
    # Auto-fill the TumarCSP price popup (Windows/pywinauto). Falls back to
    # manual entry if it can't (see popup_filler.py).
    autofill_price_popup: bool = False
    # Optional absolute-coordinate fallback for the popup (this machine only),
    # used when the window can't be enumerated. {field_xy, ok_xy, detect_xy,
    # detect_rgb} — see config tumarcsp.popup.*.
    popup_coords: Optional[dict] = None
    # login
    auto_login_account_password: Optional[str] = None


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


async def _docs_next_until_accepted(gz, ann, app_id, *, on_reject=None) -> tuple[str, bool]:
    """POST docs_next, polling until the server confirms every doc is accepted.

    The recurring failure: the server answers "Подпишите ... Приложение N" even
    though we just signed + saved those files — the save didn't stick (a concurrent
    or parallel-session clobbered write, or the server dropped it). Re-polling alone
    never fixes that (nothing changed), so when the rejection names a doc that still
    needs signing we RE-SAVE via ``on_reject`` (a serial re-commit on the main
    session — covers Приложение 1, not just the тех.спец) and only then poll again.
    This runs on the failure path only — a clean bid accepts on attempt 1 and never
    re-saves, so the happy path stays fast.

    Returns (last_body, accepted). On exhaustion it stays NON-FATAL (logs
    rejected_ignored and lets the price step report the real problem), matching the
    prior by-request behaviour.
    """
    import json as _json

    body = ""
    for attempt in range(1, _DOCS_NEXT_TRIES + 1):
        dn = await gz.docs_next(ann, app_id)
        body = dn.text
        try:
            dj = _json.loads(body)
        except (ValueError, TypeError):
            dj = None
        accepted = isinstance(dj, dict) and str(dj.get("status")) in ("1", "ok")
        log.info("flow.docs_next", attempt=attempt, status=dn.status_code,
                 len=len(body), accepted=accepted, body=body[:300])
        if accepted:
            return body, True
        msg = dj.get("message") if isinstance(dj, dict) else None
        if attempt < _DOCS_NEXT_TRIES:
            log.info("flow.docs_next.retry", attempt=attempt,
                     message=(msg or body[:200])[:200])
            # Self-heal: the server still reports a doc unsigned — a save didn't
            # take (concurrent / parallel-session clobber). Re-commit before the
            # next poll. We re-save on any signature rejection now (not just
            # тех.спец) so Приложение 1 is covered too.
            if on_reject is not None and _needs_resave(msg):
                try:
                    await on_reject()
                    log.info("flow.docs_next.resaved", attempt=attempt)
                except Exception as e:  # noqa: BLE001 — re-save best-effort, keep polling
                    log.warning("flow.docs_next.resave_failed", attempt=attempt, err=str(e))
            await asyncio.sleep(_DOCS_NEXT_DELAY_S)
        else:
            _dump(body, f"docs_next_rejected_{ann}")
            log.warning("flow.docs_next.rejected_ignored", message=(msg or body[:400]))
    return body, False


async def submit_bid(
    *,
    params: AtagosBidParams,
    ci_session: str,
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
            # Initial login is race-free (no probes running yet) — do it directly
            # on gz so the shared jar holds the authenticated session.
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

        # Pre-warm the parallel-doc session pool UP FRONT (before the poll) so the
        # doc phase pays ZERO login latency — one logged-in session per document,
        # ready the moment the tender opens. During a long pre-T0 wait a pool
        # session can outlive its ~5-min cabinet cookie; a stale one is re-logged-in
        # on use (_save_parallel_sessions), so this stays correct either way. Mode B
        # only (serial mode never spawns extra sessions).
        doc_pool: list = []
        if (params.doc_save_mode == "parallel_sessions"
                and params.auto_login_account_password and params.signer is not None):
            doc_pool = await _prewarm_doc_pool(gz, params, stack)

        # === PRE-T0 ===
        if pre_t0_poll:
            await gz.poll_announce_ready(
                params.announce_id, interval_s=poll_interval_s, relogin=relogin_cb)
        state.timestamps["t0"] = time.time()
        state_store.save(state)
        # Tender just opened — let the caller announce it (e.g. Telegram
        # "тендер открыт, начинаю подачу") before the submission burst.
        if on_open is not None:
            try:
                await on_open()
            except Exception:  # noqa: BLE001 — a notification must never sink a bid
                log.warning("flow.on_open.failed", announce=params.announce_id)

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
        state_store.save(state)
        log.info("flow.created", app_id=app_id)

        await gz.add_lots(params.announce_id, app_id, params.tender_lot_ids)
        await gz.lots_next(params.announce_id, app_id)
        state.step = "lots_added"
        state_store.save(state)

        # === discover tender_lot -> app_lot ===
        app_lot_map = await _discover_app_lots(gz, params, app_id)
        log.info("flow.app_lot_map", mapping=app_lot_map)

        # Pre-warm TumarCSP concurrently with document signing — its connect +
        # SetAPIKey + GetVersion (~2s) otherwise lands on the critical path right
        # before the price popup. Only when we'll actually open one here (have an
        # api_key and no injected instance). _gamma_price awaits this task; the
        # except below cancels it if an earlier step fails first.
        tumar_task = None
        if params.tumar is None and params.tumar_api_key:
            tumar_task = asyncio.ensure_future(_open_tumar(params, stack))
            log.info("flow.tumar.prewarm.start")

        try:
            # === documents ===
            save_ops = await _process_documents(
                gz, ns, params, app_id, app_lot_map, beneficiary, base_url,
                doc_pool=doc_pool,
            )
            state.step = "docs_signed"
            state.timestamps["docs_signed"] = time.time()
            state_store.save(state)

            # Land on the docs page before advancing — the working manual capture
            # goes save -> GET /docs -> docs_next, and the GET appears to make the
            # server recompute draft completeness off the just-saved тех.спец signs.
            try:
                await gz._get_page(f"/ru/application/docs/{params.announce_id}/{app_id}")
            except Exception:  # noqa: BLE001 — best-effort refresh, never fatal
                pass

            # Then poll docs_next until the server confirms. Should pass on attempt
            # 1; the poll stays a non-fatal safety net (the price step reports the
            # real problem instead of a cryptic "no gamma pairs"). If it reports a
            # doc still unsigned, a save didn't stick (concurrent / parallel-session
            # clobber) — self-heal by re-committing SERIALLY on the main session:
            # first re-POST every prepared save (covers Приложение 1 + others, тех.
            # спец last), then the proven тех.спец re-prepare for belt-and-braces.
            async def _resave_cb():  # noqa: E306
                await _save_serial(gz, params.announce_id, app_id, save_ops)
                await _resave_tech_spec(gz, ns, params, app_id, app_lot_map, base_url)
            await _docs_next_until_accepted(
                gz, params.announce_id, app_id, on_reject=_resave_cb)

            # === gamma price ===
            await _gamma_price(gz, ns, params, app_id, app_lot_map, base_url, stack,
                               tumar_task=tumar_task)
        except BaseException:
            if tumar_task is not None and not tumar_task.done():
                tumar_task.cancel()
            raise
        state.step = "signed"
        state.timestamps["signed"] = time.time()
        state_store.save(state)

        # === final submit (gated) ===
        if params.dry_run:
            log.warning("DRY_RUN: skipping ajax_public_application", app_id=app_id)
            state.step = "dry_run_complete"
            state_store.save(state)
            return state

        result = await gz.public_application(params.announce_id, app_id)
        log.info("flow.public_application.result", result=result)
        state.step = "public_ok" if result.get("status") in ("ok", 1, "1") else "public_error"
        if state.step == "public_error":
            state.error = result.get("message") or str(result)[:500]
        state.timestamps["public"] = time.time()
        state_store.save(state)

    return state


async def _discover_app_lots(gz, params, app_id) -> dict[int, int]:
    # Try the per-lot doc page first — empirically it carries the
    # tender_lot -> app_lot mapping and resolves in ONE GET (the /docs and /lots
    # pages don't, and /lots just 302s back to /docs — pure waste). Fall through
    # to the others only as a belt-and-braces fallback.
    lot_doc = next((s.doc_id for s in params.doc_plan if s.kind == "lot_sign"), 3334)
    ann = params.announce_id
    candidates = (
        f"/ru/application/show_doc/{ann}/{app_id}/{lot_doc}",
        f"/ru/application/docs/{ann}/{app_id}",
        f"/ru/application/lots/{ann}/{app_id}",
        f"/ru/application/create/{ann}",
    )
    app_lot_map: dict[int, int] = {}
    html_text = ""
    for cand in candidates:
        html_text = await gz._get_page(cand)
        got = _extract_app_lot_mapping(html_text, params.tender_lot_ids)
        if got:
            app_lot_map.update(got)
            if len(app_lot_map) == len(params.tender_lot_ids):
                return app_lot_map
    if len(app_lot_map) != len(params.tender_lot_ids):
        if 'current_method = "preview"' in html_text or "/application/preview/" in html_text:
            raise AlreadyPrepared(
                f"draft {app_id} already at /preview/ stage", app_id=app_id
            )
        _dump(html_text, f"app_lot_miss_{params.announce_id}")
        raise RuntimeError(
            f"could not resolve app_lot for all tender lots (got {app_lot_map}); "
            "HTML dumped under ~/.atagos/logs/"
        )
    return app_lot_map


def _first_file_id(html_text: str) -> Optional[int]:
    """Non-raising file_id lookup (parser.extract_file_id raises + dumps)."""
    from .parser import _iter_file_ids
    ids = list(dict.fromkeys(_iter_file_ids(html_text)))
    return ids[0] if ids else None


async def _generate_doc_fid(gz, ann, app_id, doc_id, fields, *, hint: str) -> int:
    """Generate a doc and return the produced file_id.

    The browser reads the new file_id from the generate-POST **response** (the
    page re-renders with download_file/<id> + the sign UI). A fresh GET shows
    the pre-generate state (still the "Сформировать документ" button) — which is
    why parsing a separate GET found nothing. So: parse the POST response first,
    fall back to a GET, and only then dump+raise (on the larger page).
    """
    r = await gz.show_doc_generate(ann, app_id, doc_id, fields=fields)
    fid = _first_file_id(r.text)
    if fid is not None:
        return fid
    html = await gz.get_show_doc(ann, app_id, doc_id)
    fid = _first_file_id(html)
    if fid is not None:
        return fid
    # neither had it — dump the more complete page so we can teach the parser
    best = r.text if len(r.text) >= len(html) else html
    return extract_file_id(best, hint=hint)  # raises ValueError + dumps HTML


@dataclass
class _SaveOp:
    """One deferred signature-commit POST. ``run(client)`` issues the save on the
    given client — the shared gz (serial) or a per-doc sibling (parallel sessions).
    Deferring the commit is what lets us serialize it (GET barrier between saves) or
    isolate it per session, instead of racing the saves inside the prepare batch."""
    label: str
    doc_id: int
    run: Callable[["GoszakupClient"], Awaitable]


async def _prepare_doc(gz, ns, params, app_id, app_lot_map, beneficiary, base_url, spec):
    """Do everything for one doc EXCEPT the final signature-commit save: generate /
    upload / describe / download / GOST-sign. Returns the deferred _SaveOp(s).

    Runs on whichever (client, signer) pair is passed — gz/ns for the shared-session
    prepare, or a sibling/sibling-signer for a parallel-session full flow."""
    ann = params.announce_id

    async def sign_url(fid: int) -> str:
        return await ns.sign_file(f"{base_url}/files/download_file/{fid}/")

    if spec.kind == "generate_sign":
        fid = await _generate_doc_fid(
            gz, ann, app_id, spec.doc_id, spec.generate_fields, hint=f"doc_{spec.doc_id}")
        sig = await sign_url(fid)
        log.info("flow.doc.prepared", doc=spec.doc_id, fid=fid)
        return [_SaveOp(
            f"doc_{spec.doc_id}", spec.doc_id,
            lambda c, d=spec.doc_id, f=fid, s=sig:
                c.show_doc_save_signed(ann, app_id, d, file_id=f, signature_b64=s))]

    if spec.kind == "beneficiary":
        signed_fid, sig = await _beneficiary_prepare(
            gz, ns, params, app_id, spec, app_lot_map, beneficiary, base_url)
        return [_SaveOp(
            f"beneficiary_{spec.doc_id}", spec.doc_id,
            lambda c, d=spec.doc_id, f=signed_fid, s=sig:
                c.show_doc_save_signed(ann, app_id, d, file_id=f, signature_b64=s))]

    if spec.kind == "bankruptcy":
        await gz.bankruptcy_gen(ann, app_id, doc_id=spec.doc_id)
        html = await gz.get_show_doc(ann, app_id, spec.doc_id)
        fid = extract_file_id(html, hint="bankruptcy")
        sig = await sign_url(fid)
        log.info("flow.bankruptcy.prepared", doc=spec.doc_id, fid=fid)
        return [_SaveOp(
            f"bankruptcy_{spec.doc_id}", spec.doc_id,
            lambda c, d=spec.doc_id, f=fid, s=sig:
                c.bankruptcy_save(ann, app_id, doc_id=d, file_id=f, signature_b64=s))]

    if spec.kind == "deposit":
        ops: list[_SaveOp] = []
        for app_lot in app_lot_map.values():
            await gz.deposit_add(ann, app_id, doc_id=spec.doc_id, lot_id=app_lot)
            ops.append(_SaveOp(
                f"deposit_{spec.doc_id}_{app_lot}", spec.doc_id,
                lambda c, d=spec.doc_id, lot=app_lot:
                    c.deposit_save(ann, app_id, doc_id=d, lot_id=lot)))
        log.info("flow.deposit.prepared", doc=spec.doc_id, lots=list(app_lot_map.values()))
        return ops

    if spec.kind == "lot_sign":
        # тех.спец / Приложение 15: GET + download + GOST-sign each sub-doc page
        # (read-only, parallel inside), defer each page's save POST.
        ops = []
        for app_lot in app_lot_map.values():
            saves = await _lot_sign_prepare(gz, ns, ann, app_id, spec.doc_id, app_lot, base_url)
            for num, sigs in saves:
                ops.append(_SaveOp(
                    f"lot_{spec.doc_id}_{app_lot}_{num}", spec.doc_id,
                    lambda c, d=spec.doc_id, lot=app_lot, n=num, s=sigs:
                        c.sign_lot_doc(ann, app_id, d, lot, n, signatures=s)))
        return ops

    raise ValueError(f"unknown doc kind: {spec.kind!r}")


async def _process_documents(gz, ns, params, app_id, app_lot_map, beneficiary, base_url,
                             doc_pool=None):
    """Sign + commit every document; return the prepared _SaveOp list (so the
    docs_next self-heal can re-commit serially on the main session).

    PREPARE (concurrent): generate / upload / download / GOST-sign every doc —
      read-heavy, independent, overlaps the big тех.спец downloads. ~25s → ~6-8s.
    SAVE (params.doc_save_mode): the commit POSTs are where the server recomputes
      the application's draft completeness, so a concurrent save drops another's
      just-saved signature (Приложение 1 / Приложение 15 "не подписано").
        "serial" (A, default): one save at a time on gz, GET barrier between,
          тех.спец last — никаких гонок. Fixes the drops for good.
        "parallel_sessions" (B): each doc on its own logged-in session (from the
          pre-warmed `doc_pool`), parallel.
    """
    ann = params.announce_id
    mode = params.doc_save_mode

    if mode == "parallel_sessions":
        if params.auto_login_account_password and params.signer is not None:
            return await _save_parallel_sessions(
                gz, params, app_id, app_lot_map, beneficiary, base_url, doc_pool or [])
        log.warning("flow.doc_save_mode.parallel_unavailable",
                    reason="needs auto_login_account_password + signer; using serial")

    # A — prepare all concurrently, commit serially (тех.спец last).
    results: dict[int, list[_SaveOp]] = {}

    async def _prep(i: int, spec: DocSpec) -> None:
        results[i] = await _prepare_doc(
            gz, ns, params, app_id, app_lot_map, beneficiary, base_url, spec)

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
    """Log in one sibling session PER document UP FRONT (before the T0 poll) and
    register them on `stack` for cleanup. Logins run concurrently; the shared
    Kalkan signer serializes the GOST ops. Returns [(client, signer), …] for
    _save_parallel_sessions — so the doc phase pays no login latency. A session
    that goes stale during a long pre-T0 wait is re-logged-in on use, so prewarming
    is a pure latency win, never a correctness risk."""
    acc_pw = params.auto_login_account_password
    n = len(params.doc_plan)

    async def _spawn() -> tuple:
        sib = gz.clone_unauthenticated()
        await stack.enter_async_context(sib)
        sib_ns = LocalSigner(kalkan_signer=params.signer, http_client=sib._http)
        await sib.login(account_password=acc_pw, kalkan_signer=sib_ns)
        return (sib, sib_ns)

    t0 = time.time()
    log.info("flow.doc_pool.prewarm.start", n=n)
    pool = list(await asyncio.gather(*(_spawn() for _ in range(n))))
    log.info("flow.doc_pool.prewarm.done", n=len(pool), elapsed_s=round(time.time() - t0, 2))
    return pool


async def _save_parallel_sessions(gz, params, app_id, app_lot_map, beneficiary, base_url, pool):
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
    """
    ann = params.announce_id
    acc_pw = params.auto_login_account_password
    specs = list(params.doc_plan)
    pool = list(pool or [])
    all_ops: list[_SaveOp] = []
    lock = asyncio.Lock()
    spawned: list = []   # sessions created here (no/short pool) -> we close them

    async def _session_for(i: int) -> tuple:
        if i < len(pool):
            return pool[i]
        sib = gz.clone_unauthenticated()
        sib_ns = LocalSigner(kalkan_signer=params.signer, http_client=sib._http)
        await sib.login(account_password=acc_pw, kalkan_signer=sib_ns)
        spawned.append(sib)
        return (sib, sib_ns)

    async def _run_doc(sib, sib_ns, spec: DocSpec) -> list:
        ops = await _prepare_doc(
            sib, sib_ns, params, app_id, app_lot_map, beneficiary, base_url, spec)
        # A doc with several saves (тех.спец sub-pages) still needs the GET barrier
        # between its own saves — but only within this session; other docs' sessions
        # run in parallel.
        for op in ops:
            await op.run(sib)
            log.info("flow.doc.saved.isolated", label=op.label, doc=op.doc_id)
            try:
                await sib.get_show_doc(ann, app_id, op.doc_id)  # commit barrier
            except Exception:  # noqa: BLE001 — best-effort, never fatal
                pass
        return ops

    async def _one(i: int, spec: DocSpec) -> None:
        sib, sib_ns = await _session_for(i)
        try:
            ops = await _run_doc(sib, sib_ns, spec)
        except SessionExpired:
            # pool session died during a long pre-T0 wait — re-login in place + retry
            log.warning("flow.doc_session.expired.relogin", doc=spec.doc_id)
            await sib.login(account_password=acc_pw, kalkan_signer=sib_ns)
            ops = await _run_doc(sib, sib_ns, spec)
        async with lock:
            all_ops.extend(ops)

    t0 = time.time()
    log.info("flow.docs.parallel_sessions.start", n=len(specs), pooled=len(pool))
    try:
        await asyncio.gather(*(_one(i, s) for i, s in enumerate(specs)))
    finally:
        for sib in spawned:
            await sib.aclose()
    log.info("flow.docs.parallel_sessions.done", elapsed_s=round(time.time() - t0, 2))
    return all_ops


async def _beneficiary_prepare(gz, ns, params, app_id, spec, app_lot_map, beneficiary, base_url):
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
    fid = await _generate_doc_fid(
        gz, ann, app_id, spec.doc_id, spec.generate_fields,
        hint=f"beneficiary_doc_{spec.doc_id}",
    )
    sig = await ns.sign_file(f"{base_url}/files/download_file/{fid}/")
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


async def _probe_lot_page(gz, ns, ann, app_id, doc_id, app_lot, num, base_url, *, with_retry: bool):
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
        except Exception:
            return "unreachable", None
        try:
            # quiet: a fileless page here is the EXPECTED end-of-list terminator,
            # not a failure — don't spam [error] parser.no_file_ids + HTML dumps.
            fids = extract_all_file_ids(html, hint=f"lot_doc_{doc_id}_{app_lot}_{num}", quiet=True)
        except ValueError:
            sig = _describe_unsignable_page(html)
            if _lot_subdoc_actionable(sig):
                last = ("actionable", sig)
                if attempt < attempts:
                    log.info("flow.lot_doc.retry", doc=doc_id, app_lot=app_lot,
                             num=num, attempt=attempt, **sig)
                    await asyncio.sleep(1.5)
                    continue
                return last
            if sig["has_upload"]:
                # optional "attach your own document" slot, empty by default —
                # traceable but NOT a blocker (a complete bid submits without it).
                log.info("flow.lot_doc.upload_slot_skipped", doc=doc_id,
                         app_lot=app_lot, num=num, title=sig["title"])
            return "end", sig
        # Download + sign this page's files CONCURRENTLY — a multi-file page (the
        # тех.спец num=2 carries the 1.7MB + 2.2MB files) otherwise fetches them
        # one after another. The Kalkan signer serializes the actual GOST op
        # (~50ms); the file downloads are what overlap.
        sig_list = await asyncio.gather(*[
            ns.sign_file(f"{base_url}/files/download_file/{fid}/") for fid in fids
        ])
        sigs = dict(zip(fids, sig_list))
        log.info("flow.lot_doc.prepared", doc=doc_id, app_lot=app_lot, num=num, files=list(fids))
        return "file", sigs
    return last


async def _lot_sign_prepare(gz, ns, ann, app_id, doc_id, app_lot, base_url,
                            max_nums: int = 10, probe_width: int = 3):
    """тех.спец (doc 3334): GET + download + sign every sub-doc page, but DON'T
    save. Returns [(num, {fid: sig})] for _lot_sign_save to POST later.

    Why split: the save POST must be the last, uninterleaved write before docs_next
    (a concurrent save of another doc clobbers the тех.спец one). But the SLOW part
    — downloading the two multi-MB files and GOST-signing — is read-only.

    Speed: probe the first `probe_width` pages CONCURRENTLY so num=2's big files
    download while num=1 does, instead of one-after-another. Then take the
    contiguous signable prefix from num=1 (sub-docs are contiguous; the first
    fileless upload-only/blank page ends the list). Tenders with more sub-docs than
    the probe window fall back to sequential fetches past it.
    """
    width = max(1, min(probe_width, max_nums))
    probed = await asyncio.gather(*[
        _probe_lot_page(gz, ns, ann, app_id, doc_id, app_lot, n, base_url, with_retry=False)
        for n in range(1, width + 1)
    ])
    by_num = {n: probed[n - 1] for n in range(1, width + 1)}

    saves: list[tuple[int, dict]] = []
    num = 1
    while num <= max_nums:
        kind, payload = by_num.get(num) or await _probe_lot_page(
            gz, ns, ann, app_id, doc_id, app_lot, num, base_url, with_retry=True)
        if kind == "actionable" and num in by_num:
            # probed concurrently without retry — re-probe WITH retry (lagging file)
            kind, payload = await _probe_lot_page(
                gz, ns, ann, app_id, doc_id, app_lot, num, base_url, with_retry=True)
        if kind == "file":
            saves.append((num, payload))
        elif kind == "actionable":
            # an action page that never produced a file — surface, then keep going
            log.error("flow.lot_doc.needs_action", doc=doc_id, app_lot=app_lot,
                      num=num, **payload)
        else:
            break  # "end" or "unreachable" -> past the last contiguous sub-doc
        num += 1
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


async def _open_tumar(params, stack):
    """Build the popup auto-filler, then open + handshake the TumarCSP WS.

    Pulled out so it can be PRE-WARMED concurrently with document signing — the
    connect + SetAPIKey + GetVersion is ~2s that otherwise sits on the critical
    path right before the price popup. Returns the connected TumarCSP (entered
    into `stack` for cleanup)."""
    if not params.tumar_api_key:
        raise RuntimeError("gamma price needs tumar_api_key (or an injected TumarCSP)")
    autofill = None
    # Force-enable popup auto-fill whenever the platform supports it (Windows +
    # pywinauto), regardless of the config flag — the popup must be driven
    # automatically. config tumarcsp.popup.* coords are the fallback.
    from .popup_filler import is_supported, make_autofiller
    if is_supported():
        pc = params.popup_coords or {}
        autofill = make_autofiller(
            field_xy=pc.get("field_xy"), ok_xy=pc.get("ok_xy"),
            field_offset=pc.get("field_offset"), ok_offset=pc.get("ok_offset"),
            detect_xy=pc.get("detect_xy"), detect_rgb=pc.get("detect_rgb"),
        )
        log.info("flow.gamma.autofill.enabled", forced=True,
                 config_flag=params.autofill_price_popup,
                 coords_fallback=bool(pc.get("field_xy")))
    else:
        log.warning("flow.gamma.autofill.unsupported",
                    hint="not Windows or pywinauto missing — manual price entry")
    return await stack.enter_async_context(
        TumarCSP(api_key=params.tumar_api_key, ws_url=params.tumar_ws_url, autofill=autofill)
    )


async def _gamma_price(gz, ns, params, app_id, app_lot_map, base_url, stack, tumar_task=None):
    """Encrypt + sign the price for every (app_lot, lp_id) pair."""
    ann = params.announce_id

    # Discover (app_lot, lp_id) pairs from the priceoffers page.
    po_html = await gz._get_page(f"/ru/application/priceoffers/{ann}/{app_id}")
    # Bring-up only: the inline <script> shows how get_encr_info maps to the
    # TumarCSP Param. Off the hot path now (set ATAGOS_DEBUG_DUMPS=1 to re-enable).
    if os.environ.get("ATAGOS_DEBUG_DUMPS"):
        _dump(po_html, f"priceoffers_{ann}")
    pairs = _extract_gamma_pairs(po_html)
    if not pairs:
        raise RuntimeError("no gamma (app_lot, lp_id) pairs on priceoffers page")
    log.info("flow.gamma.pairs", pairs=pairs)

    # TumarCSP: injected (bot) > pre-warmed during docs > opened now.
    if params.tumar is not None:
        tumar = params.tumar
    elif tumar_task is not None:
        try:
            tumar = await tumar_task            # connected while docs were signing
        except Exception as e:  # noqa: BLE001 — pre-warm failed; open it inline now
            log.warning("flow.gamma.prewarm_failed", err=str(e))
            tumar = await _open_tumar(params, stack)
    else:
        tumar = await _open_tumar(params, stack)

    # Fallback source of pl_sum if ajax_get_encr_info doesn't carry it: the
    # planned price rendered next to each .offer_gamma input on this page.
    page_pl = _extract_planned_sums(po_html)

    # public_key (the per-tender EFC cert) lives in a hidden input on the price
    # page — ajax_get_encr_info doesn't return it, but EncryptOfferPrice needs it.
    pkm = re.search(r'id=["\']public_key["\'][^>]*\bvalue=["\']([^"\']+)["\']', po_html)
    page_public_key = pkm.group(1) if pkm else None
    if not page_public_key:
        log.warning("flow.gamma.no_public_key_on_page", ann=ann)

    encrypted_by_pair: dict[tuple[int, int], str] = {}
    signs_by_lp: dict[int, tuple[str, str]] = {}

    for (app_lot, lp_id) in pairs:
        # 1. server hands us the recipient cert + price range
        info_raw = await gz.get_encr_info(ann, app_id, lp_id=lp_id, version=tumar.version or "")
        log.info("flow.get_encr_info.raw", lp_id=lp_id, keys=sorted(info_raw), resp=str(info_raw)[:500])
        info = _parse_encr_info(
            info_raw, lp_id,
            override_price=params.price,
            floor_ratio=params.dumping_floor_ratio,
            page_pl_sum=page_pl.get(lp_id, 0.0),
            page_public_key=page_public_key,
        )
        # 2. TumarCSP seals the price (we bid d_sum = the dumping floor minimum)
        enc = await tumar.encrypt_offer_price(info)
        # 3. register the sealed price
        await gz.add_encrypt(
            ann, app_id, item_id=lp_id,
            encrypted_data=enc.encrypted_data, session_key=enc.session_key,
            salt=info.salt, info=enc.info, sign=enc.sign,
        )
        # 4. sign the sealed blob with the bidder's ЭЦП (CAdES-T w/ TSA — matches
        #    the NURSign "multitext" CMS in the HAR). sign_xml_batch signs each
        #    value via Kalkan sign_text_with_tsa; the value here is the encrypted
        #    text, exactly what NURSign signed.
        signed = await ns.sign_xml_batch({lp_id: enc.encrypted_data})
        signs_by_lp[lp_id] = (enc.encrypted_data, signed[lp_id])
        encrypted_by_pair[(app_lot, lp_id)] = enc.encrypted_data

    # 5. save all gamma signatures, then advance with the encrypted prices
    await gz.save_gamma_signs(ann, app_id, signs_by_lp=signs_by_lp)
    await gz.priceoffers_next_gamma(ann, app_id, prices=encrypted_by_pair)


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
    (data-planed-price in the legacy priceoffers.js). Used only as a pl_sum
    fallback when ajax_get_encr_info omits it.
    """
    out: dict[int, float] = {}
    for m in re.finditer(
        r'buy_lot_point_id="(\d+)"[^>]*?data-planed[-_]price="([\d.,\s\xa0]+)"'
        r'|data-planed[-_]price="([\d.,\s\xa0]+)"[^>]*?buy_lot_point_id="(\d+)"',
        html_text,
    ):
        if m.group(1):
            out[int(m.group(1))] = _num(m.group(2))
        else:
            out[int(m.group(4))] = _num(m.group(3))
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

    # Real field names from a live ajax_get_encr_info: plnSum (ceiling),
    # minPrice (dumping floor), salt, sign, info ("<id>;<pl>;<min>;").
    pl_sum = _num(pick("pl_sum", "plSum", "planned_sum", "plannedSum", "plnSum")) or page_pl_sum
    server_d = _num(pick("d_sum", "dSum", "dumping_sum", "dumpingSum", "minPrice", "min_price"))
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
