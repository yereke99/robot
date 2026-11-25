"""/bid — ConversationHandler that walks the user through one bid.

States:
    PICK_LOT       — multi-lot tender; pick one via inline buttons
    ENTER_PRICE    — user types price in KZT
    CONFIRM_DRY    — show summary; user taps "🧪 Сухой прогон" or "❌ Отмена"
    POST_DRY       — dry-run finished; 5-sec countdown button → finalize

Lifecycle invariants:
  * Only one bid in flight per bot process (bid_lock).
  * Real submit only available after a successful dry-run for the same
    (announce_id, lot_ids, price) tuple — enforced via `parent_bid_id` in
    history_db and via the explicit POST_DRY state.
"""

from __future__ import annotations

import asyncio
import io
import logging
from html import escape

import structlog
import time
from typing import Any, Optional

from websockets.protocol import State

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import re

from ..auth import whitelist_only
from ..context import BidQueueItem, BotContext
from . import settings as settings_handler
from ..progress import ProgressMessage, current_progress
from ..search import probe_cookie
from ..texts import BID_QUEUE_YOUR_TURN, BUSY_QUEUE, CANCELLED
from ... import secrets as gz_secrets
from ...client import AlreadyPrepared, GoszakupClient, SessionExpired
from ...pipeline import BidParams, finalize_application, submit_bid
from ... import flow as flow_repairs
from ...flow import AtagosBidParams, BeneficiaryInfo
from ...host_diagnostics import runtime_summary
from ...state import StateStore
from ...timing import BidTrace, current_trace
from ...timing import poll_interval_s as configured_poll_interval_s


COOKIE_PROBE_CACHE_S = 30.0
_TIMING_SUMMARY_LIMIT = 1800

# bid_row_id -> SubmissionState.timestamps of that run, handed from
# _run_pipeline to whoever renders the result message (popped on read).
_BID_TIMESTAMPS: dict[int, dict] = {}

# bid_row_id -> timing trace of that run; chat_id -> (filename, text) of the last report
_BID_TRACES: dict[int, BidTrace] = {}
_LAST_REPORT: dict[int, tuple[str, str]] = {}


async def _send_timing_report(
    ctx, bot, chat_id, bid_row_id, announce_id, app_id, *, result="failed",
) -> None:
    """Send the per-request timeline after a bid and save it for /timing."""
    trace = _BID_TRACES.pop(bid_row_id, None)
    if trace is not None:
        await deliver_timing_report(ctx, bot, chat_id, trace, announce_id, app_id, result=result)


async def deliver_timing_report(
    ctx, bot, chat_id, trace: BidTrace, announce_id, app_id, *,
    result: str, scheduled_elapsed: float | None = None,
) -> None:
    reports = getattr(ctx, "run_reports", None)
    try:
        git_name = await reports.save_bid(trace, result, scheduled_elapsed) if reports is not None else None
        title = f"Тендер #{announce_id} · app {app_id or '—'}"
        if scheduled_elapsed is not None:
            title += f"\nОт T0 по расписанию {scheduled_elapsed:.2f} с"
        try:
            runtime = await asyncio.to_thread(runtime_summary)
        except Exception as exc:
            log.warning("bid.runtime_diagnostics_failed", error_type=type(exc).__name__)
            runtime = ""
        text = trace.render(title=title, runtime=runtime)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        if git_name:
            text = f"Имя файла для GitHub {git_name}\n\n{text}"
            stamp += "_" + git_name.rsplit("_", 1)[-1].removesuffix(".txt")
        name = f"timing_{announce_id}_{stamp}.txt"
        _LAST_REPORT[chat_id] = (name, text)
        config = getattr(ctx, "config", None)
        log_dir = getattr(config, "log_dir", None)
        if log_dir:
            from pathlib import Path
            folder = Path(log_dir) / "timing"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text(text, encoding="utf-8")
        raw = getattr(config, "raw", None) or {}
        if raw.get("defaults", {}).get("send_timing_report", True):
            if reports is not None:
                recipients = [reports.chat_id] if reports.chat_id is not None else []
            else:
                extra_chats = (raw.get("telegram") or {}).get("report_chat_ids") or []
                recipients = list(dict.fromkeys([chat_id, *map(int, extra_chats)]))
            for target in recipients:
                try:
                    await bot.send_document(
                        chat_id=target,
                        document=InputFile(io.BytesIO(text.encode("utf-8")), filename=name),
                        caption=f"⏱ Отчёт о подаче #{announce_id}: запросы, время, журнал",
                    )
                except Exception as e:  # noqa: BLE001 — one unreachable chat must not stop the rest
                    log.warning("bid.timing_report.send_failed", error_type=type(e).__name__)
    except Exception as e:  # noqa: BLE001 — diagnostics must never break a bid
        log.warning("bid.timing_report.failed", error_type=type(e).__name__)


def _queue_timing_report(context, ctx, chat_id, row_id, announce_id, app_id, *, ok: bool, dry: bool) -> None:
    result = "dry_run_complete" if dry else "public_ok" if ok else "failed"
    report = _send_timing_report(ctx, context.bot, chat_id, row_id, announce_id, app_id, result=result)
    if getattr(ctx, "run_reports", None) is not None:
        context.application.create_task(report)
    else:
        asyncio.ensure_future(report)


