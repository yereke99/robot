"""atagos multi-tender Telegram bot — `python -m goszakup.bot.atagos_bot`.

Built for the operator's core need: hold **many tenders at once**. Each tender
spawns an independent asyncio task that waits for its tender to open (pre-T0
poll), then runs the full gamma submission — without blocking other tenders.
A client may have 5-10 tenders opening within seconds of each other; the bot
watches and fills them all concurrently.

Conversational flow (mobile-friendly):
  send a tender number / URL (or /bid <id>) → bot fetches its lots → you tap the
  lots you want → ▶️ Начать → bot watches it. While waiting it pings you that the
  tender is still on hold; the moment it opens it says so and starts the
  submission; when done it confirms "заявка по тендеру _ и лотам _ подана".
  Several tenders can be queued this way and they run fully in parallel.

Power-path: `/bid <id> <lot1,lot2> [price]` skips the picker and watches at once.

Shared across tasks: one long-lived KalkanSigner (signing is ~50ms, serialized
by its own lock) and the process-global price-popup gate (only one popup driven
at a time, see tumar.py). Per task: its own GoszakupClient + its own TumarCSP, so
HTTP and the price seal run truly in parallel.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import structlog
import yaml
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

from .. import secrets as gz_secrets
from ..client import GoszakupClient, SessionExpired
from ..flow import AtagosBidParams, BeneficiaryInfo, submit_bid
from ..local_signer import KalkanSigner
from ..state import StateStore
from ..tumar import TumarCSP
from ..timing import poll_interval_s
from .instance_lock import BotAlreadyRunning, bot_instance

log = structlog.get_logger("bot")

STATUS_EMOJI = {
    "waiting": "⏳", "running": "⚙️", "dry_run_complete": "🧪",
    "public_ok": "✅", "public_error": "🔴", "failed": "🔴", "cancelled": "🚫",
}

_ANNOUNCE_URL_RE = re.compile(r"/announce/index/(\d+)")


def _extract_announce_id(raw: str) -> Optional[str]:
    """Pull an announce_id from a number, a goszakup URL, or "<id>-1"."""
    raw = (raw or "").strip()
    if not raw:
        return None
    m = _ANNOUNCE_URL_RE.search(raw)
    if m:
        return m.group(1)
    if raw.isdigit() and 6 <= len(raw) <= 12:
        return raw
    m2 = re.match(r"^\s*(\d{6,12})(?:[\s\-]|$)", raw)
    return m2.group(1) if m2 else None


def _fmt_lots(lot_ids: list[int]) -> str:
    return ", ".join(str(x) for x in lot_ids)


def _lot_sum_disp(raw) -> str:
    try:
        v = float(str(raw).replace(" ", "").replace("\xa0", "").replace(",", "."))
        return f"{v / 1_000_000:.2f}M ₸"
    except Exception:
        return str(raw)


@dataclass
class WatchedTender:
    announce_id: str
    lot_ids: list[int]
    price: Optional[float]
    dry_run: bool
    chat_id: int
    status: str = "waiting"
    detail: str = ""
    app_id: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    task: Optional[asyncio.Task] = None
    notify_task: Optional[asyncio.Task] = None


class TenderManager:
    """Holds the concurrently-watched tenders for the whole bot process."""

    def __init__(self, cfg: dict, signer: KalkanSigner):
        self.cfg = cfg
        self.signer = signer
        self.tenders: dict[str, WatchedTender] = {}
        self.store = StateStore(Path(cfg["defaults"]["state_dir"]).expanduser())
        # Conversational lot-picking state, keyed "<chat_id>:<announce_id>".
        self.pending: dict[str, dict] = {}
        self.notify_interval_s = float(cfg["defaults"].get("waiting_notify_interval_s", 60))

    def _beneficiary(self) -> BeneficiaryInfo:
        b = self.cfg["beneficiary"]
        return BeneficiaryInfo(
            name=b["name"], citizenship=str(b["citizenship"]),
            res_country=str(b["res_country"]), doc_number=str(b["doc_number"]),
            doc_date=b["doc_date"], options=b["options"], id_pdf_path=b["id_pdf_path"],
            id_file_id=b.get("id_file_id") or None,
        )

    # ---- lot discovery (public announce page, no login needed) ----
    async def discover_lots(self, announce_id: str) -> list[dict]:
        ci = gz_secrets.get(self.cfg["goszakup"]["ci_session_service"], "ci_session")
        async with GoszakupClient(
            ci_session=ci or None, base_url=self.cfg["goszakup"]["base_url"],
        ) as gz:
            try:
                return await gz.discover_announce_lots(announce_id)
            except SessionExpired:
                acc_pw = gz_secrets.get(self.cfg["account"]["password_keyring_service"],
                                        self.cfg["account"]["password_keyring_user"])
                if not acc_pw:
                    raise
                await gz.login(account_password=acc_pw, kalkan_signer=self.signer)
                return await gz.discover_announce_lots(announce_id)

    # ---- start / stop watching ----
    async def start_watch(self, app, *, announce_id, lot_ids, price, dry_run, chat_id) -> bool:
        existing = self.tenders.get(announce_id)
        if existing and existing.status in ("waiting", "running"):
            return False
        wt = WatchedTender(announce_id=announce_id, lot_ids=lot_ids, price=price,
                           dry_run=dry_run, chat_id=chat_id)
        self.tenders[announce_id] = wt
        wt.task = asyncio.create_task(self._run(app, wt))
        return True

    def active_count(self) -> int:
        return sum(1 for t in self.tenders.values() if t.status in ("waiting", "running"))

    def cancel(self, announce_id: str) -> bool:
        wt = self.tenders.get(announce_id)
        if not wt or not wt.task or wt.task.done():
            return False
        if wt.notify_task:
            wt.notify_task.cancel()
        wt.task.cancel()
        wt.status = "cancelled"
        return True

    async def _heartbeat(self, app, wt: WatchedTender) -> None:
        """Ping the chat that the tender is still on hold, until it opens."""
        try:
            while wt.status == "waiting":
                await asyncio.sleep(self.notify_interval_s)
                if wt.status != "waiting":
                    break
                mins = int((time.time() - wt.started_at) / 60)
                await _send(app, wt.chat_id,
                            f"⏳ Тендер <b>{wt.announce_id}</b> ещё не открылся — "
                            f"жду открытия… ({mins} мин)")
        except asyncio.CancelledError:
            pass

    async def _run(self, app, wt: WatchedTender) -> None:
        cfg = self.cfg
        ci = gz_secrets.get(cfg["goszakup"]["ci_session_service"], "ci_session")
        acc_pw = gz_secrets.get(cfg["account"]["password_keyring_service"],
                                cfg["account"]["password_keyring_user"])
        opened = {"done": False}

        async def _on_open():
            # Tender opened: stop the waiting pings and announce the start.
            if opened["done"]:
                return
            opened["done"] = True
            wt.status = "running"
            if wt.notify_task:
                wt.notify_task.cancel()
            await _send(app, wt.chat_id,
                        f"🟢 Тендер <b>{wt.announce_id}</b> открылся — "
                        f"начинаю подачу заявки (лоты {_fmt_lots(wt.lot_ids)})…")

        try:
            await _send(app, wt.chat_id,
                        f"⏳ Жду открытия тендера <b>{wt.announce_id}</b>, "
                        f"лоты {_fmt_lots(wt.lot_ids)}…")
            wt.notify_task = asyncio.create_task(self._heartbeat(app, wt))
            params = AtagosBidParams(
                announce_id=wt.announce_id,
                tender_lot_ids=wt.lot_ids,
                price=wt.price,
                dumping_floor_ratio=float(cfg["defaults"].get("dumping_floor_ratio", 0.80)),
                dry_run=wt.dry_run,
                signer=self.signer,                       # shared, serialized
                tumar_api_key=cfg["tumarcsp"]["api_key"], # each run opens its own WS
                tumar_ws_url=cfg["tumarcsp"]["ws_url"],
                autofill_price_popup=bool(cfg["tumarcsp"].get("autofill_popup", False)),
                auto_login_account_password=acc_pw,       # own client per run -> safe
                # "serial" (safe, default) | "parallel_sessions" (experimental B —
                # each doc saved on its own login). Roll back by flipping to serial.
                doc_save_mode=cfg["defaults"].get("doc_save_mode", "serial"),
            )
            state = await submit_bid(
                params=params,
                ci_session=ci or "",
                company=cfg["company"],
                beneficiary=self._beneficiary(),
                state_store=self.store,
                base_url=cfg["goszakup"]["base_url"],
                pre_t0_poll=True,
                poll_interval_s=poll_interval_s(cfg),
                on_open=_on_open,
            )
            wt.status = state.step
            wt.app_id = state.application_id
            wt.detail = state.error or ""
            await self._send_result(app, wt, state)
        except asyncio.CancelledError:
            wt.status = "cancelled"
            await _send(app, wt.chat_id, f"🚫 Снял с наблюдения тендер {wt.announce_id}")
            raise
        except Exception as e:  # noqa: BLE001
            wt.status = "failed"
            wt.detail = str(e)
            log.exception("bot.tender.failed", announce=wt.announce_id)
            await _send(app, wt.chat_id,
                        f"🔴 Тендер <b>{wt.announce_id}</b> упал:\n"
                        f"<code>{html.escape(str(e)[:400])}</code>")
        finally:
            if wt.notify_task:
                wt.notify_task.cancel()

    async def _send_result(self, app, wt: WatchedTender, state) -> None:
        lots = _fmt_lots(wt.lot_ids)
        preview = ""
        if state.application_id:
            preview = (f"\n🔗 https://v3bl.goszakup.gov.kz/ru/application/preview/"
                       f"{wt.announce_id}/{state.application_id}")
        if state.step == "public_ok":
            await _send(app, wt.chat_id,
                        f"✅ Заявка по тендеру <b>{wt.announce_id}</b> и лотам "
                        f"<b>{lots}</b> подана.{preview}")
        elif state.step == "dry_run_complete":
            await _send(app, wt.chat_id,
                        f"🧪 Сухой прогон по тендеру <b>{wt.announce_id}</b>, лоты "
                        f"<b>{lots}</b> прошёл до конца (заявка {state.application_id}). "
                        f"Финальная подача пропущена.{preview}")
        else:
            emoji = STATUS_EMOJI.get(state.step, "ℹ️")
            msg = (f"{emoji} Тендер <b>{wt.announce_id}</b>, лоты {lots}: "
                   f"<code>{state.step}</code>{preview}")
            if state.error:
                msg += f"\n⚠️ {html.escape(state.error[:300])}"
            await _send(app, wt.chat_id, msg)


# ---------------- helpers ----------------

async def _send(app, chat_id: int, text: str) -> None:
    try:
        await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                                   disable_web_page_preview=True)
    except Exception as e:  # noqa: BLE001
        log.warning("bot.send_failed", err=str(e))


def _allowed(update: Update, cfg: dict) -> bool:
    u = update.effective_user
    return bool(u and u.id in cfg["telegram"]["allowed_user_ids"])


def _parse_bid_args(args: list[str]) -> tuple[str, list[int], Optional[float]]:
    if len(args) < 2:
        raise ValueError("Формат: /bid <announce_id> <лоты через запятую> [цена]")
    announce = _extract_announce_id(args[0]) or args[0].strip()
    lots = [int(x) for x in args[1].replace(" ", "").split(",") if x]
    price = float(args[2]) if len(args) > 2 else None
    return announce, lots, price


# ---------------- lot picker (inline keyboard) ----------------

def _picker_keyboard(pend: dict) -> InlineKeyboardMarkup:
    ann = pend["announce_id"]
    rows = []
    for l in pend["lots"]:
        lot_id = int(l["lot_id"])
        mark = "☑️" if lot_id in pend["selected"] else "☐"
        rows.append([InlineKeyboardButton(
            f"{mark} Лот {l['lot_number']} · {_lot_sum_disp(l.get('sum'))}",
            callback_data=f"ls:{ann}:{lot_id}",
        )])
    n = len(pend["selected"])
    rows.append([
        InlineKeyboardButton(f"▶️ Начать ({n})", callback_data=f"lg:{ann}"),
        InlineKeyboardButton("❌ Отмена", callback_data=f"lc:{ann}"),
    ])
    return InlineKeyboardMarkup(rows)


async def _present_lots(update, ctx, *, announce_id: str, dry_run: bool):
    cfg = ctx.application.bot_data["cfg"]
    mgr: TenderManager = ctx.application.bot_data["mgr"]
    chat_id = update.effective_chat.id

    existing = mgr.tenders.get(announce_id)
    if existing and existing.status in ("waiting", "running"):
        await update.effective_chat.send_message(
            f"Тендер {announce_id} уже в работе ({existing.status}). "
            f"Лоты: {_fmt_lots(existing.lot_ids)}.")
        return

    msg = await update.effective_chat.send_message(
        f"🔍 Получаю лоты тендера <code>{announce_id}</code>…", parse_mode="HTML")
    try:
        lots = await mgr.discover_lots(announce_id)
    except Exception as e:  # noqa: BLE001
        await msg.edit_text(f"❌ Не получилось получить лоты: <code>{html.escape(str(e)[:200])}</code>",
                            parse_mode="HTML")
        return
    if not lots:
        await msg.edit_text(
            f"❌ В тендере <code>{announce_id}</code> нет лотов "
            f"(или он не публичный / неверный id).", parse_mode="HTML")
        return

    # Single lot -> preselect it so one tap on ▶️ Начать is the consent.
    selected = {int(lots[0]["lot_id"])} if len(lots) == 1 else set()
    pend = {"announce_id": announce_id, "lots": lots, "selected": selected,
            "dry_run": dry_run, "msg_id": msg.message_id}
    mgr.pending[f"{chat_id}:{announce_id}"] = pend
    head = "🧪 СУХОЙ ПРОГОН\n" if dry_run else ""
    await msg.edit_text(
        f"{head}🎯 Тендер <code>{announce_id}</code> — {len(lots)} лот(ов). "
        f"Отметь нужные и жми ▶️ Начать.\nЦена — минимум (демпинг-порог).",
        parse_mode="HTML", reply_markup=_picker_keyboard(pend))


# ---------------- handlers ----------------

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg = ctx.application.bot_data["cfg"]
    if not _allowed(update, cfg):
        await update.message.reply_text(f"Доступ запрещён. Ваш id: {update.effective_user.id}")
        return
    await update.message.reply_text(
        "🤖 GoszakupBot — бот ставок на госзакуп.\n\n"
        "Просто пришли номер тендера (или ссылку) — покажу лоты, отметишь нужные, "
        "и я буду ждать открытия и подам.\n\n"
        "/bid <id> [лоты] — то же; с лотами через запятую запускает сразу\n"
        "/drybid <id> — сухой прогон (без финальной подачи)\n"
        "/watching — что сейчас в работе\n"
        "/cancel <id> — снять с наблюдения\n"
        "/status — здоровье подписчика/TumarCSP/сессии\n\n"
        "Можно держать несколько тендеров сразу — они ждут и подаются параллельно.")


async def _bid_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *, dry: bool):
    cfg = ctx.application.bot_data["cfg"]
    mgr: TenderManager = ctx.application.bot_data["mgr"]
    if not _allowed(update, cfg):
        return
    # Power-path: /bid <id> <lots> [price] -> start immediately, skip the picker.
    if len(ctx.args) >= 2:
        try:
            announce, lots, price = _parse_bid_args(ctx.args)
        except ValueError as e:
            await update.message.reply_text(str(e))
            return
        ok = await mgr.start_watch(ctx.application, announce_id=announce, lot_ids=lots,
                                   price=price, dry_run=dry, chat_id=update.effective_chat.id)
        if not ok:
            await update.message.reply_text(f"Тендер {announce} уже в работе.")
        else:
            await update.message.reply_text(
                f"{'🧪 DRY ' if dry else '🎯 '}Принял тендер <b>{announce}</b>, "
                f"лоты {_fmt_lots(lots)}. Жду открытия. "
                f"В работе тендеров: {mgr.active_count()}.\n"
                f"Можешь прислать ещё номер — добавлю параллельно.",
                parse_mode="HTML")
        return
    # Conversational: need an id, then show the lot picker.
    if not ctx.args:
        await update.message.reply_text("Пришли номер тендера: /bid <id> (или просто число / ссылку).")
        return
    announce = _extract_announce_id(" ".join(ctx.args))
    if not announce:
        await update.message.reply_text("Не нашёл номер тендера. Пришли число или ссылку goszakup.")
        return
    await _present_lots(update, ctx, announce_id=announce, dry_run=dry)


async def bid_cmd(update, ctx):
    await _bid_cmd(update, ctx, dry=False)


async def drybid_cmd(update, ctx):
    await _bid_cmd(update, ctx, dry=True)


async def text_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Plain number / goszakup URL — the primary mobile shortcut -> lot picker."""
    cfg = ctx.application.bot_data["cfg"]
    if not _allowed(update, cfg):
        return
    announce = _extract_announce_id(update.message.text or "")
    if not announce:
        await update.message.reply_text(
            "Не понял. Пришли номер тендера (например <code>17046469</code>) или ссылку.",
            parse_mode="HTML")
        return
    # Default safe: dry-run unless config explicitly says otherwise.
    cfg_dry = bool((cfg.get("defaults") or {}).get("dry_run", True))
    await _present_lots(update, ctx, announce_id=announce, dry_run=cfg_dry)


