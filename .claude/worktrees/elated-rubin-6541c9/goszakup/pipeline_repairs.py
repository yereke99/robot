"""Submission orchestrator — КОНКУРС (метод 2, работы/услуги).

Адаптация pipeline.py под тип тендера «Конкурс» (method=2).
Отличия от оригинального pipeline.py (питание, method=120):

    ОРИГИНАЛ (питание)          ЭТОТ ФАЙЛ (конкурс)
    ─────────────────────────   ─────────────────────────────────
    doc 741  Заявка на участие  doc 1253  Приложение 1 (Перечень лотов)
    doc 705  Перечень категорий doc 3327  Приложение 2 (Соглашение)
    doc 709  ТЗ (per-lot)       doc 3334  Приложение 15 (ТЗ per-lot)
    doc 3240 Банкротство  ✅    doc 3240  Банкротство  ✅ (без изменений)
                                doc 3359  Приложение 4 (Бенефициарное владение)
                                doc 3335  Приложение 11 (Квалификация)
                                doc 3328  Приложение 19 (Обеспечение заявки)

СТАТУС ДОКУМЕНТОВ (нужна проверка через HAR):
    doc 1253  — предположительно авто-генерируется, подписывается через show_doc
    doc 3327  — предположительно авто-генерируется, подписывается через show_doc
    doc 3334  — предположительно per-lot ТЗ через sign_task (как 709)
               ⚠ НУЖНА ПРОВЕРКА: sign_task или show_doc?
    doc 3359  — предположительно авто-генерируется
               ⚠ НУЖНА ПРОВЕРКА: авто или требует генерации?
    doc 3335  — предположительно авто-генерируется (квалификация)
               ⚠ НУЖНА ПРОВЕРКА: авто или требует генерации?
    doc 3328  — возможно требует deposit flow (как 719 в оригинале)
               ⚠ НУЖНА ПРОВЕРКА: deposit endpoint или show_doc?

Как проверить: захвати HAR в браузере при ручной подаче заявки
на тендер метода 2, посмотри POST запросы для каждого документа.

Order of operations:
    1. POST ajax_create_application
    2. POST ajax_add_lots
    3. POST ajax_lots_next
    4. GET  discover app_lot_id mapping (через doc 3334)
    5. Sign doc 1253  (Приложение 1 — Перечень лотов)
    6. Sign doc 3327  (Приложение 2 — Соглашение)
    7. Sign doc 3359  (Приложение 4 — Бенефициарное владение)
    8. Sign doc 3335  (Приложение 11 — Квалификация)
    9. Sign doc 3334  (Приложение 15 — ТЗ per-lot)
   10. Sign doc 3240  (Декларация о банкротстве, генерация)
   11. Deposit doc 3328 (Приложение 19 — Обеспечение заявки)
   12. POST ajax_docs_next
   13-17. Ценовое предложение → подать
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

log = structlog.get_logger("pipeline_repairs")

# ─── Документы конкурса (метод 2) ────────────────────────────────────────────
DOC_LOT_LIST    = 1253  # Приложение 1  — Перечень лотов и условия поставки
DOC_AGREEMENT   = 3327  # Приложение 2  — Соглашение об участии в конкурсе
DOC_BENEFICIARY = 3359  # Приложение 4  — Информация о бенефициарном владении
DOC_QUAL        = 3335  # Приложение 11 — Сведения о квалификации
DOC_TASK        = 3334  # Приложение 15 — Техническая спецификация (ТЗ per-lot)
DOC_BANKRUPTCY  = 3240  # Декларация о банкротстве (без изменений — работает)
DOC_SECURITY    = 3328  # Приложение 19 — Обеспечение заявки
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class BidParams:
    announce_id: str
    tender_lot_ids: list[int]
    price: int
    qualification_source_anno: Optional[str] = None
    dry_run: bool = False
    backend: Literal["local", "ncalayer"] = "local"
    p12_path: Optional[str] = None
    ecp_password: Optional[str] = None
    signer: Optional[KalkanSigner] = None
    auto_login_account_password: Optional[str] = None


def _build_priceoffer_xml(
    *, announce_id: str, tender_lot_id: int, point_id: int, price: int
) -> str:
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
    """Без изменений — универсальная функция поиска app_lot_id."""
    patterns: list[str] = [
        r"/sign_task/[^/]+/[^/]+/(\d+)/",
        r"/show_doc/\d+/\d+/\d+/(\d+)",
        r'data-app-lot-id="(\d+)"',
        r'data-application-lot-id="(\d+)"',
    ]
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
    if len(mapping) < len(tender_lot_ids):
        candidates: list[int] = []
        for pat in patterns:
            candidates.extend(int(x) for x in re.findall(pat, html_text))
        seen: set[int] = set()
        candidates = [c for c in candidates if not (c in seen or seen.add(c))]
        for tlot in tender_lot_ids:
            if tlot in mapping:
                continue
            for al in candidates:
                if al not in mapping.values():
                    mapping[tlot] = al
                    break
    return mapping


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
    if gz_client is not None and params.auto_login_account_password is not None:
        raise ValueError(
            "shared gz_client несовместим с auto_login_account_password"
        )

    state = state_store.load(params.announce_id)
    state.timestamps["start"] = time.time()
    state_store.save(state)

    async with AsyncExitStack() as stack:
        if gz_client is not None:
            gz = gz_client
        else:
            initial_cookie = None if params.auto_login_account_password else ci_session
            gz = await stack.enter_async_context(GoszakupClient(
                ci_session=initial_cookie,
                agreement_cookie=agreement_cookie,
                base_url=base_url,
                proxy=proxy,
            ))

        if params.backend == "local":
            if params.signer is not None:
                ns = await stack.enter_async_context(LocalSigner(
                    kalkan_signer=params.signer,
                    http_client=gz._http,
                ))
            else:
                if not params.p12_path or params.ecp_password is None:
                    raise ValueError("backend=local требует p12_path+ecp_password или signer")
                ns = await stack.enter_async_context(LocalSigner(
                    p12_path=params.p12_path,
                    password=params.ecp_password,
                    http_client=gz._http,
                ))
        else:
            ns = await stack.enter_async_context(NursignClient())

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
        log.info(
            "pipeline.create_application.params",
            subject_address=company.get("subject_address"),
            iik=company.get("iik"),
            tax_payer_type=company.get("tax_payer_type", "UL"),
        )
        app_id = await gz.create_application(
            params.announce_id,
            subject_address=company["subject_address"],
            iik=company["iik"],
            contact_phone=company.get("contact_phone", ""),
            tax_payer_type=company.get("tax_payer_type", "UL"),
        )
        if not app_id:
            raise RuntimeError("create_application не вернул application_id")
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
        # Для конкурса: пробуем doc 3334 (ТЗ) первым вместо 709
        candidates = (
            f"/ru/application/show_doc/{params.announce_id}/{app_id}/{DOC_TASK}",   # 3334
            f"/ru/application/show_doc/{params.announce_id}/{app_id}/{DOC_LOT_LIST}",  # 1253
            f"/ru/application/lots/{params.announce_id}/{app_id}",
            f"/ru/application/docs/{params.announce_id}/{app_id}",
            f"/ru/application/create/{params.announce_id}",
        )
        app_lot_map: dict[int, int] = {}
        from .parser import _dump
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
            if 'current_method = "preview"' in (html_text or "") or (
                "/application/preview/" in (html_text or "")
                and "Подать заявку" in (html_text or "")
            ):
                log.warning("pipeline.already_prepared", app_id=app_id)
                raise AlreadyPrepared(
                    f"draft {app_id} for tender {params.announce_id} is already at /preview/",
                    app_id=app_id,
                )
            log.error("pipeline.app_lot_mapping_incomplete", got=app_lot_map, last_dump=last_dump)
            raise RuntimeError(
                f"could not resolve app_lot (got {app_lot_map}). "
                f"HTML dumps under ~/.goszakup/logs/html_dumps/"
            )
        log.info("pipeline.app_lot_map", mapping=app_lot_map)

        # ─── Вспомогательные функции подписания ──────────────────────────────

        async def _sign_main_doc(doc_id: int, *, sign_flag: bool = True) -> int:
            """Авто-генерируемый документ: GET → file_id → подписать → POST."""
            return await _sign_show_doc(
                gz, ns, params.announce_id, app_id, doc_id, sign_flag=sign_flag,
            )

        async def _sign_tz_for_lot(app_lot: int) -> list[int]:
            """ТЗ per-lot (doc 3334).
            ⚠ НУЖНА ПРОВЕРКА: возможно sign_task endpoint,
              возможно show_doc/{ann}/{app}/3334/{app_lot}.
            Сейчас пробуем sign_task (как в оригинале с doc 709).
            Если не работает — переключить на show_doc ниже.
            """
            html_text = await gz.get_sign_task(
                params.announce_id, app_id, app_lot, DOC_TASK  # 3334
            )
            file_ids = extract_all_file_ids(html_text, hint=f"sign_task_{app_lot}_3334", quiet=True)

            if not file_ids:
                # Fallback: попробовать через show_doc
                log.warning(
                    "pipeline_repairs.tz_sign_task_empty",
                    app_lot=app_lot,
                    hint="falling back to show_doc for doc 3334",
                )
                html_text2 = await gz.get_show_doc(params.announce_id, app_id, DOC_TASK)
                file_ids = extract_all_file_ids(html_text2, hint=f"show_doc_3334", quiet=True)

            if not file_ids:
                log.warning("pipeline_repairs.tz_no_files", app_lot=app_lot)
                return []

            async def _sign_one(fid: int) -> int:
                sig = await ns.sign_file(f"{base_url}/files/download_file/{fid}/")
                await gz.post_sign_task_signature(
                    params.announce_id, app_id, app_lot, DOC_TASK,
                    file_id=fid, signature_b64=sig,
                )
                return fid

            return await asyncio.gather(*[_sign_one(fid) for fid in file_ids])

        async def _bankruptcy_gen_and_sign() -> int:
            """Декларация о банкротстве — без изменений, уже работает."""
            await gz.bankruptcy_gen(params.announce_id, app_id)
            html_text = await gz.get_show_doc(params.announce_id, app_id, DOC_BANKRUPTCY)
            fid = extract_file_id(html_text, hint="bankruptcy")
            sig = await ns.sign_file(f"{base_url}/files/download_file/{fid}/")
            await gz.bankruptcy_save(
                params.announce_id, app_id, file_id=fid, signature_b64=sig,
            )
            return fid

        # ─────────────────────────────────────────────────────────────────────

        # Опциональное копирование квалификации (до банкротства)
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

        # === Steps 5-10: Подписание всех документов параллельно ===
        # Документы конкурса:
        #   1253 — Приложение 1  (авто, show_doc)
        #   3327 — Приложение 2  (авто, show_doc)
        #   3359 — Приложение 4  (авто?, show_doc) ⚠ проверить
        #   3335 — Приложение 11 (авто?, show_doc) ⚠ проверить
        #   3334 — Приложение 15 (per-lot ТЗ, sign_task/show_doc) ⚠ проверить
        #   3240 — Банкротство   (генерация + sign) ✅ работает
        #
        # ⚠ Документы 3328 (Обеспечение заявки) — возможно deposit flow,
        #   пока НЕ включён. Добавить после проверки HAR.

        n_total = 4 + len(app_lot_map) + 1  # 1253,3327,3359,3335 + ТЗ per-lot + банкротство
        log.info("pipeline.sign_group.start", n_total=n_total, n_lots=len(app_lot_map))
        sign_t0 = time.time()

        _done = [0]
        async def _track(coro, name: str):
            r = await coro
            _done[0] += 1
            log.info("pipeline.sign_progress", n_done=_done[0], n_total=n_total, last=name)
            return r

        results = await asyncio.gather(
            _track(_sign_main_doc(DOC_LOT_LIST,    sign_flag=True),  "lot_list_1253"),
            _track(_sign_main_doc(DOC_AGREEMENT,   sign_flag=True),  "agreement_3327"),
            _track(_sign_main_doc(DOC_BENEFICIARY, sign_flag=True),  "beneficiary_3359"),
            _track(_sign_main_doc(DOC_QUAL,        sign_flag=True),  "qual_3335"),
            *[_track(_sign_tz_for_lot(al), f"tz_lot_{al}_3334") for al in app_lot_map.values()],
            _track(_bankruptcy_gen_and_sign(), "bankruptcy_3240"),
        )
        sign_elapsed_s = round(time.time() - sign_t0, 2)
        state.timestamps["sign_group_elapsed_s"] = sign_elapsed_s
        log.info("pipeline.sign_group.done", elapsed_s=sign_elapsed_s, results=results)

        # Deposit per lot (doc 3328 — Обеспечение заявки)
        # ⚠ НУЖНА ПРОВЕРКА: возможно это обычный show_doc, не deposit endpoint.
        # Пока используем тот же deposit flow что в оригинале (deposit_add + deposit_save).
        # Если не работает — заменить на _sign_main_doc(DOC_SECURITY).
        async def _deposit_one(app_lot: int) -> None:
            await gz.deposit_add(params.announce_id, app_id, lot_id=app_lot)
            await gz.deposit_save(params.announce_id, app_id, lot_id=app_lot)

        await asyncio.gather(*[_deposit_one(al) for al in app_lot_map.values()])

        state.step = "docs_signed"
        state.timestamps["docs_signed"] = time.time()
        state_store.save(state)

        # === Step 11: docs_next ===
        await gz.docs_next(params.announce_id, app_id)

        # === Steps 12-16: Ценовое предложение ===
        import os as _os
        presign_enabled = _os.environ.get("GZ_PRESIGN_PRICEOFFER", "0") == "1"

        async def _presign_priceoffer():
            try:
                r = await gz._http.get(
                    f"/ru/application/priceoffers/{params.announce_id}/{app_id}",
                    follow_redirects=False,
                )
                if r.status_code != 200:
                    return None
                offer_pairs = extract_priceoffer_structure(r.text)
                if not offer_pairs:
                    return None
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
                return sig_by_point, offer_pairs, xml_by_point
            except Exception as e:
                log.warning("opt3.presign.failed", err=str(e)[:200])
                return None

        presign_task = asyncio.create_task(_presign_priceoffer()) if presign_enabled else None

        presign_result = None
        if presign_task is not None:
            presign_result = await presign_task

        if presign_result is not None:
            sig_by_point, offer_pairs, xml_by_point = presign_result
            prices = {(lot, pt): params.price for (lot, pt) in offer_pairs}
            await gz.check_priceoffers(params.announce_id, app_id, prices=prices)
        else:
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

        await gz.priceoffers_next(params.announce_id, app_id, prices=prices)
        state.step = "signed"
        state.timestamps["signed"] = time.time()
        state_store.save(state)

        # === Step 17: Финальная подача (gated by dry_run) ===
        if params.dry_run:
            log.warning("DRY_RUN: skipping ajax_public_application", app_id=app_id)
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
    """Финализировать черновик на этапе dry_run_complete."""
    state = state_store.load(announce_id)
    if state.application_id != application_id:
        raise RuntimeError(f"finalize: state app_id={state.application_id}, запрошен {application_id}")
    if state.step not in ("signed", "dry_run_complete"):
        raise RuntimeError(f"finalize: state.step={state.step!r}; ожидается 'signed' или 'dry_run_complete'")

    async with AsyncExitStack() as stack:
        if gz_client is not None:
            gz = gz_client
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
    ns,
    announce_id: str,
    app_id: str,
    doc_id: int,
    *,
    sign_flag: bool,
) -> int:
    """GET show_doc → file_id → подписать → POST подпись."""
    html_text = await gz.get_show_doc(announce_id, app_id, doc_id)
    file_id = extract_file_id(html_text, hint=f"doc_{doc_id}")
    sig = await ns.sign_file(f"{gz.base_url}/files/download_file/{file_id}/")
    await gz.post_show_doc_signature(
        announce_id, app_id, doc_id,
        file_id=file_id, signature_b64=sig, sign_flag=sign_flag,
    )
    return file_id