_PHASE_LABELS = (
    ("create_lots", "заявка+лоты"),
    ("docs", "документы"),
    ("price", "цена"),
    ("submit", "отправка"),
)


def _timing_text(
    bid_row_id: Optional[int], elapsed_total: float, *, dry: bool = False, failed: bool = False,
) -> str:
    """Render the result timings before the report task takes the trace."""
    ts = _BID_TIMESTAMPS.pop(bid_row_id, None) if bid_row_id is not None else None
    tb = flow_repairs.timing_breakdown(ts) if ts else {}
    trace = _BID_TRACES.get(bid_row_id) if bid_row_id is not None else None
    if failed and "from_open" not in tb and trace is not None:
        marks = {phase: at for at, phase in getattr(trace, "marks", ())}
        if "t0" in marks and marks.get("bid_finished", -1) >= marks["t0"]:
            tb["from_open"] = round(marks["bid_finished"] - marks["t0"], 1)
    if "from_open" not in tb:
        lines = [f"⏱ <b>Время:</b> {elapsed_total} сек"]
    else:
        head = "От открытия до готовности" if dry else "От открытия до подачи"
        if failed:
            head = "От открытия до остановки"
        lines = [f"⏱ <b>{head}:</b> {tb['from_open']} сек"]
        parts = [f"{label} {tb[key]}" for key, label in _PHASE_LABELS if key in tb]
        if parts:
            lines.append("   " + " · ".join(parts))
        if "to_publish_request" in tb and "publish_request" in tb:
            lines.append(f"📨 Начало запроса публикации: {tb['to_publish_request']} сек · "
                         f"ответ портала: {tb['publish_request']} сек")
        wait = tb.get("wait")
        if wait is not None and wait >= 1.0:
            lines.append(f"⏳ Ждал открытия тендера: {wait} сек (в расчёт не входит)")

    if trace is not None:
        try:
            summary = trace.render_summary(max_chars=_TIMING_SUMMARY_LIMIT)
        except Exception as e:
            # Keep the result visible even if the diagnostics cannot be rendered.
            log.warning("bid.timing_summary.failed", error_type=type(e).__name__)
        else:
            details = []
            remaining = _TIMING_SUMMARY_LIMIT
            for row in summary.splitlines():
                safe_row = escape(row)
                length = len(safe_row.encode("utf-16-le")) // 2 + 1
                if length > remaining:
                    break
                details.append(safe_row)
                remaining -= length
            if details:
                lines.extend(["", "🔎 <b>Запросы и задержки</b>", *details,
                              "Полный отчёт доступен через /timing."])
    return "\n".join(lines)


async def _is_cookie_fresh(ctx: BotContext) -> bool:
    """Cached cookie liveness probe. Used to skip 3.5s auto-login.

    Cache TTL is 30s — short enough to catch a freshly-killed cookie
    (e.g. Chrome sign-in), long enough to skip the probe inside rapid
    sequential bids.
    """
    cached = ctx.last_cookie_probe
    if cached and time.time() - cached[0] < COOKIE_PROBE_CACHE_S:
        return cached[1]
    if ctx.gz_client is None:
        return False
    try:
        alive = await probe_cookie(ctx.gz_client)
    except Exception:
        alive = False
    ctx.last_cookie_probe = (time.time(), alive)
    return alive


async def _ensure_tumar(ctx: BotContext, raw_tumarcsp: dict):
    """Connect TumarCSP once; reuse across /bid (saves ~1-2s per bid)."""
    existing = getattr(ctx, "tumar", None)
    if existing is not None:
        ws = getattr(existing, "_ws", None)
        if ws is not None and ws.state == State.OPEN:
            if getattr(existing, "_autofill", None) is None:
                from ...popup_filler import is_supported, make_autofiller
                if is_supported():
                    pc = raw_tumarcsp.get("popup") or {}
                    existing._autofill = make_autofiller(
                        field_xy=pc.get("field_xy"), ok_xy=pc.get("ok_xy"),
                        field_offset=pc.get("field_offset"), ok_offset=pc.get("ok_offset"),
                        detect_xy=pc.get("detect_xy"), detect_rgb=pc.get("detect_rgb"),
                    )
            return existing
        # A startup connection may have closed while waiting for a tender.
        await existing.close()
        ctx.tumar = None
    api_key = raw_tumarcsp.get("api_key")
    if not api_key:
        return None
    from ...popup_filler import is_supported, make_autofiller
    from ...tumar import TumarCSP

    autofill = None
    if is_supported():
        pc = raw_tumarcsp.get("popup") or {}
        autofill = make_autofiller(
            field_xy=pc.get("field_xy"), ok_xy=pc.get("ok_xy"),
            field_offset=pc.get("field_offset"), ok_offset=pc.get("ok_offset"),
            detect_xy=pc.get("detect_xy"), detect_rgb=pc.get("detect_rgb"),
        )
    tumar = TumarCSP(
        api_key=api_key,
        ws_url=raw_tumarcsp.get("ws_url", "wss://127.0.0.1:6127/tumarcsp/"),
        autofill=autofill,
    )
    try:
        await tumar.connect()
    except BaseException:
        await tumar.close()
        raise
    ctx.tumar = tumar
    log.info("bid.tumar.warmed", version=tumar.version)
    return tumar


