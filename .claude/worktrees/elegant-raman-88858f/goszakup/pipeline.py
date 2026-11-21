"""Submission orchestrator.

Order of operations (timings are with NCALayer in the loop; cut to ~1/8
when local GOST signing lands in Phase 2):

    PRE-T0  (run while waiting for tender to open)
        - ensure ci_session is alive
        - open NursignClient WS to NCALayer, validate plugin
        - poll /ru/announce/index/{ann} until 'Доступные действия' visible

    T0  (the moment the button appears)
        1. POST ajax_create_application  -> application_id
        2. POST ajax_add_lots
        3. POST ajax_lots_next
        4. GET  application/create/{ann}  -> discover app_lot_id mapping
        5. Sign Заявка на участие (doc 741)
        6. Sign Перечень категорий (doc 705)
        7. For each lot: GET sign_task/{lot}/709, sign every file_id found
        8. (optional) copy_data_docs from another tender for quals
        9. POST bankruptcy_gen, GET doc, sign, POST bankruptcy_save
       10. For each lot: deposit_add + deposit_save (typeDoc=3)
       11. POST ajax_docs_next
       12. GET priceoffers page -> discover (app_lot, point) pairs
       13. POST ajax_check_priceoffers
       14. NCALayer.sign_xml_batch (multitext+TSA) for all points
       15. POST ajax_add_priceoffers
       16. POST ajax_priceoffers_next
       17. POST ajax_public_application       <-- gated by dry_run
"""

from __future__ import annotations

import asyncio
import re
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Literal, Optional, Sequence

import structlog

from .client import AlreadyPrepared, GoszakupClient
from .local_signer import KalkanSigner, LocalSigner
from .nursign import NursignClient
from .parser import (
    extract_all_file_ids,
    extract_file_id,
    extract_priceoffer_structure,
)
from .state import StateStore, SubmissionState

log = structlog.get_logger("pipeline")

DOC_APPLICATION = 741  # Заявка на участие в конкурсе для ЮЛ
DOC_CATEGORIES = 705   # Перечень категорий получателей услуги
DOC_TASK = 709         # Техническое задание (per-lot, signed via sign_task/)
DOC_BANKRUPTCY = 3240  # Декларация о банкротстве + квалификация работников
DOC_DEPOSIT = 719      # Обеспечение заявки


@dataclass
class BidParams:
    announce_id: str
    tender_lot_ids: list[int]
    price: int
    # Optional: copy qualification data from this prior tender
    qualification_source_anno: Optional[str] = None
    # When True: do everything except the final ajax_public_application.
    dry_run: bool = False
    # Crypto backend. "local" uses our Kalkan helper (fast, no popup).
    # "ncalayer" uses NURSign over WebSocket (legacy, popup per sign).
    backend: Literal["local", "ncalayer"] = "local"
    # For backend="local" — either (p12_path, ecp_password) for a one-shot
    # signer (CLI) or `signer` for an externally-managed long-lived one (bot).
    p12_path: Optional[str] = None
    ecp_password: Optional[str] = None
    signer: Optional[KalkanSigner] = None
    # If set, run the 5-step ECP login flow first (same http client),
    # avoiding session race with concurrent Chrome usage. ~3.5s extra.
    auto_login_account_password: Optional[str] = None


def _build_priceoffer_xml(
    *, announce_id: str, tender_lot_id: int, point_id: int, price: int
) -> str:
    # Exact format observed in HAR (no whitespace between tags).
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<priceoffer>"
        f"<trd_buy_id>{announce_id}</trd_buy_id>"
        f"<trd_buy_lot_id>{tender_lot_id}</trd_buy_lot_id>"
        f"<trd_buy_lot_point_id>{point_id}</trd_buy_lot_point_id>"
        f"<price>{price}</price>"
        "</priceoffer>"
    )


