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
import logging

import structlog
import time
from typing import Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
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
from ..progress import ProgressMessage, current_progress
from ..search import probe_cookie
from ..texts import BID_QUEUE_YOUR_TURN, BUSY_QUEUE, CANCELLED
from ... import secrets as gz_secrets
from ...client import AlreadyPrepared, GoszakupClient, SessionExpired
from ...pipeline import BidParams, finalize_application, submit_bid
from ...state import StateStore


COOKIE_PROBE_CACHE_S = 30.0


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
        log.warning("opt1.bot_relogin.failed: %s", e)
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
    # Pure-digit input.
    if raw.isdigit() and 6 <= len(raw) <= 12:
        return raw
    # First standalone digit chunk (so "16941325-1" returns "16941325").
    m2 = re.match(r"^\s*(\d{6,12})(?:[\s\-]|$)", raw)
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

    # Step 1: cookie probe + auto-login if dead.
    if not await _is_cookie_fresh(ctx):
        await msg.edit_text(
            f"🔐 Кука протухла, логинюсь через ЭЦП… "
            f"(<code>{announce_id}</code>)",
            parse_mode="HTML",
        )
        ok = await _ensure_logged_in(ctx)
        if not ok:
            await msg.edit_text(
                "❌ Логин не удался. Проверь пароли в <code>.env</code>.",
                parse_mode="HTML",
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
        return await _kick_off_submit(update, context, replace_msg_id=msg.message_id)

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
            "Просто пришли announce_id числом, например <code>17042109</code>.\n"
            "Или ссылку: <code>v3bl.goszakup.gov.kz/ru/announce/index/17042109</code>.\n"
            "Или <code>/bid 17042109</code> — то же самое.",
            parse_mode="HTML",
        )
        return ConversationHandler.END
    announce_id = _extract_announce_id(" ".join(context.args))
    if not announce_id:
        await update.effective_chat.send_message(
            "Не нашёл announce_id в твоём сообщении. Пришли число или полную ссылку.",
        )
        return ConversationHandler.END
    return await _enter_bid_flow(update, context, announce_id=announce_id)


