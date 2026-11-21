"""/schedule — Запланированные ставки с известным временем открытия.

Формат команды:
    /schedule 17058849 09:00
    /schedule 17058849 2026-06-01 09:00
    /schedule 17058849 09:00 17058850 09:00     ← несколько тендеров

Как работает (fast-T0, две фазы):
    1. За WARMUP_LEAD_S до T0 — login, pool x6, TumarCSP
    2. Сразу после прогрева — poll открытия; как только тендер открыт —
       PHASE-1: create+lots+docs → черновик на /priceoffers/ (stop_after_docs)
    3. В wall-clock T0 (или сразу, если позже) — PHASE-2: только gamma+submit
       по resume_app_id; если документы ещё не готовы, сначала завершаем их
    4. Несколько тендеров — параллельно
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Asia/Almaty")
except ImportError:
    import pytz as _pytz
    _TZ = _pytz.timezone("Asia/Almaty")


def _now() -> datetime:
    """Текущее время в часовом поясе Asia/Almaty."""
    return datetime.now(_TZ).replace(tzinfo=None)
from typing import Optional

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import CommandHandler, ContextTypes

from ..auth import whitelist_only
from ..context import BotContext
from . import settings as settings_handler
from .bid import deliver_timing_report
from ... import flow as flow_repairs
from ...client import GoszakupClient
from ...flow import AtagosBidParams, BeneficiaryInfo
from ...local_signer import LocalSigner
from ...state import StateStore
from ...timing import BidTrace, current_trace, poll_interval_s

log = structlog.get_logger("scheduled")

# Сколько секунд до T0 начинаем прогрев (login, pool, TumarCSP)
WARMUP_LEAD_S = 300   # 5 минут
# Шаг уведомления "ждём T0" в секундах
NOTIFY_INTERVAL_S = 60
# После прогрева сразу начинаем poll+prepare (не ждём wall-clock T0).
# Подготовка возможна только после открытия тендера на портале.


@dataclass
class ScheduledTender:
    announce_id: str
    t0: datetime          # точное UTC+6 время открытия
    lot_ids: list[int]    # заполняется при discover (пусто = авто)


# ─────────────────────────────────────────────────────────────────────────────
# Парсинг команды
# ─────────────────────────────────────────────────────────────────────────────

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_ANN_RE  = re.compile(r"^\d{6,12}$")


def _parse_schedule_args(raw: str) -> list[ScheduledTender] | str:
    """Вернуть список ScheduledTender или строку-ошибку."""
    tokens = raw.split()
    results: list[ScheduledTender] = []
    i = 0
    now = _now()

    while i < len(tokens):
        tok = tokens[i]

        # Announce ID
        if not _ANN_RE.match(tok):
            return f"Не распознан announce_id: <code>{tok}</code>"
        announce_id = tok
        i += 1

        # Дата (опционально)
        date_part: Optional[datetime] = None
        if i < len(tokens) and _DATE_RE.match(tokens[i]):
            m = _DATE_RE.match(tokens[i])
            date_part = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            i += 1

        # Время
        if i >= len(tokens) or not _TIME_RE.match(tokens[i]):
            return (f"После тендера <code>{announce_id}</code> ожидается время "
                    f"в формате <code>HH:MM</code>")
        m = _TIME_RE.match(tokens[i])
        hour, minute = int(m.group(1)), int(m.group(2))
        i += 1

        # Собираем datetime
        if date_part:
            t0 = date_part.replace(hour=hour, minute=minute, second=0, microsecond=0)
        else:
            t0 = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if t0 <= now:
                t0 += timedelta(days=1)

        if t0 <= now:
            # Если только что прошло (< 2 мин) — всё равно запускаем немедленно
            if (now - t0).total_seconds() < 120:
                pass  # запустим, прогрев начнётся сразу
            else:
                return f"Время {t0.strftime('%d.%m %H:%M')} уже в прошлом (>{int((now-t0).total_seconds()//60)} мин назад)"

        results.append(ScheduledTender(announce_id=announce_id, t0=t0, lot_ids=[]))

    if not results:
        return "Укажи хотя бы один тендер: <code>/schedule 17058849 09:00</code>"

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Прогрев одного тендера
# ─────────────────────────────────────────────────────────────────────────────

async def _warmup_one(ctx: BotContext, tender: ScheduledTender,
                      stack: AsyncExitStack) -> dict:
    """Прогреть ресурсы для одного тендера. Возвращает словарь с готовыми
    объектами: gz_client, ns (LocalSigner), doc_pool, tumar, beneficiary."""
    defaults = ctx.config.raw.get("defaults", {})
    raw_tumarcsp = ctx.config.raw.get("tumarcsp", {})
    raw_ben = ctx.config.raw.get("beneficiary", {})

    # Создаём отдельный HTTP клиент для этого тендера
    gz = await stack.enter_async_context(GoszakupClient(
        ci_session=ctx.ci_session(),
        agreement_cookie=ctx.agreement_cookie(),
        base_url=ctx.config.base_url,
        proxy=ctx.config.proxy_url,
    ))

    # Логинимся через ЭЦП если есть пароль кабинета, иначе переиспользуем куку
    try:
        if ctx.account_password and ctx.signer:
            ci = await gz.login(
                account_password=ctx.account_password,
                kalkan_signer=ctx.signer,
            )
            ctx.set_ci_session(ci)
            log.info("scheduled.warmup.login", announce=tender.announce_id)
        else:
            log.info("scheduled.warmup.reusing_cookie", announce=tender.announce_id)
    except Exception as e:
        log.warning("scheduled.warmup.login_failed", announce=tender.announce_id,
                    err=str(e)[:100])

    # Прогрев doc_pool — 6 параллельных сессий.
    # Сайт допускает только ОДНУ активную сессию на аккаунт: каждый новый
    # логин убивает предыдущую сессию (в браузере тоже).  Поэтому клонируем
    # ci_session от уже залогиненного gz — без повторного логина.
    ns = LocalSigner(kalkan_signer=ctx.signer, http_client=gz._http)
    await stack.enter_async_context(ns)

    signer = ctx.signer

    def _spawn_session_no_login():
        # Same session, own connection — no separate login, no session kill.
        sib = flow_repairs._clone_session(gz)
        sib_ns = LocalSigner(kalkan_signer=signer, http_client=sib._http)
        return sib, sib_ns

    doc_plan = ctx.config.raw.get("defaults", {}).get("doc_plan") or []
    n_sessions = max(6, len(doc_plan))
    t0_pool = time.monotonic()
    log.info("scheduled.warmup.pool.start", announce=tender.announce_id,
             n=n_sessions)
    doc_pool = [_spawn_session_no_login() for _ in range(n_sessions)]
    for sib, _ in doc_pool:
        await stack.enter_async_context(sib)
    log.info("scheduled.warmup.pool.done", announce=tender.announce_id,
             elapsed_s=round(time.monotonic() - t0_pool, 2))

    # Прогрев TumarCSP
    from ...flow import _open_tumar, AtagosBidParams, DEFAULT_DOC_PLAN
    flow_params = AtagosBidParams(
        announce_id=tender.announce_id,
        tender_lot_ids=tender.lot_ids or [0],
        price=None,
        signer=ctx.signer,
        tumar_api_key=raw_tumarcsp.get("api_key"),
        tumar_ws_url=raw_tumarcsp.get("ws_url", "wss://127.0.0.1:6127/tumarcsp/"),
        autofill_price_popup=raw_tumarcsp.get("autofill_popup", False),
        popup_coords=raw_tumarcsp.get("popup"),
        auto_login_account_password=ctx.account_password,
        doc_save_mode=defaults.get("doc_save_mode", "serial"),
    )
    tumar = None
    try:
        # _open_tumar инициализирует autofill (pywinauto) если платформа поддерживает.
        # Прямое TumarCSP(...) без autofill — попап не заполняется автоматически.
        tumar = await _open_tumar(flow_params, stack)
        log.info("scheduled.warmup.tumar.ready", announce=tender.announce_id,
                 version=getattr(tumar, "version", "?"))
    except Exception as e:
        log.warning("scheduled.warmup.tumar_failed", announce=tender.announce_id,
                    err=str(e)[:100])

    beneficiary = BeneficiaryInfo(
        name=raw_ben.get("name", ""),
        citizenship=str(raw_ben.get("citizenship", "398")),
        res_country=str(raw_ben.get("res_country", "398")),
        doc_number=str(raw_ben.get("doc_number", "")),
        doc_date=str(raw_ben.get("doc_date", "")),
        options=raw_ben.get("options", {}),
        id_pdf_path=str(raw_ben.get("id_pdf_path", "")),
        id_file_id=raw_ben.get("id_file_id") or None,
    )

    return {
        "gz": gz, "ns": ns, "doc_pool": doc_pool,
        "tumar": tumar, "beneficiary": beneficiary,
        "flow_params": flow_params,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Подача одного тендера в T0
# ─────────────────────────────────────────────────────────────────────────────

# Максимальное время ожидания открытия тендера (poll). Warmup начинается за
# 5 мин до T0 — даём запас если открытие чуть позже расписания.
POST_T0_WAIT_S = 300  # 5 минут


def _resolve_lot_ids(tender: ScheduledTender, warmed: dict) -> list[int]:
    return list(tender.lot_ids or [])


async def _ensure_lot_ids(tender: ScheduledTender, warmed: dict) -> list[int] | str:
    lot_ids = _resolve_lot_ids(tender, warmed)
    if lot_ids:
        return lot_ids
    try:
        lots = await warmed["gz"].discover_announce_lots(tender.announce_id)
        if lots:
            lot_ids = [int(lots[0]["lot_id"])]
            tender.lot_ids = lot_ids
            log.info("scheduled.lots_discovered",
                     announce=tender.announce_id, lot_ids=lot_ids)
            return lot_ids
        return f"лоты не найдены для #{tender.announce_id}"
    except Exception as e:
        return f"ошибка получения лотов: {e}"


def _bid_settings(ctx: BotContext, chat_id: int, bot, *, for_prepare: bool):
    defaults = ctx.config.raw.get("defaults", {})
    settings = ctx.bid_settings or {}
    floor_ratio = float(settings.get("dumping_floor_ratio",
                                     defaults.get("dumping_floor_ratio", 0.80)))
    guarantee_type = int(settings.get("guarantee_type", 3))
    dry_run = bool(settings.get("dry_run", False))
    # На prepare подтверждение не нужно; на fire — как в /config.
    confirm_before_submit = (
        False if for_prepare
        else bool(settings.get("confirm_before_submit", False))
    )
    on_before_submit = (
        settings_handler.make_confirm_gate(ctx, bot, chat_id)
        if confirm_before_submit else None
    )
    return {
        "defaults": defaults,
        "floor_ratio": floor_ratio,
        "guarantee_type": guarantee_type,
        "dry_run": dry_run,
        "on_before_submit": on_before_submit,
        "fast_t0": bool(defaults.get("fast_t0", True)),
    }


async def _prepare_one(ctx: BotContext, tender: ScheduledTender,
                       warmed: dict, chat_id: int, bot,
                       *, max_poll_wait_s: float,
                       notify: Callable[[str], None]) -> tuple[bool, str]:
    """PHASE-1: довести черновик до /priceoffers/ (без цены и без submit)."""
    cfg = _bid_settings(ctx, chat_id, bot, for_prepare=True)
    raw_tumarcsp = ctx.config.raw.get("tumarcsp", {})
    raw_ben = ctx.config.raw.get("beneficiary", {})

    lot_ids = await _ensure_lot_ids(tender, warmed)
    if isinstance(lot_ids, str):
        return False, lot_ids

    state_store = StateStore(state_dir=ctx.config.state_dir, namespace="scheduled")

    async def _on_open():
        notify(
            f"🟢 <b>#{tender.announce_id}</b> — открыт! "
            "Готовлю документы (фаза 1)…"
        )

    try:
        state = await flow_repairs.submit_bid(
            params=AtagosBidParams(
                announce_id=tender.announce_id,
                tender_lot_ids=lot_ids,
                price=None,
                dumping_floor_ratio=cfg["floor_ratio"],
                dry_run=False,
                stop_after_docs=True,
                guarantee_type=cfg["guarantee_type"],
                signer=ctx.signer,
                tumar=warmed.get("tumar"),
                tumar_api_key=raw_tumarcsp.get("api_key"),
                tumar_ws_url=raw_tumarcsp.get("ws_url", "wss://127.0.0.1:6127/tumarcsp/"),
                autofill_price_popup=raw_tumarcsp.get("autofill_popup", False),
                popup_coords=raw_tumarcsp.get("popup"),
                auto_login_account_password=ctx.account_password,
                skip_initial_login=True,
                doc_save_mode=cfg["defaults"].get("doc_save_mode", "serial"),
                **flow_repairs.speed_options(cfg["defaults"]),
                max_poll_wait_s=max_poll_wait_s,
            ),
            ci_session=ctx.ci_session(),
            company=ctx.config.raw["company"],
            beneficiary=warmed.get("beneficiary") or BeneficiaryInfo(
                name=raw_ben.get("name", ""),
                citizenship=str(raw_ben.get("citizenship", "398")),
                res_country=str(raw_ben.get("res_country", "398")),
                doc_number=str(raw_ben.get("doc_number", "")),
                doc_date=str(raw_ben.get("doc_date", "")),
                options=raw_ben.get("options", {}),
                id_pdf_path=str(raw_ben.get("id_pdf_path", "")),
                id_file_id=raw_ben.get("id_file_id") or None,
            ),
            state_store=state_store,
            base_url=ctx.config.base_url,
            agreement_cookie=ctx.agreement_cookie(),
            pre_t0_poll=True,
            poll_interval_s=poll_interval_s(ctx.config.raw),
            proxy=ctx.config.proxy_url,
            gz_client=warmed["gz"],
            lp_id_cache=ctx.lp_id_cache,
            reuse_doc_pool=warmed["doc_pool"],
            tz_sig_cache=ctx.tz_sig_cache,
            on_open=_on_open,
        )
        if state.step == "docs_ready" and state.application_id:
            warmed["app_id"] = state.application_id
            warmed["lot_ids"] = lot_ids
            return True, state.application_id
        return False, state.error or state.step
    except Exception as e:
        log.exception("scheduled.prepare.failed", announce=tender.announce_id)
        return False, str(e)[:300]


async def _fire_one(ctx: BotContext, tender: ScheduledTender,
                    warmed: dict, chat_id: int, bot,
                    *, resume_app_id: str | None = None,
                    pre_t0_poll: bool = False) -> tuple[str, str]:
    """PHASE-2: gamma + submit. Если resume_app_id задан — без create/docs."""
    cfg = _bid_settings(ctx, chat_id, bot, for_prepare=False)
    raw_tumarcsp = ctx.config.raw.get("tumarcsp", {})
    raw_ben = ctx.config.raw.get("beneficiary", {})

    lot_ids = warmed.get("lot_ids") or await _ensure_lot_ids(tender, warmed)
    if isinstance(lot_ids, str):
        return "error", lot_ids

    state_store = StateStore(state_dir=ctx.config.state_dir, namespace="scheduled")
    try:
        state = await flow_repairs.submit_bid(
            params=AtagosBidParams(
                announce_id=tender.announce_id,
                tender_lot_ids=lot_ids,
                planned_price_ratio=flow_repairs.REPAIRS_PLANNED_PRICE_RATIO,
                dumping_floor_ratio=cfg["floor_ratio"],
                dry_run=cfg["dry_run"],
                guarantee_type=cfg["guarantee_type"],
                on_before_submit=cfg["on_before_submit"],
                resume_app_id=resume_app_id,
                signer=ctx.signer,
                tumar=warmed.get("tumar"),
                tumar_api_key=raw_tumarcsp.get("api_key"),
                tumar_ws_url=raw_tumarcsp.get("ws_url", "wss://127.0.0.1:6127/tumarcsp/"),
                autofill_price_popup=raw_tumarcsp.get("autofill_popup", False),
                popup_coords=raw_tumarcsp.get("popup"),
                auto_login_account_password=ctx.account_password,
                skip_initial_login=True,
                doc_save_mode=cfg["defaults"].get("doc_save_mode", "serial"),
                **flow_repairs.speed_options(cfg["defaults"]),
                max_poll_wait_s=float(POST_T0_WAIT_S),
            ),
            ci_session=ctx.ci_session(),
            company=ctx.config.raw["company"],
            beneficiary=warmed.get("beneficiary") or BeneficiaryInfo(
                name=raw_ben.get("name", ""),
                citizenship=str(raw_ben.get("citizenship", "398")),
                res_country=str(raw_ben.get("res_country", "398")),
                doc_number=str(raw_ben.get("doc_number", "")),
                doc_date=str(raw_ben.get("doc_date", "")),
                options=raw_ben.get("options", {}),
                id_pdf_path=str(raw_ben.get("id_pdf_path", "")),
                id_file_id=raw_ben.get("id_file_id") or None,
            ),
            state_store=state_store,
            base_url=ctx.config.base_url,
            agreement_cookie=ctx.agreement_cookie(),
            pre_t0_poll=pre_t0_poll,
            poll_interval_s=poll_interval_s(ctx.config.raw),
            proxy=ctx.config.proxy_url,
            gz_client=warmed["gz"],
            lp_id_cache=ctx.lp_id_cache,
            # Never prewarm doc pool on resume — phase-2 is gamma+public only.
            reuse_doc_pool=([] if resume_app_id else warmed["doc_pool"]),
            tz_sig_cache=ctx.tz_sig_cache,
        )
        if state.step == "submit_declined":
            return state.step, "подача отменена вами (черновик готов, можно подать вручную)"
        if state.step in ("public_ok", "dry_run_complete"):
            return state.step, str(state.application_id or "")
        return state.step, state.error or state.step
    except Exception as e:
        log.exception("scheduled.fire.failed", announce=tender.announce_id)
        return "error", str(e)[:300]


# ─────────────────────────────────────────────────────────────────────────────
# Главная корутина планировщика
# ─────────────────────────────────────────────────────────────────────────────

async def _run_scheduled(ctx: BotContext, tenders: list[ScheduledTender],
                         chat_id: int, bot) -> None:
    """Прогрев → docs с открытия → цена после готовности, не раньше T0."""

    defaults = ctx.config.raw.get("defaults", {})
    fast_t0 = bool(defaults.get("fast_t0", True))

    async def _handle_one(
        tender: ScheduledTender, notify: Callable[[str], None],
    ) -> tuple[str, str | None, float | None]:
        now = _now()
        # Anchor wall-clock T0 to the monotonic clock before warmup/prepare.
        # Starting the timer at phase 2 omitted documents completed after T0.
        # Later wall-clock adjustments must not change the elapsed measurement.
        scheduled_t0_mono = time.monotonic() + (tender.t0 - now).total_seconds()
        warmup_at = tender.t0 - timedelta(seconds=WARMUP_LEAD_S)
        t0_str = tender.t0.strftime("%H:%M:%S")

        warmup_now = now >= warmup_at
        warmup_line = ("Прогрев начнётся <b>немедленно</b>" if warmup_now
                       else f"Прогрев начнётся в <b>{warmup_at.strftime('%H:%M:%S')}</b>")
        mode_line = (
            "⚡ <b>fast-T0</b>: документы с открытия, затем цена+submit не раньше T0"
            if fast_t0 else "обычный режим (полный пайплайн в T0)"
        )
        notify(
            f"⏳ Тендер <b>#{tender.announce_id}</b> — T0 в <b>{t0_str}</b>\n"
            f"{warmup_line}\n{mode_line}"
        )

        if warmup_now:
            notify(
                f"⚡ <b>#{tender.announce_id}</b> — прогрев уже нужен, стартую немедленно!"
            )
        while _now() < warmup_at:
            remaining = (warmup_at - _now()).total_seconds()
            if remaining > NOTIFY_INTERVAL_S:
                await asyncio.sleep(min(NOTIFY_INTERVAL_S, remaining))
                r = int((warmup_at - _now()).total_seconds())
                if r > 0:
                    notify(f"⏳ #{tender.announce_id}: прогрев через {r//60}м {r%60}с")
            else:
                await asyncio.sleep(max(0, remaining))
                break

        # ── ПРОГРЕВ ──────────────────────────────────────────
        notify(f"🔥 <b>#{tender.announce_id}</b> — начинаю прогрев...")
        warmed: dict = {}
        async with AsyncExitStack() as stack:
            try:
                warmed = await _warmup_one(ctx, tender, stack)
                notify(
                    f"✅ <b>#{tender.announce_id}</b> — прогрев готов"
                    + (", жду открытия → готовлю документы…" if fast_t0 else ", жду T0")
                )
            except Exception as e:
                notify(
                    f"⚠️ <b>#{tender.announce_id}</b> — ошибка прогрева: {e}\n"
                    "Ставка пропущена."
                )
                return "failed", None, None

            app_id: str | None = None

            if fast_t0:
                # PHASE-1: poll открытия сразу (не ждём wall T0) + docs.
                # Если тендер откроется в T0 — документы поедут с первой секунды.
                # PHASE-2 начнётся только после готовности документов.
                remaining_to_deadline = max(
                    60.0,
                    (tender.t0 - _now()).total_seconds() + POST_T0_WAIT_S,
                )
                t_prep = time.monotonic()
                ok_prep, prep_msg = await _prepare_one(
                    ctx, tender, warmed, chat_id, bot,
                    max_poll_wait_s=remaining_to_deadline, notify=notify,
                )
                prep_elapsed = round(time.monotonic() - t_prep, 1)
                if ok_prep:
                    app_id = prep_msg
                    notify(
                        f"📄 <b>#{tender.announce_id}</b> — документы готовы "
                        f"(app <code>{app_id}</code>, {prep_elapsed}с).\n"
                        "Подам цену не раньше T0…"
                    )
                else:
                    notify(
                        f"⚠️ <b>#{tender.announce_id}</b> — prepare не удался:\n"
                        f"<code>{prep_msg[:250]}</code>\n"
                        "В T0 попробую полный пайплайн…"
                    )

            # ── ЖДЁМ WALL-CLOCK T0 (если ещё не наступил) ─────────
            wait_s = (tender.t0 - _now()).total_seconds()
            if wait_s > 0:
                await asyncio.sleep(wait_s)
            phase2_started_mono = time.monotonic()

            # ── PHASE-2: цена + submit ────────────────────────────
            if app_id:
                notify(
                    f"🚀 <b>#{tender.announce_id}</b> — T0! "
                    f"Цена+submit (fast path, app <code>{app_id}</code>)…"
                )
                status, detail = await _fire_one(
                    ctx, tender, warmed, chat_id, bot,
                    resume_app_id=app_id,
                    pre_t0_poll=False,
                )
                mode = "fast-T0"
            else:
                notify(f"🚀 <b>#{tender.announce_id}</b> — T0! Полный пайплайн…")
                status, detail = await _fire_one(
                    ctx, tender, warmed, chat_id, bot,
                    resume_app_id=None,
                    pre_t0_poll=True,
                )
                mode = "full"
            completed_mono = time.monotonic()
            elapsed = round(max(0.0, completed_mono - scheduled_t0_mono), 1)
            phase2_elapsed = round(completed_mono - phase2_started_mono, 1)

            if status == "public_ok":
                result = f"✅ <b>#{tender.announce_id}</b> — заявка подана!"
            elif status == "dry_run_complete":
                result = (f"✅ <b>#{tender.announce_id}</b> — черновик готов (dry-run)\n"
                          f"app <code>{detail}</code>")
            else:
                result = (f"❌ <b>#{tender.announce_id}</b> — ошибка:\n"
                          f"<code>{detail[:300]}</code>")
            phase = "фаза подготовки" if status == "dry_run_complete" else "фаза подачи"
            notify(f"{result}\n⏱ {elapsed}с от T0 ({mode}); {phase}: {phase2_elapsed}с")
            return status, detail if status in ("public_ok", "dry_run_complete") else app_id, elapsed

    async def _handle_with_notifications(tender: ScheduledTender):
        # One sender per tender preserves message order without waiting for
        # Telegram before polling, preparing documents or submitting.
        updates: asyncio.Queue[str | None] = asyncio.Queue()

        async def _send_updates():
            while (message := await updates.get()) is not None:
                try:
                    await bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML")
                except TelegramError as e:
                    log.warning("scheduled.notification_failed", announce=tender.announce_id,
                                error=str(e))

        sender = asyncio.create_task(_send_updates())
        try:
            trace = BidTrace(f"#{tender.announce_id}") if getattr(ctx, "run_reports", None) else None
            token = current_trace.set(trace) if trace is not None else None
            try:
                status, app_id, elapsed = await _handle_one(tender, updates.put_nowait)
            finally:
                if token is not None:
                    current_trace.reset(token)
            updates.put_nowait(None)
            if trace is not None:
                if not any(phase in {"public_done", "dry_run_complete"} for _, phase in trace.marks):
                    trace.mark("bid_finished")
                await deliver_timing_report(
                    ctx, bot, chat_id, trace, tender.announce_id, app_id,
                    result=status, scheduled_elapsed=elapsed,
                )
            await sender
        finally:
            if not sender.done():
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)

    await asyncio.gather(*[_handle_with_notifications(tender) for tender in tenders])


# ─────────────────────────────────────────────────────────────────────────────
# Command handler
# ─────────────────────────────────────────────────────────────────────────────

@whitelist_only
async def schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/schedule 17058849 09:00
    /schedule 17058849 09:00 17058850 09:05
    /schedule 17058849 2026-06-01 09:00
    """
    ctx: BotContext = context.application.bot_data["ctx"]
    raw = " ".join(context.args or [])

    if not raw.strip():
        await update.effective_chat.send_message(
            "📋 <b>Использование:</b>\n\n"
            "<code>/schedule 17058849 09:00</code>\n"
            "<code>/schedule 17058849 2026-06-01 09:00</code>\n"
            "<code>/schedule 17058849 09:00 17058850 09:05</code>\n\n"
            "Бот прогреется за 5 минут до T0.\n"
            "⚡ <b>fast-T0</b>: как только тендер откроется — готовит документы;\n"
            "после готовности подаёт цену и заявку, не раньше T0.\n"
            "Время от T0 включает документы, если они готовились после T0.\n\n"
            "Выключить: в config.yaml → <code>defaults.fast_t0: false</code>",
            parse_mode="HTML",
        )
        return

    result = _parse_schedule_args(raw)
    if isinstance(result, str):
        await update.effective_chat.send_message(
            f"❌ Ошибка парсинга: {result}", parse_mode="HTML")
        return

    tenders = result
    lines = []
    for t in tenders:
        lines.append(f"• <b>#{t.announce_id}</b> — T0: <b>{t.t0.strftime('%d.%m.%Y %H:%M')}</b>")

    await update.effective_chat.send_message(
        f"📋 <b>Запланировано {len(tenders)} тендер(ов):</b>\n"
        + "\n".join(lines)
        + "\n\n⏰ Прогрев за 5 мин до T0 → документы с открытия → "
          "цена+submit после готовности, не раньше T0.\n"
          "Для отмены: /cancel",
        parse_mode="HTML",
    )

    # Запускаем планировщик в фоне
    chat_id = update.effective_chat.id
    asyncio.ensure_future(
        _run_scheduled(ctx, tenders, chat_id, context.bot)
    )


def build_schedule_handler() -> CommandHandler:
    return CommandHandler("schedule", schedule_cmd)