def _extract_app_lot_mapping(
    html_text: str, tender_lot_ids: Sequence[int]
) -> dict[int, int]:
    """Find tender_lot -> app_lot mapping in any of several goszakup pages.

    The app_lot_id is the server-internal id for an application-level lot
    binding. It appears in URLs in several possible forms:

        /sign_task/{ann}/{app}/{app_lot}/{doc_id}
        /show_doc/{ann}/{app}/{doc_id}/{app_lot}
        /show_doc/{ann}/{app}/{doc_id}/{app_lot}/{n}
        data-app-lot-id="{app_lot}"

    We try each pattern. If none match in the given HTML, the caller should
    fetch additional pages (lots index, docs/709, etc.) and try again.

    Returns the mapping or {} if nothing was found (no exception).
    """
    patterns: list[str] = [
        # Original pattern (works on classic goszakup overview pages)
        r"/sign_task/[^/]+/[^/]+/(\d+)/",
        # show_doc/{ann}/{app}/{doc}/{app_lot}
        r"/show_doc/\d+/\d+/\d+/(\d+)",
        # data attributes
        r'data-app-lot-id="(\d+)"',
        r'data-application-lot-id="(\d+)"',
    ]

    # Row-aware mapping: each <tr> may reference both tender_lot_id and app_lot_id.
    rows = re.findall(r"<tr\b.*?</tr>", html_text, flags=re.DOTALL | re.IGNORECASE)
    mapping: dict[int, int] = {}
    for row in rows:
        tlot = next((t for t in tender_lot_ids if str(t) in row), None)
        if tlot is None:
            continue
        for pat in patterns:
            m = re.search(pat, row)
            if m:
                mapping[tlot] = int(m.group(1))
                break

    # Global fallback: extract all app_lot candidates, pair with tender_lots in order.
    if len(mapping) < len(tender_lot_ids):
        candidates: list[int] = []
        for pat in patterns:
            candidates.extend(int(x) for x in re.findall(pat, html_text))
        # dedupe while preserving order
        seen: set[int] = set()
        candidates = [c for c in candidates if not (c in seen or seen.add(c))]
        for tlot in tender_lot_ids:
            if tlot in mapping:
                continue
            for al in candidates:
                if al not in mapping.values():
                    mapping[tlot] = al
                    break
    return mapping  # may be incomplete; caller decides what to do