async def lot_toggle_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    mgr: TenderManager = ctx.application.bot_data["mgr"]
    _, ann, lot_id = q.data.split(":", 2)
    pend = mgr.pending.get(f"{q.message.chat_id}:{ann}")
    if not pend:
        await q.answer("Список устарел — пришли номер заново.", show_alert=True)
        return
    lot_id = int(lot_id)
    if lot_id in pend["selected"]:
        pend["selected"].discard(lot_id)
    else:
        pend["selected"].add(lot_id)
    await q.answer()
    try:
        await q.edit_message_reply_markup(reply_markup=_picker_keyboard(pend))
    except Exception:  # noqa: BLE001 — ignore "message not modified"
        pass


async def lot_go_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    mgr: TenderManager = ctx.application.bot_data["mgr"]
    _, ann = q.data.split(":", 1)
    key = f"{q.message.chat_id}:{ann}"
    pend = mgr.pending.get(key)
    if not pend:
        await q.answer("Список устарел — пришли номер заново.", show_alert=True)
        return
    if not pend["selected"]:
        await q.answer("Отметь хотя бы один лот.", show_alert=True)
        return
    await q.answer()
    lot_ids = sorted(pend["selected"])
    dry = pend["dry_run"]
    mgr.pending.pop(key, None)
    ok = await mgr.start_watch(ctx.application, announce_id=ann, lot_ids=lot_ids,
                               price=None, dry_run=dry, chat_id=q.message.chat_id)
    head = "🧪 DRY · " if dry else ""
    if not ok:
        await q.edit_message_text(f"Тендер {ann} уже в работе.")
        return
    await q.edit_message_text(
        f"{head}🎯 Принял тендер <b>{ann}</b>, лоты <b>{_fmt_lots(lot_ids)}</b>. "
        f"Жду открытия — напишу, когда откроется.\n"
        f"В работе тендеров: {mgr.active_count()}. "
        f"Можешь прислать ещё номер, чтобы добавить параллельно.",
        parse_mode="HTML")