async def _ensure_logged_in(ctx: BotContext) -> bool:
    """Make sure the shared client's cookie is alive.

    If the probe says it's dead, run the 5-step ECP login on the SAME client
    so the bid pipeline can skip auto-login. Returns True if we end up
    logged in, False otherwise (caller surfaces SessionExpired).
    """
    if await _is_cookie_fresh(ctx):
        return True
    if not ctx.account_password or ctx.gz_client is None:
        return False
    try:
        ci = await ctx.gz_client.login(
            account_password=ctx.account_password,
            kalkan_signer=ctx.signer,
        )
        # gz_secrets imported at module top — `..` would point at
        # goszakup.bot (no `secrets` module there); the correct relative
        # is at module level already.
        try:
            gz_secrets.set(ctx.config.ci_session_service, "ci_session", ci)
        except Exception:
            pass
        ctx.set_ci_session(ci)
        ctx.last_login_at = time.time()
        ctx.last_cookie_probe = (time.time(), True)
        log.info("opt1.bot_relogin.done")
        return True
    except Exception as e:
        log.warning("opt1.bot_relogin.failed", error_type=type(e).__name__)
        ctx.last_cookie_probe = (time.time(), False)
        return False


_ANNOUNCE_URL_RE = re.compile(r"/announce/index/(\d+)")