async def submit_bid(
    *,
    params: BidParams,
    ci_session: str,
    company: dict[str, str],
    state_store: StateStore,
    base_url: str = "https://v3bl.goszakup.gov.kz",
    agreement_cookie: Optional[str] = None,
    pre_t0_poll: bool = True,
    poll_interval_s: float = 0.1,
    proxy: Optional[str] = None,
    gz_client: Optional[GoszakupClient] = None,
) -> SubmissionState:
    """Run a full bid pipeline.

    ``gz_client`` is the opt #4 escape hatch: when the bot passes in a
    long-lived shared client (already authenticated, pre-warmed TLS),
    we skip creating + closing a fresh httpx client. The client's cookie
    jar is treated as authoritative — `auto_login_account_password` MUST
    be None in that case (eng review H1: shared client + in-pipeline login
    would race with concurrent /find or /status using the same jar).

    CLI path (no shared client): creates a transient client as before;
    `auto_login_account_password` works the same way it always did.
    """
    if gz_client is not None and params.auto_login_account_password is not None:
        raise ValueError(
            "shared gz_client is incompatible with auto_login_account_password; "
            "the caller must run gz.login() explicitly before submit_bid"
        )

    state = state_store.load(params.announce_id)
    state.timestamps["start"] = time.time()
    state_store.save(state)

    async with AsyncExitStack() as stack:
        if gz_client is not None:
            gz = gz_client       # borrowed — never aclosed here
        else:
            # Start with empty cookies if we're going to auto-login; otherwise
            # use the cookie that was passed in (or pulled from Keychain).
            initial_cookie = None if params.auto_login_account_password else ci_session
            gz = await stack.enter_async_context(GoszakupClient(
                ci_session=initial_cookie,
                agreement_cookie=agreement_cookie,
                base_url=base_url,
                proxy=proxy,
            ))
        # Pick crypto backend. Both expose the same sign_file / sign_xml_batch
        # interface so the rest of the pipeline doesn't care which one is in use.
        if params.backend == "local":
            if params.signer is not None:
                ns = await stack.enter_async_context(LocalSigner(
                    kalkan_signer=params.signer,
                    http_client=gz._http,
                ))
            else:
                if not params.p12_path or params.ecp_password is None:
                    raise ValueError("backend=local requires p12_path+ecp_password or signer")
                ns = await stack.enter_async_context(LocalSigner(
                    p12_path=params.p12_path,
                    password=params.ecp_password,
                    http_client=gz._http,
                ))
        else:
            ns = await stack.enter_async_context(NursignClient())

        # Run a fresh login in this same http client if requested. This avoids
        # the session-loss race where Chrome's separate login between
        # `submit.py login` and `submit.py bid` invalidates our cookie.
        if params.auto_login_account_password:
            log.info("pipeline.auto_login.start")
            await gz.login(
                account_password=params.auto_login_account_password,
                kalkan_signer=ns if params.backend == "local" else None,
            )
            log.info("pipeline.auto_login.done")

        # === PRE-T0 ===
        if pre_t0_poll:
            log.info("pipeline.poll_announce", announce=params.announce_id)
            await gz.poll_announce_ready(
                params.announce_id, interval_s=poll_interval_s
            )
        state.timestamps["t0"] = time.time()
        state_store.save(state)

        # === Step 1: create_application ===
        app_id = await gz.create_application(
            params.announce_id,
            subject_address=company["subject_address"],
            iik=company["iik"],
            contact_phone=company["contact_phone"],
        )
        if not app_id:
            raise RuntimeError("create_application did not return application_id")
        state.application_id = app_id
        state.step = "created"
        state.timestamps["created"] = time.time()
        state_store.save(state)
        log.info("pipeline.created", app_id=app_id)

        # === Step 2-3: add_lots + lots_next ===
        await gz.add_lots(params.announce_id, app_id, params.tender_lot_ids)
        await gz.lots_next(params.announce_id, app_id)
        state.step = "lots_added"
        state_store.save(state)

        # === Step 4: discover tender_lot -> app_lot mapping ===
        # Reordered after empirical data: /show_doc/709 returns the
        # complete mapping in ~0.8s and ALWAYS wins for service tenders.
        # Previously /create + /docs were tried first; both pages render
        # ~2.8s and never carry the data-app-lot-id we need — pure waste.
        # We now try /709 first (sequential), then fall through to /719,
        # /lots, /docs, /create as belt-and-braces only.
        candidates = (
            f"/ru/application/show_doc/{params.announce_id}/{app_id}/709",
            f"/ru/application/show_doc/{params.announce_id}/{app_id}/719",
            f"/ru/application/lots/{params.announce_id}/{app_id}",
            f"/ru/application/docs/{params.announce_id}/{app_id}",
            f"/ru/application/create/{params.announce_id}",
        )
        app_lot_map: dict[int, int] = {}
        from .parser import _dump  # for diagnostic dumps
        last_dump: Optional[str] = None
        html_text: str = ""

        discover_t0 = time.monotonic()
        for candidate in candidates:
            html_text = await gz._get_page(candidate)
            attempt = _extract_app_lot_mapping(html_text, params.tender_lot_ids)
            log.info(
                "pipeline.app_lot_attempt",
                page=candidate,
                got=attempt,
                complete=len(attempt) == len(params.tender_lot_ids),
            )
            if attempt:
                app_lot_map.update(attempt)
                if len(app_lot_map) == len(params.tender_lot_ids):
                    break
            last_dump = str(_dump(html_text, f"app_lot_search_{candidate.replace('/', '_')}"))
        log.info(
            "opt2.discover.done",
            elapsed_ms=int((time.monotonic() - discover_t0) * 1000),
            tried=candidates.index(candidate) + 1 if attempt else len(candidates),
        )

        if len(app_lot_map) != len(params.tender_lot_ids):
            # If every candidate page redirected to /preview/ — the draft is
            # already fully prepared from a prior run (CLI, browser, earlier
            # bot session). Surface this distinctly so the bot can show a
            # useful UX instead of a cryptic parse error.
            if 'current_method = "preview"' in (html_text or "") or (
                "/application/preview/" in (html_text or "")
                and "Подать заявку" in (html_text or "")
            ):
                log.warning("pipeline.already_prepared", app_id=app_id)
                raise AlreadyPrepared(
                    f"draft {app_id} for tender {params.announce_id} is "
                    f"already at /preview/ stage",
                    app_id=app_id,
                )
            log.error(
                "pipeline.app_lot_mapping_incomplete",
                got=app_lot_map,
                last_dump=last_dump,
            )
            raise RuntimeError(
                f"could not resolve app_lot for all tender lots "
                f"(got {app_lot_map}). HTML dumps under "
                f"~/.goszakup/logs/html_dumps/"
            )
        log.info("pipeline.app_lot_map", mapping=app_lot_map)

        # === Steps 5-9: SIGN ALL INDEPENDENT DOCUMENTS IN PARALLEL =========
        # The captured HAR did these sequentially but they're independent --
        # each is a separate (file_id, signature) tuple submitted to its own
        # endpoint. Server has no ordering constraint between them. With
        # asyncio.gather we collapse ~25s of serial signing into ~3-4s bound
        # by the slowest single document.
        #
        # GET pages, GET files, and POST signatures all run concurrently.
        # The KalkanSigner's _cmd_lock serializes Kalkan calls (50ms each),
        # which is not the bottleneck.

        async def _sign_main_doc(doc_id: int, *, sign_flag: bool) -> int:
            return await _sign_show_doc(
                gz, ns, params.announce_id, app_id, doc_id, sign_flag=sign_flag,
            )

        async def _sign_tz_for_lot(app_lot: int) -> list[int]:
            html_text = await gz.get_sign_task(
                params.announce_id, app_id, app_lot, DOC_TASK
            )
            file_ids = extract_all_file_ids(html_text, hint=f"sign_task_{app_lot}")

            async def _sign_one_tz(fid: int) -> int:
                sig = await ns.sign_file(f"{base_url}/files/download_file/{fid}/")
                await gz.post_sign_task_signature(
                    params.announce_id, app_id, app_lot, DOC_TASK,
                    file_id=fid, signature_b64=sig,
                )
                return fid

            return await asyncio.gather(*[_sign_one_tz(fid) for fid in file_ids])

        async def _bankruptcy_gen_and_sign() -> int:
            # gen must happen before fetch (server regenerates the document)
            await gz.bankruptcy_gen(params.announce_id, app_id)
            html_text = await gz.get_show_doc(
                params.announce_id, app_id, DOC_BANKRUPTCY
            )
            fid = extract_file_id(html_text, hint="bankruptcy")
            sig = await ns.sign_file(f"{base_url}/files/download_file/{fid}/")
            await gz.bankruptcy_save(
                params.announce_id, app_id, file_id=fid, signature_b64=sig,
            )
            return fid

        # Optional qualifications copy must run BEFORE bankruptcy (same doc).
        if params.qualification_source_anno:
            await gz.copy_data_docs_search(
                params.announce_id, app_id, DOC_BANKRUPTCY,
                source_anno=params.qualification_source_anno,
            )
            from_html = await gz._get_page(
                f"/ru/application/copy_data_docs/{params.announce_id}/{app_id}/{DOC_BANKRUPTCY}"
            )
            from .parser import extract_copy_data_from_lots, extract_copy_data_to_lots
            from_lots = extract_copy_data_from_lots(from_html)
            to_lots = extract_copy_data_to_lots(from_html)
            if from_lots and to_lots:
                await gz.copy_data_docs_apply(
                    params.announce_id, app_id, DOC_BANKRUPTCY,
                    source_anno=params.qualification_source_anno,
                    from_lot=from_lots[0],
                    to_lots=to_lots,
                )
                await gz.copy_data_docs_finalize(
                    params.announce_id, app_id, DOC_BANKRUPTCY,
                    source_anno=params.qualification_source_anno,
                )

        # Opt #3 (gated, safer variant per eng review S1): try to pre-fetch
        # and pre-sign the priceoffer XML in parallel with main-doc signing.
        # The priceoffer page may 302 to /docs/ if it's not ready yet, in
        # which case we fall back to the original sequential flow.
        # Gated by env GZ_PRESIGN_PRICEOFFER=1 — disabled by default.
        import os as _os
        presign_enabled = _os.environ.get("GZ_PRESIGN_PRICEOFFER", "0") == "1"

        async def _presign_priceoffer():
            """Returns (sig_by_point, offer_pairs, xml_by_point) on success, None on fallback."""
            try:
                r = await gz._http.get(
                    f"/ru/application/priceoffers/{params.announce_id}/{app_id}",
                    follow_redirects=False,
                )
                if r.status_code != 200:
                    log.info(
                        "opt3.presign.skip",
                        reason=f"status={r.status_code}",
                        location=r.headers.get("location"),
                    )
                    return None
                offer_pairs = extract_priceoffer_structure(r.text)
                if not offer_pairs:
                    log.info("opt3.presign.skip", reason="no_pairs")
                    return None
                log.info("opt3.presign.probe_ok", pairs=offer_pairs)
                tender_for_app = {v: k for k, v in app_lot_map.items()}
                xml_by_point = {
                    pt: _build_priceoffer_xml(
                        announce_id=params.announce_id,
                        tender_lot_id=tender_for_app[lot],
                        point_id=pt,
                        price=params.price,
                    )
                    for (lot, pt) in offer_pairs
                }
                sig_by_point = await ns.sign_xml_batch(xml_by_point)
                log.info("opt3.presign.signed", n=len(sig_by_point))
                return sig_by_point, offer_pairs, xml_by_point
            except Exception as e:
                log.warning("opt3.presign.failed", err=str(e)[:200])
                return None

        presign_task = (
            asyncio.create_task(_presign_priceoffer()) if presign_enabled else None
        )

        n_total = 3 + len(app_lot_map)  # app, categories, bankruptcy, + tz per lot
        log.info("pipeline.sign_group.start", n_total=n_total, n_lots=len(app_lot_map))
        sign_t0 = time.time()

        _done = [0]  # mutable counter for cross-task progress
        async def _track(coro, name: str):
            r = await coro
            _done[0] += 1
            log.info(
                "pipeline.sign_progress",
                n_done=_done[0], n_total=n_total, last=name,
            )
            return r

        results = await asyncio.gather(
            _track(_sign_main_doc(DOC_APPLICATION, sign_flag=True), "application"),
            _track(_sign_main_doc(DOC_CATEGORIES, sign_flag=False), "categories"),
            *[_track(_sign_tz_for_lot(al), f"tz_lot_{al}") for al in app_lot_map.values()],
            _track(_bankruptcy_gen_and_sign(), "bankruptcy"),
        )
        sign_elapsed_s = round(time.time() - sign_t0, 2)
        # Surface the Kalkan-parallel wall-clock so the bot can show users
        # "our work vs goszakup wait" — clarifies that the dominant chunk
        # of bid time is the server, not our code.
        state.timestamps["sign_group_elapsed_s"] = sign_elapsed_s
        log.info(
            "pipeline.sign_group.done",
            elapsed_s=sign_elapsed_s,
            results=results,
        )

        # Deposit per lot -- independent across lots, so parallel.
        async def _deposit_one(app_lot: int) -> None:
            await gz.deposit_add(params.announce_id, app_id, lot_id=app_lot)
            await gz.deposit_save(params.announce_id, app_id, lot_id=app_lot)

        await asyncio.gather(*[_deposit_one(al) for al in app_lot_map.values()])

        state.step = "docs_signed"
        state.timestamps["docs_signed"] = time.time()
        state_store.save(state)

        # === Step 11: docs_next ===
        await gz.docs_next(params.announce_id, app_id)

        # === Step 12-14: priceoffer (with opt #3 fast path when presign succeeded) ===
        presign_result = None
        if presign_task is not None:
            presign_result = await presign_task

        if presign_result is not None:
            sig_by_point, offer_pairs, xml_by_point = presign_result
            prices = {(lot, pt): params.price for (lot, pt) in offer_pairs}
            log.info("opt3.presign.applied", pairs=offer_pairs)
            # check_priceoffers is server-side validation; still required.
            await gz.check_priceoffers(params.announce_id, app_id, prices=prices)
        else:
            # Fallback: original sequential flow.
            po_html = await gz._get_page(
                f"/ru/application/priceoffers/{params.announce_id}/{app_id}"
            )
            offer_pairs = extract_priceoffer_structure(po_html)
            prices = {(lot, pt): params.price for (lot, pt) in offer_pairs}
            log.info("pipeline.priceoffer_pairs", pairs=offer_pairs)

            await gz.check_priceoffers(params.announce_id, app_id, prices=prices)

            tender_for_app = {v: k for k, v in app_lot_map.items()}
            xml_by_point: dict[int, str] = {}
            for (app_lot, pt) in offer_pairs:
                tender_lot = tender_for_app[app_lot]
                xml_by_point[pt] = _build_priceoffer_xml(
                    announce_id=params.announce_id,
                    tender_lot_id=tender_lot,
                    point_id=pt,
                    price=params.price,
                )

            sig_by_point = await ns.sign_xml_batch(xml_by_point)

        # === Step 15: add_priceoffers ===
        form_data_parts = [
            f"offer[{lot}][{pt}][price]={params.price}"
            for (lot, pt) in offer_pairs
        ]
        form_data_parts.append("is_construction_pilot=")
        form_data = "&".join(form_data_parts)

        await gz.add_priceoffers(
            params.announce_id, app_id,
            xml_by_point=xml_by_point,
            sig_by_point=sig_by_point,
            form_data=form_data,
        )

        # === Step 16: priceoffers_next ===
        await gz.priceoffers_next(params.announce_id, app_id, prices=prices)
        state.step = "signed"
        state.timestamps["signed"] = time.time()
        state_store.save(state)

        # === Step 17: FINAL submit, gated by dry_run ===
        if params.dry_run:
            log.warning(
                "DRY_RUN: skipping ajax_public_application",
                app_id=app_id,
                state_dir=str(state_store.state_dir),
            )
            state.step = "dry_run_complete"
            state_store.save(state)
            return state

        result = await gz.public_application(params.announce_id, app_id)
        log.info("pipeline.public_application.result", result=result)
        if result.get("status") == "ok":
            state.step = "public_ok"
        else:
            state.step = "public_error"
            state.error = result.get("message") or str(result)[:500]
        state.timestamps["public"] = time.time()
        state_store.save(state)

    return state