async def lot_cancel_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    mgr: TenderManager = ctx.application.bot_data["mgr"]
    _, ann = q.data.split(":", 1)
    mgr.pending.pop(f"{q.message.chat_id}:{ann}", None)
    await q.answer()
    await q.edit_message_text(f"Отменил выбор по тендеру {ann}.")


async def watching_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg = ctx.application.bot_data["cfg"]
    mgr: TenderManager = ctx.application.bot_data["mgr"]
    if not _allowed(update, cfg):
        return
    if not mgr.tenders:
        await update.message.reply_text("Пусто. Пришли номер тендера или /bid <id>.")
        return
    lines = []
    for wt in sorted(mgr.tenders.values(), key=lambda t: -t.started_at):
        e = STATUS_EMOJI.get(wt.status, "ℹ️")
        age = int(time.time() - wt.started_at)
        lines.append(f"{e} <b>{wt.announce_id}</b> лоты {_fmt_lots(wt.lot_ids)} · "
                     f"{wt.status} · {age}s"
                     f"{(' · ' + html.escape(wt.detail[:60])) if wt.detail else ''}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cancel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg = ctx.application.bot_data["cfg"]
    mgr: TenderManager = ctx.application.bot_data["mgr"]
    if not _allowed(update, cfg):
        return
    if not ctx.args:
        await update.message.reply_text("Формат: /cancel <announce_id>")
        return
    ok = mgr.cancel(ctx.args[0].strip())
    await update.message.reply_text("Снял." if ok else "Не нашёл активного наблюдения с таким id.")


async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg = ctx.application.bot_data["cfg"]
    mgr: TenderManager = ctx.application.bot_data["mgr"]
    if not _allowed(update, cfg):
        return
    sig_ok = False
    try:
        sig_ok = await mgr.signer.ping()
    except Exception:
        pass
    tumar_ok, tumar_ver = False, "?"
    try:
        async with TumarCSP(api_key=cfg["tumarcsp"]["api_key"], ws_url=cfg["tumarcsp"]["ws_url"]) as t:
            tumar_ok, tumar_ver = True, t.version
    except Exception as e:
        tumar_ver = str(e)[:60]
    ci = gz_secrets.get(cfg["goszakup"]["ci_session_service"], "ci_session")
    await update.message.reply_text(
        f"🔐 Подписчик (Kalkan): {'✅' if sig_ok else '🔴'}\n"
        f"🧮 TumarCSP: {'✅ v' + str(tumar_ver) if tumar_ok else '🔴 ' + str(tumar_ver)}\n"
        f"🍪 ci_session: {'✅ есть' if ci else '🔴 нет (нужен /login через CLI)'}\n"
        f"📊 Активных тендеров: {mgr.active_count()} / всего в памяти {len(mgr.tenders)}")


# ---------------- boot ----------------

def _setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for noisy in ("httpx", "httpcore", "telegram", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    structlog.configure(
        processors=[structlog.processors.add_log_level, structlog.dev.ConsoleRenderer(colors=False)],
        cache_logger_on_first_use=True,
    )


async def _post_init(app: Application) -> None:
    await app.bot_data["mgr"].signer.start()
    logging.info("kalkan signer started")
    await app.bot.set_my_commands([
        BotCommand("bid", "Тендер: показать лоты и ждать открытия"),
        BotCommand("drybid", "То же, но сухой прогон"),
        BotCommand("watching", "Активные тендеры"),
        BotCommand("cancel", "Снять тендер с наблюдения"),
        BotCommand("status", "Здоровье бота"),
    ])


async def _post_shutdown(app: Application) -> None:
    try:
        await app.bot_data["mgr"].signer.close()
    except Exception:
        pass


_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{20,}$")


def _resolve_bot_token(cfg: dict) -> Optional[str]:
    """Resolve the Telegram bot token, tolerant of the common config mix-ups.

    Priority: telegram.bot_token (the token written straight into config) > the
    env var NAMED by telegram.bot_token_env. Defensive: if bot_token_env was set
    to the TOKEN itself ("<digits>:<secret>") instead of an env-var name, use it
    directly — that exact mistake just silently exits with a confusing message.
    """
    tg = cfg.get("telegram", {}) or {}
    literal = str(tg.get("bot_token") or "").strip()
    if _TOKEN_RE.match(literal):
        return literal
    env_name = str(tg.get("bot_token_env") or "TG_BOT_TOKEN").strip()
    if _TOKEN_RE.match(env_name):     # token pasted into bot_token_env by mistake
        return env_name
    return os.environ.get(env_name)


def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    _setup_logging()
    cfg_path = Path(os.environ.get("BOT_CONFIG_PATH", "config.yaml"))
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))

    token = _resolve_bot_token(cfg)
    if not token:
        env_name = str((cfg.get("telegram") or {}).get("bot_token_env") or "TG_BOT_TOKEN").strip()
        raise SystemExit(
            "Telegram bot token not found. Either put it straight into config.yaml:\n"
            '  telegram:\n    bot_token: "123456:ABC-your-token"\n'
            f'or set the env var (PowerShell:  $env:{env_name} = "123456:ABC-your-token").')
    try:
        with bot_instance(token):
            return _run_bot(cfg, token)
    except BotAlreadyRunning as e:
        logging.error("%s", e)
        return 1


