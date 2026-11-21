"""/schedule — Запланированные ставки с известным временем открытия.

Формат команды:
    /schedule 17058849 09:00
    /schedule 17058849 2026-06-01 09:00
    /schedule 17058849 09:00 17058850 09:00     ← несколько тендеров

Как работает:
    1. Бот парсит список тендеров и времён T0
    2. За WARMUP_LEAD_S секунд до каждого T0 запускает прогрев
       (login, pool x6, TumarCSP) — всё параллельно для всех тендеров
    3. Ровно в T0 — мгновенная подача без poll (pre_t0_poll=False)
    4. Несколько тендеров в одно время — параллельные ставки
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
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
from telegram.ext import CommandHandler, ContextTypes

from ..auth import whitelist_only
from ..context import BotContext
from ..progress import ProgressMessage
from ... import flow as flow_repairs
from ...client import GoszakupClient
from ...flow import AtagosBidParams, BeneficiaryInfo
from ...local_signer import LocalSigner
from ...state import StateStore

log = structlog.get_logger("scheduled")

# Сколько секунд до T0 начинаем прогрев (login, pool, TumarCSP)
WARMUP_LEAD_S = 300   # 5 минут
# Шаг уведомления "ждём T0" в секундах
NOTIFY_INTERVAL_S = 60


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

    # Логинимся через ЭЦП
    try:
        ci = await gz.login(
            account_password=ctx.account_password,
            kalkan_signer=ctx.signer,
        )
        ctx.set_ci_session(ci)
        log.info("scheduled.warmup.login", announce=tender.announce_id)
    except Exception as e:
        log.warning("scheduled.warmup.login_failed", announce=tender.announce_id,
                    err=str(e)[:100])

    # Прогрев doc_pool — 6 параллельных сессий
    ns = LocalSigner(kalkan_signer=ctx.signer, http_client=gz._http)
    await stack.enter_async_context(ns)

    acc_pw = ctx.account_password
    signer = ctx.signer

    async def _spawn_session():
        sib = gz.clone_unauthenticated()
        await stack.enter_async_context(sib)
        sib_ns = LocalSigner(kalkan_signer=signer, http_client=sib._http)
        await sib.login(account_password=acc_pw, kalkan_signer=sib_ns)
        return (sib, sib_ns)

    doc_plan = ctx.config.raw.get("defaults", {}).get("doc_plan") or []
    n_sessions = max(6, len(doc_plan))
    t0_pool = time.monotonic()
    log.info("scheduled.warmup.pool.start", announce=tender.announce_id,
             n=n_sessions)
    doc_pool = list(await asyncio.gather(*[_spawn_session() for _ in range(n_sessions)]))
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
        auto_login_account_password=acc_pw,
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

# Максимальное время ожидания открытия тендера после T0 (секунды).
# Если тендер открылся чуть позже — бот будет поллить до этого предела.
POST_T0_WAIT_S = 300  # 5 минут


async def _fire_one(ctx: BotContext, tender: ScheduledTender,
                    warmed: dict, chat_id: int, bot) -> tuple[bool, str]:
    """Запустить pipeline. Поллит до POST_T0_WAIT_S секунд на случай
    если тендер открылся чуть позже запланированного времени."""
    defaults = ctx.config.raw.get("defaults", {})
    floor_ratio = float(defaults.get("dumping_floor_ratio", 0.80))

    state_store = StateStore(
        state_dir=ctx.config.state_dir,
        namespace="scheduled",
    )

    raw_tumarcsp = ctx.config.raw.get("tumarcsp", {})
    raw_ben = ctx.config.raw.get("beneficiary", {})

    # Если lot_ids не известны — открываем страницу тендера и берём первый лот
    lot_ids = tender.lot_ids
    if not lot_ids:
        try:
            lots = await warmed["gz"].discover_announce_lots(tender.announce_id)
            if lots:
                lot_ids = [int(lots[0]["lot_id"])]
                log.info("scheduled.fire.lots_discovered",
                         announce=tender.announce_id, lot_ids=lot_ids)
            else:
                return False, f"лоты не найдены для #{tender.announce_id}"
        except Exception as e:
            return False, f"ошибка получения лотов: {e}"

    try:
        state = await flow_repairs.submit_bid(
            params=AtagosBidParams(
                announce_id=tender.announce_id,
                tender_lot_ids=lot_ids,
                price=None,
                dumping_floor_ratio=floor_ratio,
                dry_run=False,
                signer=ctx.signer,
                tumar=warmed.get("tumar"),
                tumar_api_key=raw_tumarcsp.get("api_key"),
                tumar_ws_url=raw_tumarcsp.get("ws_url", "wss://127.0.0.1:6127/tumarcsp/"),
                autofill_price_popup=raw_tumarcsp.get("autofill_popup", False),
                popup_coords=raw_tumarcsp.get("popup"),
                auto_login_account_password=ctx.account_password,
                doc_save_mode=defaults.get("doc_save_mode", "serial"),
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
            pre_t0_poll=True,   # поллим до POST_T0_WAIT_S сек на случай позднего открытия
            poll_interval_s=0.5,
            proxy=ctx.config.proxy_url,
            gz_client=warmed["gz"],
            lp_id_cache=ctx.lp_id_cache,
            reuse_doc_pool=warmed["doc_pool"],
            tz_sig_cache=ctx.tz_sig_cache,
        )
        ok = state.step == "public_ok"
        err = state.error or state.step
        return ok, err
    except Exception as e:
        log.exception("scheduled.fire.failed", announce=tender.announce_id)
        return False, str(e)[:300]


# ─────────────────────────────────────────────────────────────────────────────
# Главная корутина планировщика
# ─────────────────────────────────────────────────────────────────────────────

async def _run_scheduled(ctx: BotContext, tenders: list[ScheduledTender],
                         chat_id: int, bot) -> None:
    """Ждём T0, прогреваем, стреляем. Для нескольких тендеров — параллельно."""

    async def _handle_one(tender: ScheduledTender):
        now = _now()
        warmup_at = tender.t0 - timedelta(seconds=WARMUP_LEAD_S)
        t0_str = tender.t0.strftime("%H:%M:%S")

        # Уведомляем что ждём
        await bot.send_message(
            chat_id=chat_id,
            text=(f"⏳ Тендер <b>#{tender.announce_id}</b> — T0 в <b>{t0_str}</b>\n"
                  f"Прогрев начнётся в <b>{warmup_at.strftime('%H:%M:%S')}</b>"),
            parse_mode="HTML",
        )

        # Если прогрев уже должен был начаться — стартуем сразу
        if _now() >= warmup_at:
            await bot.send_message(
                chat_id=chat_id,
                text=f"⚡ <b>#{tender.announce_id}</b> — прогрев уже нужен, стартую немедленно!",
                parse_mode="HTML",
            )
        # Иначе периодические уведомления пока ждём
        while _now() < warmup_at:
            remaining = (warmup_at - _now()).total_seconds()
            if remaining > NOTIFY_INTERVAL_S:
                await asyncio.sleep(min(NOTIFY_INTERVAL_S, remaining))
                r = int((warmup_at - datetime.now()).total_seconds())
                if r > 0:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"⏳ #{tender.announce_id}: прогрев через {r//60}м {r%60}с",
                        parse_mode="HTML",
                    )
            else:
                await asyncio.sleep(max(0, remaining))
                break

        # ── ПРОГРЕВ ──────────────────────────────────────────
        await bot.send_message(
            chat_id=chat_id,
            text=f"🔥 <b>#{tender.announce_id}</b> — начинаю прогрев...",
            parse_mode="HTML",
        )
        warmed: dict = {}
        async with AsyncExitStack() as stack:
            try:
                warmed = await _warmup_one(ctx, tender, stack)
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ <b>#{tender.announce_id}</b> — прогрев готов, жду T0",
                    parse_mode="HTML",
                )
            except Exception as e:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ <b>#{tender.announce_id}</b> — ошибка прогрева: {e}",
                    parse_mode="HTML",
                )

            # ── ЖДЁМ ТОЧНО ДО T0 ──────────────────────────────────
            wait_s = (tender.t0 - _now()).total_seconds()
            if wait_s > 0:
                await asyncio.sleep(wait_s)

            # ── ОГОНЬ! ─────────────────────────────────────────────
            t_start = time.monotonic()
            await bot.send_message(
                chat_id=chat_id,
                text=f"🚀 <b>#{tender.announce_id}</b> — T0! Подаю заявку...",
                parse_mode="HTML",
            )
            ok, err = await _fire_one(ctx, tender, warmed, chat_id, bot)
            elapsed = round(time.monotonic() - t_start, 1)

            if ok:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(f"✅ <b>#{tender.announce_id}</b> — заявка подана!\n"
                          f"⏱ {elapsed}с от T0"),
                    parse_mode="HTML",
                )
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(f"❌ <b>#{tender.announce_id}</b> — ошибка:\n"
                          f"<code>{err[:300]}</code>\n⏱ {elapsed}с от T0"),
                    parse_mode="HTML",
                )

    # Запускаем все тендеры параллельно
    await asyncio.gather(*[_handle_one(t) for t in tenders])


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
            "Бот прогреется за 5 минут до T0 и подаст заявку ровно в указанное время.",
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
        + "\n\n⏰ Прогрев начнётся за 5 минут до T0.\n"
          "Для отмены используй /cancel",
        parse_mode="HTML",
    )

    # Запускаем планировщик в фоне
    chat_id = update.effective_chat.id
    asyncio.ensure_future(
        _run_scheduled(ctx, tenders, chat_id, context.bot)
    )


def build_schedule_handler() -> CommandHandler:
    return CommandHandler("schedule", schedule_cmd)