async def finalize_application(
    *,
    announce_id: str,
    application_id: str,
    ci_session: str,
    state_store: StateStore,
    base_url: str = "https://v3bl.goszakup.gov.kz",
    agreement_cookie: Optional[str] = None,
    proxy: Optional[str] = None,
    gz_client: Optional[GoszakupClient] = None,
) -> SubmissionState:
    """Finalize a draft that's already at step=='signed' (dry-run produced it).

    Single POST to ajax_public_application; no re-signing, no re-creating.
    Use when you've done a dry-run, the user confirmed, and you want to
    finalize. Per AGENTS.md:138-145, the server treats subsequent
    create/lots/docs calls on an existing draft as a no-op redirect to
    /preview/, so retrying the whole pipeline is wasted work and risks
    state divergence.
    """
    state = state_store.load(announce_id)
    if state.application_id != application_id:
        raise RuntimeError(
            f"finalize: state has app_id={state.application_id}, "
            f"caller asked for {application_id}"
        )
    if state.step not in ("signed", "dry_run_complete"):
        raise RuntimeError(
            f"finalize: state.step is {state.step!r}; expected 'signed' or 'dry_run_complete'"
        )

    async with AsyncExitStack() as stack:
        if gz_client is not None:
            gz = gz_client       # borrowed
        else:
            gz = await stack.enter_async_context(GoszakupClient(
                ci_session=ci_session,
                agreement_cookie=agreement_cookie,
                base_url=base_url,
                proxy=proxy,
            ))
        log.info("pipeline.finalize.start", app_id=application_id)
        result = await gz.public_application(announce_id, application_id)
        log.info("pipeline.public_application.result", result=result)
        if result.get("status") == "ok":
            state.step = "public_ok"
        else:
            state.step = "public_error"
            state.error = result.get("message") or str(result)[:500]
        state.timestamps["public"] = time.time()
        state_store.save(state)
    return state


async def _sign_show_doc(
    gz: GoszakupClient,
    ns: NursignClient,
    announce_id: str,
    app_id: str,
    doc_id: int,
    *,
    sign_flag: bool,
) -> int:
    """GET show_doc page, extract file_id, sign via NURSign, POST signature."""
    html_text = await gz.get_show_doc(announce_id, app_id, doc_id)
    file_id = extract_file_id(html_text, hint=f"doc_{doc_id}")
    sig = await ns.sign_file(f"{gz.base_url}/files/download_file/{file_id}/")
    await gz.post_show_doc_signature(
        announce_id, app_id, doc_id,
        file_id=file_id, signature_b64=sig, sign_flag=sign_flag,
    )
    return file_id