def _run_bot(cfg: dict, token: str) -> int:
    ecp_pw = gz_secrets.get(cfg["ecp"]["password_keyring_service"], cfg["ecp"]["password_keyring_user"])
    if not ecp_pw:
        raise SystemExit("ECP password missing — run `python cli.py login` once, or set the secret.")
    if not Path(cfg["ecp"]["p12_path"]).is_file():
        raise SystemExit(f"ECP .p12 not found at {cfg['ecp']['p12_path']}")

    signer = KalkanSigner(p12_path=cfg["ecp"]["p12_path"], password=ecp_pw)
    mgr = TenderManager(cfg, signer)

    app = (
        Application.builder().token(token)
        .defaults(Defaults(parse_mode=None, block=False))
        .post_init(_post_init).post_shutdown(_post_shutdown).build()
    )
    app.bot_data["cfg"] = cfg
    app.bot_data["mgr"] = mgr
    app.add_handler(CommandHandler(["start", "help"], start_cmd))
    app.add_handler(CommandHandler("bid", bid_cmd))
    app.add_handler(CommandHandler("drybid", drybid_cmd))
    app.add_handler(CommandHandler("watching", watching_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CallbackQueryHandler(lot_toggle_cb, pattern=r"^ls:"))
    app.add_handler(CallbackQueryHandler(lot_go_cb, pattern=r"^lg:"))
    app.add_handler(CallbackQueryHandler(lot_cancel_cb, pattern=r"^lc:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_entry))
    logging.info("goszakup bot starting (whitelist: %s)", cfg["telegram"]["allowed_user_ids"])
    app.run_polling(close_loop=False, drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