def _extract_announce_id(raw: str) -> Optional[str]:
    """Pull an announce_id out of raw text. Accepts:

    - "16941325"
    - "https://v3bl.goszakup.gov.kz/ru/announce/index/16941325"
    - "v3bl.goszakup.gov.kz/kz/announce/index/16941325?..."
    - "16941325-1"   (treats the digits before the dash as the announce_id)
    - "  16941325  "
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    m = _ANNOUNCE_URL_RE.search(raw)
    if m:
        return m.group(1)
    # Pure-digit or contains 6-12 digits.
    m2 = re.search(r"\b(\d{6,12})\b", raw)
    if m2:
        return m2.group(1)
    return None

log = structlog.get_logger(__name__)  # accepts arbitrary kwargs in .info/.warning


PICK_LOT = 0
# Hardcoded bid price. For this procurement method (Конкурс по приобретению
# услуг по организации питания, code 120), the initial price field is
# always submitted as 0 — actual price haggling happens later in the
# procedure. The user never enters a price.
BID_PRICE = 0


def _ctx(context: ContextTypes.DEFAULT_TYPE) -> BotContext:
    return context.application.bot_data["ctx"]


def _fmt_price(p: int) -> str:
    return f"{p:,}".replace(",", " ") + " ₸"


def _failure_recovery_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Повторить", callback_data="bid_retry"),
            InlineKeyboardButton("🔐 /login", callback_data="trigger_login"),
        ],
    ])


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

async def _enter_bid_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    announce_id: str,
    preset_price: Optional[int] = None,    # legacy kwarg; ignored now (price is fixed)
):
    """Shared entry: from /bid <id>, callback_data 'bid_pick:<id>'.

    Step 1 — auto-login if cookie's dead (we do it BEFORE discovery so
    the user sees a single coherent progress message instead of "Cookie
    протухла" mid-flow).
    Step 2 — discover lots.
    Step 3 — single lot → straight to confirm; multi-lot → picker.
    Price is hardcoded to 0; never prompted.
    """
    del preset_price       # not used anymore — kept in sig for callback compat
    ctx = _ctx(context)
    context.user_data.clear()
    context.user_data["conversation_active"] = True
    context.user_data["announce_id"] = announce_id

    msg = await update.effective_chat.send_message(
        f"🔍 Принял <code>{announce_id}</code>. Проверяю сессию…",
        parse_mode="HTML",
    )

    # Step 1: verify active session.
    if not await _is_cookie_fresh(ctx):
        await msg.edit_text(
            "❌ <b>Сессия Goszakup не авторизована или устарела.</b>\n\n"
            "Текущая кука <code>ci_session</code> не дает доступ к кабинету (портал перенаправляет на страницу входа).\n\n"
            "<b>Как передать рабочую сессию:</b>\n"
            "1. Откройте в браузере: <a href='https://v3bl.goszakup.gov.kz/ru/cabinet/profile'>https://v3bl.goszakup.gov.kz/ru/cabinet/profile</a>\n"
            "2. Убедитесь, что кабинет открылся (справа вверху видно ваше имя или ТОО «Беласар Ер-Н»).\n"
            "3. Нажмите <code>F12</code> → вкладка <b>Application</b> (Приложение) → <b>Cookies</b> → <code>https://v3bl.goszakup.gov.kz</code>.\n"
            "4. Скопируйте значение <b><code>ci_session</code></b>.\n"
            "5. Отправьте боту команду:\n<code>/cookie ВАШ_CI_SESSION</code>\n\n"
            "После сообщения бота об успешной проверке отправьте номер закупки снова.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        context.user_data.clear()
        return ConversationHandler.END

    # Step 2: discover lots.
    await msg.edit_text(
        f"🔍 Получаю лоты <code>{announce_id}</code>…",
        parse_mode="HTML",
    )
    try:
        gz = ctx.gz_client
        lots = await gz.discover_announce_lots(announce_id)
    except SessionExpired:
        # Should be rare after the explicit login above; surface plainly.
        await msg.edit_text("🔐 Сессия отвалилась прямо в момент запроса. Попробуй ещё раз.")
        context.user_data.clear()
        return ConversationHandler.END
    except Exception as e:
        await msg.edit_text(
            f"❌ Не получилось получить лоты: <code>{e}</code>",
            parse_mode="HTML",
            reply_markup=_failure_recovery_keyboard(),
        )
        context.user_data.clear()
        return ConversationHandler.END

    if not lots:
        await msg.edit_text(
            f"❌ В тендере <code>{announce_id}</code> нет лотов или он не публичный.",
            parse_mode="HTML",
        )
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data["lots"] = lots
    context.user_data["entry_msg_id"] = msg.message_id

    # Step 3: lot pick — auto if single, then INSTANTLY submit. No dry-run,
    # no confirmation, no countdown. Pasting the URL IS the consent.
    if len(lots) == 1:
        context.user_data["chosen_lot_ids"] = [int(lots[0]["lot_id"])]
        return await _kick_off_submit(update, context)

    rows = []
    for l in lots:
        try:
            sum_val = float(str(l["sum"]).replace(" ", "").replace("\xa0", "").replace(",", "."))
            sum_disp = f"{sum_val / 1_000_000:.2f}M ₸"
        except Exception:
            sum_disp = str(l["sum"])
        rows.append([InlineKeyboardButton(
            f"Лот {l['lot_number']} · {sum_disp}",
            callback_data=f"lot_pick:{l['lot_id']}",
        )])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="bid_cancel")])
    await msg.edit_text(
        f"🎯 Тендер <code>{announce_id}</code> — {len(lots)} лотов. Выбери:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return PICK_LOT


@whitelist_only
async def bid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_chat.send_message(
            "Пришли announce_id числом: <code>17042109</code>\n"
            "Или несколько сразу (параллельно): <code>/bid 17042109 17042110</code>",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    # Собираем все announce_id из аргументов
    ids = []
    for arg in context.args:
        aid = _extract_announce_id(arg)
        if aid:
            ids.append(aid)

    if not ids:
        await update.effective_chat.send_message(
            "Не нашёл announce_id. Пришли число или полную ссылку.",
        )
        return ConversationHandler.END

    # Несколько тендеров → параллельная подача без conversation
    if len(ids) > 1:
        await update.effective_chat.send_message(
            "🚀 <b>Параллельная подача</b> на {} тендеров:\n{}".format(
                len(ids),
                "\n".join(f"• <code>{i}</code>" for i in ids),
            ),
            parse_mode="HTML",
        )
        asyncio.ensure_future(
            _multi_bid(update, context, ids)
        )
        return ConversationHandler.END

    return await _enter_bid_flow(update, context, announce_id=ids[0])


def _exception_message(error: Exception) -> str:
    return str(error).strip() or type(error).__name__


async def _multi_bid(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    announce_ids: list[str],
) -> None:
    """Параллельная мгновенная подача на несколько тендеров."""
    ctx = _ctx(context)
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    username = update.effective_user.username

    async def _one(announce_id: str):
        pm = ProgressMessage.start(
            context.bot, chat_id, "🔍 Получаю лоты…",
            header=f"🚀 <b>Подаю заявку #{announce_id}</b>",
        )
        started_at = time.monotonic()
        try:
            # Автоматически берём первый лот
            gz = ctx.gz_client
            lots = await gz.discover_announce_lots(announce_id)
            if not lots:
                await pm.finish("❌ Лоты не найдены")
                return
            lot_ids = [int(lots[0]["lot_id"])]
            pm.update("👀 Жду открытия тендера…")
            _dry = bool((ctx.bid_settings or {}).get("dry_run", True))
            ok, app_id, err, row_id = await _run_pipeline(
                ctx, user_id, chat_id, username, pm,
                announce_id=announce_id, lot_ids=lot_ids, price=0,
                dry_run=_dry,
            )
            elapsed_multi = round(time.monotonic() - started_at, 1)
            is_dry = err == "dry_run_complete"
            elapsed_text = _timing_text(row_id, elapsed_multi, dry=is_dry, failed=not ok)
            _queue_timing_report(context, ctx, chat_id, row_id, announce_id, app_id, ok=ok, dry=is_dry)
            if ok:
                headline = ("dry-run OK, <b>НЕ подана</b>" if is_dry else "заявка подана!")
                await pm.finish(
                    f"✅ <b>#{announce_id}</b> — {headline}\n"
                    f"{elapsed_text}",
                )
            else:
                await pm.finish(
                    f"❌ <b>#{announce_id}</b> — {escape((err or '')[:200])}\n"
                    f"{elapsed_text}",
                )
        except asyncio.CancelledError:
            pm.cancel()
            raise
        except Exception as e:
            log.exception("multi_bid.failed", announce=announce_id)
            await pm.finish(f"❌ <b>#{announce_id}</b> — ошибка: {escape(_exception_message(e)[:200])}")

    await asyncio.gather(*[_one(aid) for aid in announce_ids])


def _looks_like_cookie(text: str) -> bool:
    clean = (text or "").strip().strip('"').strip("'")
    if "ci_session=" in clean or "BNES_" in clean or clean.lower().startswith("curl ") or "--url" in clean:
        return True
    if len(clean) > 80 and " " not in clean:
        return True
    return False


@whitelist_only
async def text_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Plain text (число, goszakup URL, or raw cookie)."""
    text = (update.message.text or "").strip()
    if _looks_like_cookie(text):
        raw = text.strip('"').strip("'")
        ctx = _ctx(context)
        msg = await update.message.reply_text("🔍 Проверяю сессию и доступ в личный кабинет…")

        cookie_header = None
        ci_session_val = None
        if "curl " in raw.lower() and "-b " in raw:
            m_b = re.search(r'-(?:b|-cookie)\s+["\']?([^"\'\n]+)["\']?', raw)
            if m_b:
                raw = m_b.group(1).replace("^", "").strip()

        if ";" in raw or "BNES_" in raw or "agreement_user_2024=" in raw:
            cookie_header = raw
            m = re.search(r"ci_session=([^;\s]+)", raw)
            ci_session_val = m.group(1) if m else raw
        else:
            ci_session_val = raw
            cookie_header = None

        if cookie_header:
            ctx.set_ci_session(cookie_header)
            gz_secrets.set(ctx.config.ci_session_service, "cookie_header", cookie_header)
            if ci_session_val:
                gz_secrets.set(ctx.config.ci_session_service, "ci_session", ci_session_val)
        else:
            ctx.set_ci_session(ci_session_val)
            gz_secrets.set(ctx.config.ci_session_service, "ci_session", ci_session_val)

        import time
        ctx.last_login_at = time.time()
        try:
            alive = await probe_cookie(ctx.gz_client)
            if alive:
                ctx.last_cookie_probe = (time.time(), True)
                await msg.edit_text(
                    "✅ <b>Cookie успешно сохранена и проверена!</b>\n\n"
                    "Личный кабинет активен. Теперь можете отправить номер закупки (тендера) для подачи заявки.",
                    parse_mode="HTML",
                )
            else:
                ctx.last_cookie_probe = (time.time(), False)
                await msg.edit_text(
                    "⚠️ <b>Cookie сохранена, но сайт перенаправляет на страницу входа.</b>\n\n"
                    "Это значит, что на портале сессия не авторизована или уже истекла.\n\n"
                    "Пожалуйста:\n"
                    "1. Откройте <a href='https://v3bl.goszakup.gov.kz/cabinet/profile'>https://v3bl.goszakup.gov.kz/cabinet/profile</a>\n"
                    "2. Убедитесь, что личный кабинет открыт под вашей ЭЦП.\n"
                    "3. Скопируйте свежее значение <code>ci_session</code> из Cookies (F12) и пришлите сюда.",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
        except Exception as e:
            ctx.last_cookie_probe = None
            await msg.edit_text(f"⚠️ Cookie сохранена в боте. (Ошибка при проверке профиля: {e})", parse_mode="HTML")
        return ConversationHandler.END

    # Поиск всех номеров закупки в сообщении (для параллельной подачи нескольких тендеров)
    found_ids = []
    for token in re.split(r"[\s,;\n]+", text):
        aid = _extract_announce_id(token)
        if aid and aid not in found_ids:
            found_ids.append(aid)

    if len(found_ids) > 1:
        await update.effective_chat.send_message(
            "🚀 <b>Параллельная подача</b> на {} тендеров:\n{}".format(
                len(found_ids),
                "\n".join(f"• <code>{i}</code>" for i in found_ids),
            ),
            parse_mode="HTML",
        )
        asyncio.ensure_future(_multi_bid(update, context, found_ids))
        return ConversationHandler.END

    if len(found_ids) == 1:
        return await _enter_bid_flow(update, context, announce_id=found_ids[0])
    await update.message.reply_text(
        "Не понял команду.\n\n"
        "• Отправьте номер тендера (например: <code>17042109</code>) или ссылку на закупку для подачи заявки.\n"
        "• Или отправьте cookie <code>ci_session</code> для обновления сессии.\n"
        "• <code>/help</code> — список всех команд.",
        parse_mode="HTML",
    )
    return ConversationHandler.END


@whitelist_only
async def bid_pick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, announce_id = q.data.split(":", 1)
    return await _enter_bid_flow(update, context, announce_id=announce_id)


@whitelist_only
async def rebid_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, announce_id, price = q.data.split(":", 2)
    return await _enter_bid_flow(update, context, announce_id=announce_id, preset_price=int(price))


# ---------------------------------------------------------------------------
# Lot picker (only when multi-lot)
# ---------------------------------------------------------------------------

@whitelist_only
async def lot_pick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, lot_id = q.data.split(":", 1)
    context.user_data["chosen_lot_ids"] = [int(lot_id)]
    return await _kick_off_submit(update, context, replace_msg_id=q.message.message_id)


# ---------------------------------------------------------------------------
# Submit execution (no dry-run, no countdown — instant)
# ---------------------------------------------------------------------------

def _failed_application_id(state_store: StateStore, announce_id: str, started_at: float) -> str | None:
    try:
        state = state_store.load(announce_id)
        # The state file may still contain a draft from an earlier attempt.
        if state.timestamps.get("created", 0) >= started_at:
            return state.application_id
    except (OSError, TypeError, ValueError, AttributeError) as exc:
        log.warning("bid.failure_state_unavailable", error_type=type(exc).__name__)
    return None


async def _run_pipeline(
    ctx: BotContext,
    user_id: int,
    chat_id: int,
    username: Optional[str],
    pm: ProgressMessage,
    *,
    announce_id: str,
    lot_ids: list[int],
    price: int,
    dry_run: bool,
    parent_bid_id: Optional[int] = None,
    pre_t0_poll: bool = True,
    poll_interval_s: float | None = None,
) -> tuple[bool, Optional[str], Optional[str], int]:
    """Run one pipeline pass. Returns (ok, app_id, error, bid_row_id)."""
    if poll_interval_s is None:
        poll_interval_s = configured_poll_interval_s(ctx.config.raw)
    bid_row_id = ctx.history.insert_pending(
        user_id=user_id, chat_id=chat_id, username=username,
        announce_id=announce_id, tender_name=None, tender_type=None,
        lot_ids=lot_ids, price=price, parent_bid_id=parent_bid_id,
    )

    state_store = StateStore(
        state_dir=ctx.config.state_dir,
        namespace=str(user_id),
    )

    # Выбор pipeline по типу тендера.
    # pipeline_type задаётся в config.yaml → defaults.pipeline_type:
    #   "repairs"  → flow.py (Конкурс, метод 2, работы — документы 1253/3359/3334/3327/3335/3328)
    #   "catering" → pipeline.py (питание, метод 120) — по умолчанию
    pipeline_type = ctx.config.raw.get("defaults", {}).get("pipeline_type", "catering")

    trace = BidTrace(f"#{announce_id}")
    trace_token = current_trace.set(trace)
    while len(_BID_TRACES) > 20:
        _BID_TRACES.pop(next(iter(_BID_TRACES)))
    _BID_TRACES[bid_row_id] = trace
    token = current_progress.set(pm)
    started_at = time.time()
    try:
        # Authenticate on the shared client before entering the flow.
        probe_t0 = time.monotonic()
        cookie_alive = await _ensure_logged_in(ctx)
        log.info(
            "opt1.cookie_probe",
            alive_after_relogin=cookie_alive,
            elapsed_ms=int((time.monotonic() - probe_t0) * 1000),
        )
        ci = ctx.ci_session()
        if pipeline_type == "repairs":
            # Собираем данные бенефициара из config.yaml → beneficiary
            raw_ben = ctx.config.raw.get("beneficiary", {})
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
            raw_tumarcsp = ctx.config.raw.get("tumarcsp", {})
            defaults = ctx.config.raw.get("defaults", {})
            floor_ratio = float(defaults.get("dumping_floor_ratio", 0.80))

            # If startup's WebSocket is unavailable, flow warms its own
            # provider alongside polling and signing instead of blocking them.
            tumar = getattr(ctx, "tumar", None)
            if getattr(getattr(tumar, "_ws", None), "state", None) != State.OPEN:
                tumar = None

            flow_params = AtagosBidParams(
                announce_id=announce_id,
                tender_lot_ids=lot_ids,
                planned_price_ratio=flow_repairs.REPAIRS_PLANNED_PRICE_RATIO,
                dumping_floor_ratio=floor_ratio,
                dry_run=dry_run,
                signer=ctx.signer,
                tumar=tumar,
                # Cookie already probed/refreshed above — skip another ECP login
                # (~3-5s). Password still needed so doc-pool siblings can relogin.
                skip_initial_login=True,
                auto_login_account_password=ctx.account_password,
                doc_save_mode=defaults.get("doc_save_mode", "serial"),
                tumar_api_key=raw_tumarcsp.get("api_key"),
                tumar_ws_url=raw_tumarcsp.get("ws_url", "wss://127.0.0.1:6127/tumarcsp/"),
                autofill_price_popup=raw_tumarcsp.get("autofill_popup", False),
                popup_coords=raw_tumarcsp.get("popup"),
                **flow_repairs.speed_options(settings_handler.speed_defaults(ctx)),
            )
            # Account-scoped session clones (not tender-scoped).
            fresh_pool = ctx.get_doc_pool_if_fresh(announce_id)
            if fresh_pool:
                log.info("bid.doc_pool.reuse", n=len(fresh_pool), announce=announce_id)

            state = await flow_repairs.submit_bid(
                params=flow_params,
                ci_session=ci,
                company=ctx.config.raw["company"],
                beneficiary=beneficiary,
                state_store=state_store,
                base_url=ctx.config.base_url,
                agreement_cookie=ctx.agreement_cookie(),
                pre_t0_poll=pre_t0_poll,
                poll_interval_s=poll_interval_s,
                proxy=ctx.config.proxy_url,
                # Reuse bot-wide HTTP client (TLS already warm).
                gz_client=ctx.gz_client,
                lp_id_cache=ctx.lp_id_cache,
                reuse_doc_pool=fresh_pool or None,
                tz_sig_cache=ctx.tz_sig_cache,    # контент-кэш подписей
            )
        else:
            params = BidParams(
                announce_id=announce_id,
                tender_lot_ids=lot_ids,
                price=price,
                dry_run=dry_run,
                backend="local",
                signer=ctx.signer,
                auto_login_account_password=None,
            )
            state = await submit_bid(
                params=params,
                ci_session=ci,
                company=ctx.config.raw["company"],
                state_store=state_store,
                base_url=ctx.config.base_url,
                agreement_cookie=ctx.agreement_cookie(),
                pre_t0_poll=pre_t0_poll,
                poll_interval_s=poll_interval_s,
                proxy=ctx.config.proxy_url,
                gz_client=ctx.gz_client,
            )
    except SessionExpired as e:
        app_id = _failed_application_id(state_store, announce_id, started_at)
        ctx.history.finish(bid_row_id, status="failed", app_id=app_id,
                           error=f"session_expired: {_exception_message(e)}")
        return False, app_id, "session_expired", bid_row_id
    except AlreadyPrepared as e:
        ctx.history.finish(
            bid_row_id, status="cancelled",
            app_id=e.app_id,
            error=f"already_prepared: {e}",
        )
        return False, e.app_id, f"already_prepared:{e.app_id}", bid_row_id
    except flow_repairs.DocumentGenerationUnconfirmed as e:
        log.warning("flow.doc.generate_unconfirmed", doc=e.doc_id, app_id=e.app_id)
        ctx.history.finish(bid_row_id, status="failed", app_id=e.app_id, error=str(e))
        ctx.invalidate_doc_pool()
        return False, e.app_id, str(e), bid_row_id
    except Exception as e:
        log.exception("pipeline failed")
        app_id = _failed_application_id(state_store, announce_id, started_at)
        error = _exception_message(e)
        ctx.history.finish(bid_row_id, status="failed", app_id=app_id, error=error[:500])
        ctx.invalidate_doc_pool()  # сброс пула при ошибке
        return False, app_id, error, bid_row_id
    finally:
        if not any(phase in {"public_done", "dry_run_complete", "docs_ready"}
                   for _, phase in trace.marks):
            trace.mark("bid_finished")
        current_progress.reset(token)
        current_trace.reset(trace_token)

    while len(_BID_TIMESTAMPS) > 50:           # never grows without bound
        _BID_TIMESTAMPS.pop(next(iter(_BID_TIMESTAMPS)))
    _BID_TIMESTAMPS[bid_row_id] = dict(getattr(state, "timestamps", None) or {})

    if state.step == "public_ok":
        ctx.history.finish(bid_row_id, status="submitted", app_id=state.application_id)
        # Сохраняем doc_pool для переиспользования в следующей ставке
        if pipeline_type == "repairs" and fresh_pool is not None:
            ctx.save_doc_pool(fresh_pool, announce_id)
            log.info("bid.doc_pool.saved", n=len(fresh_pool), announce=announce_id)
        # Персистим кэши на диск (lp_id + tz_sig)
        ctx.save_caches()
        return True, state.application_id, None, bid_row_id

    if state.step == "dry_run_complete":
        ctx.history.finish(bid_row_id, status="dry_run", app_id=state.application_id)
        if pipeline_type == "repairs" and fresh_pool is not None:
            ctx.save_doc_pool(fresh_pool, announce_id)
        ctx.save_caches()
        return True, state.application_id, "dry_run_complete", bid_row_id

    err = (state.error or "").strip() or f"step={state.step}"
    ctx.history.finish(bid_row_id, status="failed", app_id=state.application_id, error=err[:500])
    # Сохраняем кэши даже при неуспешной ставке — sig_cache и lp_id могли заполниться
    ctx.save_caches()
    return False, state.application_id, err, bid_row_id


async def _kick_off_submit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    replace_msg_id: int | None = None,
):
    """One-shot real submission. No dry-run, no countdown.

    Pasting the URL was the consent — lot was either auto-picked (single)
    or explicitly tapped (multi). Acquire the global bid_lock, then run
    the full pipeline through to public_application.
    """
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    username = update.effective_user.username
    announce_id = context.user_data["announce_id"]
    lot_ids = context.user_data["chosen_lot_ids"]

    if replace_msg_id is not None:
        async def clear_keyboard():
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=replace_msg_id, reply_markup=None,
                )
            except TelegramError as e:
                log.debug("bid.keyboard.clear_failed", error=str(e))

        asyncio.create_task(clear_keyboard())
    return await _do_submit(update, context, chat_id, user_id, username, announce_id, lot_ids)


