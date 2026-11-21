"""Runtime bid settings + the pre-submit confirmation gate.

`/config` shows and edits a handful of per-bid options at runtime (seeded from
config.yaml `defaults` at boot; changes live for the process — config.yaml holds
the persistent defaults). The most important is the pre-submit approval gate:
when `confirm_before_submit` is on, `submit_bid` pauses right before the
irreversible ajax_public_application and asks in Telegram which guarantee type +
price is about to be sent, publishing only on «Подать».
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..auth import whitelist_only

log = logging.getLogger("settings")

# Human labels for the обеспечение заявки typeDoc codes we know. 3 is the only
# one the bot fully drives (cash deposit); others are shown by code until their
# upload flow exists.
GUARANTEE_TYPES = {
    3: "Гарантийный денежный взнос (электронный кошелёк)",
}

# key -> (type, human description). These are the runtime-editable bid options.
SETTABLE: dict[str, tuple[str, str]] = {
    "guarantee_type": ("int", "Тип обеспечения заявки (typeDoc; 3 = денежный взнос с кошелька)"),
    "confirm_before_submit": ("bool", "Спрашивать подтверждение в Telegram перед подачей"),
    "dumping_floor_ratio": ("float", "Доля для расчёта минимума, если портал его не передал (например 0.80)"),
    "dry_run": ("bool", "Тестовый режим: собрать заявку, но НЕ подавать"),
}

# Defaults used if config.yaml `defaults` omits a key.
# confirm_before_submit is OFF by default: at T0 every second counts, so the
# publish must not wait on a human tap. Turn on with /config when desired.
DEFAULTS: dict = {
    "guarantee_type": 3,
    "confirm_before_submit": False,
    "dumping_floor_ratio": 0.80,
    # Safe default: never publish unless config.yaml / /config explicitly allows.
    "dry_run": True,
}


def seed_settings(raw_config: dict) -> dict:
    """Build the initial bid_settings dict from config.yaml `defaults`."""
    d = (raw_config or {}).get("defaults", {}) or {}
    out = dict(DEFAULTS)
    for k in SETTABLE:
        if k in d and d[k] is not None:
            out[k] = d[k]
    # normalise types
    out["guarantee_type"] = int(out["guarantee_type"])
    out["confirm_before_submit"] = bool(out["confirm_before_submit"])
    out["dumping_floor_ratio"] = float(out["dumping_floor_ratio"])
    out["dry_run"] = bool(out["dry_run"])
    return out


def _coerce(typ: str, raw: str):
    if typ == "int":
        return int(raw)
    if typ == "float":
        return float(raw.replace(",", "."))
    if typ == "bool":
        return raw.strip().lower() in ("1", "true", "on", "yes", "да", "вкл")
    return raw


def _fmt_settings(bid_settings: dict) -> str:
    lines = ["⚙️ <b>Настройки ставки</b>", ""]
    for k, (_typ, desc) in SETTABLE.items():
        val = bid_settings.get(k)
        extra = ""
        if k == "guarantee_type":
            extra = f" — {GUARANTEE_TYPES.get(int(val), 'нестандартный тип, upload-флоу нет')}"
        lines.append(f"<code>{k}</code> = <b>{val}</b>{extra}\n   <i>{desc}</i>")
    lines.append("")
    lines.append("Изменить: <code>/config &lt;ключ&gt; &lt;значение&gt;</code>")
    lines.append("Пример: <code>/config confirm_before_submit off</code>")
    return "\n".join(lines)


@whitelist_only
async def config_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = context.application.bot_data["ctx"]
    if not ctx.bid_settings:
        ctx.bid_settings = seed_settings(ctx.config.raw)
    args = context.args or []

    if not args:
        await update.message.reply_text(_fmt_settings(ctx.bid_settings), parse_mode="HTML")
        return

    key = args[0]
    if key not in SETTABLE:
        await update.message.reply_text(
            f"Неизвестная опция «{key}».\nДоступно: {', '.join(SETTABLE)}")
        return
    if len(args) < 2:
        await update.message.reply_text(f"Укажи значение: <code>/config {key} &lt;значение&gt;</code>",
                                        parse_mode="HTML")
        return
    typ = SETTABLE[key][0]
    try:
        val = _coerce(typ, args[1])
    except ValueError:
        await update.message.reply_text(f"Неверное значение для «{key}» (ожидается {typ}).")
        return
    ctx.bid_settings[key] = val
    note = ""
    if key == "guarantee_type" and int(val) != 3:
        note = ("\n⚠️ Нестандартный тип: бот отправит только выбор typeDoc, "
                "но загрузку файла (напр. банковской гарантии) он пока не делает.")
    await update.message.reply_text(f"✅ <code>{key}</code> = <b>{val}</b>{note}", parse_mode="HTML")


@whitelist_only
async def confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resolve a pending pre-submit confirmation (Подать / Отмена)."""
    q = update.callback_query
    await q.answer()
    ctx = context.application.bot_data["ctx"]
    action, _, token = (q.data or "").partition(":")
    approved = action == "submitok"
    fut = ctx.pending_confirms.get(token)
    if fut is not None and not fut.done():
        fut.set_result(approved)
    try:
        await q.edit_message_text(
            "✅ Подтверждено — подаю заявку…" if approved
            else "❌ Отменено — заявка НЕ подана (черновик готов).")
    except Exception:  # noqa: BLE001 — cosmetic edit, never fatal
        pass


def make_confirm_gate(ctx, bot, chat_id: int, *, timeout_s: float = 180.0):
    """Build an `on_before_submit(summary)->bool` gate that asks in Telegram.

    Sends the bid summary with Подать/Отмена buttons and blocks until the user
    taps one (or `timeout_s` elapses → treated as decline, so nothing publishes
    without an explicit yes)."""

    async def gate(summary: dict) -> bool:
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        token = uuid.uuid4().hex[:8]
        ctx.pending_confirms[token] = fut

        gt = int(summary.get("guarantee_type", 3))
        gt_name = GUARANTEE_TYPES.get(gt, f"нестандартный (typeDoc={gt})")
        prices = summary.get("prices") or {}
        price_lines = "\n".join(f"  • лот {lp}: <b>{p:,.2f}</b>" for lp, p in prices.items())
        text = (
            f"🧾 <b>Подтверждение подачи #{summary['announce_id']}</b>\n\n"
            f"Обеспечение заявки: <b>{gt_name}</b>\n"
            f"Цена:\n{price_lines}\n"
            f"Итого: <b>{summary.get('total', 0):,.2f}</b> тг\n\n"
            f"Подать заявку?"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Подать", callback_data=f"submitok:{token}"),
            InlineKeyboardButton("❌ Отмена", callback_data=f"submitno:{token}"),
        ]])
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=kb)
        try:
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(f"⌛ #{summary['announce_id']} — нет ответа за {int(timeout_s)}с. "
                          f"Подача отменена (черновик готов, можно подать вручную)."))
            except Exception:  # noqa: BLE001
                pass
            return False
        finally:
            ctx.pending_confirms.pop(token, None)

    return gate