@whitelist_only
async def text_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Plain text (число or goszakup URL) is the primary mobile shortcut."""
    announce_id = _extract_announce_id(update.message.text or "")
    if announce_id:
        return await _enter_bid_flow(update, context, announce_id=announce_id)
    await update.message.reply_text(
        "Не понял. Пришли announce_id числом (<code>17042109</code>) или "
        "ссылку goszakup на тендер.\n/help — все команды.",
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
    poll_interval_s: float = 0.2,
) -> tuple[bool, Optional[str], Optional[str], int]:
    """Run one pipeline pass. Returns (ok, app_id, error, bid_row_id)."""
    bid_row_id = ctx.history.insert_pending(
        user_id=user_id, chat_id=chat_id, username=username,
        announce_id=announce_id, tender_name=None, tender_type=None,
        lot_ids=lot_ids, price=price, parent_bid_id=parent_bid_id,
    )

    state_store = StateStore(
        state_dir=ctx.config.state_dir,
        namespace=str(user_id),
    )

    # Opts #1 + #4: probe the shared client's cookie; if dead, do the
    # relogin AT THE BOT LAYER (mutating the shared client's jar). After
    # that, submit_bid runs with the shared client and never auto-logs.
    probe_t0 = time.monotonic()
    cookie_alive = await _ensure_logged_in(ctx)
    log.info(
        "opt1.cookie_probe",
        alive_after_relogin=cookie_alive,
        elapsed_ms=int((time.monotonic() - probe_t0) * 1000),
    )

    ci = ctx.ci_session()       # in-memory cache, no Keychain hit

    params = BidParams(
        announce_id=announce_id,
        tender_lot_ids=lot_ids,
        price=price,
        dry_run=dry_run,
        backend="local",
        signer=ctx.signer,
        # No ecp_password — the shared signer is already loaded.
        # No auto-login here — the bot already brought the shared client
        # to a logged-in state above. Avoids H1 cookie-jar race.
        auto_login_account_password=None,
    )

    token = current_progress.set(pm)
    try:
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
        ctx.history.finish(bid_row_id, status="failed", error=f"session_expired: {e}")
        return False, None, "session_expired", bid_row_id
    except AlreadyPrepared as e:
        ctx.history.finish(
            bid_row_id, status="cancelled",
            app_id=e.app_id,
            error=f"already_prepared: {e}",
        )
        return False, e.app_id, f"already_prepared:{e.app_id}", bid_row_id
    except Exception as e:
        log.exception("pipeline failed")
        ctx.history.finish(bid_row_id, status="failed", error=str(e)[:500])
        return False, None, str(e), bid_row_id
    finally:
        current_progress.reset(token)

    if state.step == "public_ok":
        ctx.history.finish(bid_row_id, status="submitted", app_id=state.application_id)
        return True, state.application_id, None, bid_row_id

    err = state.error or f"step={state.step}"
    ctx.history.finish(bid_row_id, status="failed", app_id=state.application_id, error=err[:500])
    return False, state.application_id, err, bid_row_id


async def _kick_off_submit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    replace_msg_id: int,
):
    """One-shot real submission. No dry-run, no countdown.

    Pasting the URL was the consent — lot was either auto-picked (single)
    or explicitly tapped (multi). Acquire the global bid_lock, then run
    the full pipeline through to public_application.
    """
    ctx = _ctx(context)
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    username = update.effective_user.username
    announce_id = context.user_data["announce_id"]
    lot_ids = context.user_data["chosen_lot_ids"]

    # Drop any keyboard left on the lot-pick message.
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=replace_msg_id, reply_markup=None,
        )
    except BadRequest:
        pass

    # Queue handling: if another bid is mid-flight, tell user their position.
    if ctx.bid_lock.locked():
        ctx.queue_waiters += 1
        try:
            current = f"#{ctx.current_bid.announce_id}" if ctx.current_bid else ""
            await context.bot.send_message(
                chat_id=chat_id,
                text=BUSY_QUEUE.format(position=ctx.queue_waiters + 1, current=current),
            )
            async with ctx.bid_lock:
                await context.bot.send_message(chat_id=chat_id, text=BID_QUEUE_YOUR_TURN)
                return await _do_submit(update, context, chat_id, user_id, username, announce_id, lot_ids)
        finally:
            ctx.queue_waiters -= 1
    else:
        async with ctx.bid_lock:
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
    ctx.current_bid = BidQueueItem(
        user_id=user_id, chat_id=chat_id, announce_id=announce_id,
        started_at=time.time(),
    )
    try:
        pm = await ProgressMessage.start(
            context.bot, chat_id, "👀 Жду открытия тендера…",
            header=f"🚀 <b>Подаю заявку #{announce_id}</b>",
        )
        ok, app_id, err, _ = await _run_pipeline(
            ctx, user_id, chat_id, username, pm,
            announce_id=announce_id, lot_ids=lot_ids, price=BID_PRICE,
            dry_run=False,
        )
    finally:
        ctx.current_bid = None

    if not ok:
        if err and err.startswith("already_prepared:"):
            already_app_id = err.split(":", 1)[1] or "?"
            cabinet_url = f"https://v3bl.goszakup.gov.kz/ru/cabinet/preview/{announce_id}/{already_app_id}"
            await pm.finish(
                f"📋 По этому тендеру у тебя <b>уже есть готовый или поданный</b> черновик "
                f"(<code>{already_app_id}</code>). Открой кабинет."
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

        await pm.finish(f"❌ Ставка не прошла: <code>{(err or '')[:200]}</code>")
        await context.bot.send_message(
            chat_id=chat_id,
            text="Что дальше?",
            reply_markup=_failure_recovery_keyboard(),
        )
        context.user_data.clear()
        return ConversationHandler.END

    await pm.finish(
        f"✅ <b>Заявка подана</b>\n"
        f"app <code>{app_id}</code>"
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

def build_conversation_handler() -> ConversationHandler:
    # Entry regex matches: pure 6-12 digit number, OR any goszakup URL with
    # /announce/index/<digits>. Both routed through `text_entry` which
    # delegates to _extract_announce_id().
    entry_regex = re.compile(
        r"^\s*\d{6,12}\s*$|/announce/index/\d+",
        re.IGNORECASE,
    )
    return ConversationHandler(
        entry_points=[
            CommandHandler("bid", bid_cmd),
            CallbackQueryHandler(bid_pick_cb, pattern=r"^bid_pick:"),
            CallbackQueryHandler(rebid_cb, pattern=r"^rebid:"),
            MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.Regex(entry_regex),
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
        # No re-entry: a new announce_id while a bid is in progress is
        # ignored. User must /cancel first. Keeps state machine sane.
        allow_reentry=False,
        name="bid_conversation",
    )