async def _do_submit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    username: Optional[str],
    announce_id: str,
    lot_ids: list[int],
):
    ctx = _ctx(context)
    t_bid0 = time.monotonic()
    waiting_line = "👀 Жду открытия тендера…"
    queued = ctx.bid_lock.locked() or ctx.queue_waiters > 0
    initial_line = waiting_line
    if queued:
        ctx.queue_waiters += 1
        current = f"#{ctx.current_bid.announce_id}" if ctx.current_bid else ""
        initial_line = BUSY_QUEUE.format(position=ctx.queue_waiters + 1, current=current)
    pm = ProgressMessage.start(
        context.bot, chat_id, initial_line,
        header=f"🚀 <b>Подаю заявку #{announce_id}</b>",
    )
    try:
        async with ctx.bid_lock:
            if queued:
                ctx.queue_waiters -= 1
                queued = False
                pm.update(BID_QUEUE_YOUR_TURN)
                pm.update(waiting_line)
            ctx.current_bid = BidQueueItem(
                user_id=user_id, chat_id=chat_id, announce_id=announce_id,
                started_at=time.time(),
            )
            try:
                _dry = bool((ctx.bid_settings or {}).get("dry_run", True))
                ok, app_id, err, bid_row_id = await _run_pipeline(
                    ctx, user_id, chat_id, username, pm,
                    announce_id=announce_id, lot_ids=lot_ids, price=BID_PRICE,
                    dry_run=_dry,
                )
            finally:
                ctx.current_bid = None
    except BaseException:
        pm.cancel()
        raise
    finally:
        if queued:
            ctx.queue_waiters -= 1

    elapsed_val = round(time.monotonic() - t_bid0, 1)
    elapsed_str = _timing_text(bid_row_id, elapsed_val, dry=(err == "dry_run_complete"), failed=not ok)
    _queue_timing_report(context, ctx, chat_id, bid_row_id, announce_id, app_id,
                         ok=ok, dry=err == "dry_run_complete")

    if not ok:
        if err and err.startswith("already_prepared:"):
            already_app_id = err.split(":", 1)[1] or "?"
            cabinet_url = f"https://v3bl.goszakup.gov.kz/ru/cabinet/preview/{announce_id}/{already_app_id}"
            await pm.finish(
                f"📋 По этому тендеру у тебя <b>уже есть готовый или поданный</b> черновик "
                f"(<code>{escape(already_app_id)}</code>). Открой кабинет.\n{elapsed_str}"
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text="Открыть в кабинете:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔗 Кабинет goszakup", url=cabinet_url)],
                ]),
            )
            context.user_data.clear()
            return ConversationHandler.END

        await pm.finish(
            f"❌ Ставка не прошла: <code>{escape((err or '')[:200])}</code>\n"
            f"{elapsed_str}"
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text="Что дальше?",
            reply_markup=_failure_recovery_keyboard(),
        )
        context.user_data.clear()
        return ConversationHandler.END

    price_text = (
        f"Цена за единицу: {flow_repairs.REPAIRS_PLANNED_PRICE_RATIO:.0%} от плановой\n"
        if ctx.config.raw.get("defaults", {}).get("pipeline_type") == "repairs" else ""
    )
    if err == "dry_run_complete":
        await pm.finish(
            f"✅ <b>Dry-run OK</b> — собрана и подписана, <b>НЕ подана</b>\n"
            f"app <code>{app_id}</code>\n"
            f"{price_text}"
            f"{elapsed_str}"
        )
    else:
        await pm.finish(
            f"✅ <b>Заявка успешно подана!</b>\n"
            f"app <code>{app_id}</code>\n"
            f"{price_text}"
            f"{elapsed_str}"
        )
    await context.bot.send_message(
        chat_id=chat_id,
        text="Заявка в кабинете:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "🔗 Открыть в кабинете",
                url=f"https://v3bl.goszakup.gov.kz/ru/cabinet/preview/{announce_id}/{app_id}",
            )],
        ]),
    )
    context.user_data.clear()
    return ConversationHandler.END


@whitelist_only
async def bid_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q is not None:
        await q.answer()
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except BadRequest:
            pass
    await update.effective_chat.send_message(CANCELLED)
    bid_id = context.user_data.get("dry_bid_id")
    if bid_id:
        _ctx(context).history.finish(bid_id, status="cancelled")
    context.user_data.clear()
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ConversationHandler factory
# ---------------------------------------------------------------------------

@whitelist_only
async def timing_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/timing — re-send the request timeline of the last bid from this chat."""
    chat_id = update.effective_chat.id
    last = _LAST_REPORT.get(chat_id)
    if not last:
        await update.effective_chat.send_message(
            "Хронологии пока нет — сначала подайте заявку (можно в dry-run).")
        return
    name, text = last
    await context.bot.send_document(
        chat_id=chat_id,
        document=InputFile(io.BytesIO(text.encode("utf-8")), filename=name),
        caption="⏱ Хронология запросов последней подачи",
    )


def build_conversation_handler() -> ConversationHandler:
    # Entry regex matches: pure 6-12 digit number, any text with 6-12 digits, OR any goszakup URL with
    # /announce/index/<digits>.
    entry_regex = re.compile(
        r"\b\d{6,12}\b|/announce/index/\d+",
        re.IGNORECASE,
    )
    return ConversationHandler(
        entry_points=[
            CommandHandler("bid", bid_cmd),
            CallbackQueryHandler(bid_pick_cb, pattern=r"^bid_pick:"),
            CallbackQueryHandler(rebid_cb, pattern=r"^rebid:"),
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                text_entry,
            ),
        ],
        states={
            PICK_LOT: [
                CallbackQueryHandler(lot_pick_cb, pattern=r"^lot_pick:"),
                CallbackQueryHandler(bid_cancel_cb, pattern=r"^bid_cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", bid_cancel_cb),
        ],
        per_message=False,
        per_user=True,
        allow_reentry=True,
        name="bid_conversation",
    )
