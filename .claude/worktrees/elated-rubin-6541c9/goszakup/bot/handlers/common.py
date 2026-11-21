"""Common handlers: /start /help /cancel /login.

Stripped down to the bare minimum after the bot was scoped to a single
job: accept a tender URL and bid on it.
"""

from __future__ import annotations

import logging
import re

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from ..auth import whitelist_only
from ..context import BotContext
from ..texts import CANCELLED, HELP, NO_ACTIVE_DIALOG, WELCOME
from ... import secrets as gz_secrets

log = logging.getLogger(__name__)


async def _fetch_company_codes(gz_client, announce_id: str) -> dict:
    """Получить числовые коды subject_address и iik со страницы создания заявки."""
    try:
        r = await gz_client._http.get(f"/ru/application/create/{announce_id}", timeout=10.0)
        html = r.text

        result = {"subject_address": [], "iik": []}

        addr_block = re.search(r'name="subject_address".*?</select>', html, re.DOTALL)
        if addr_block:
            result["subject_address"] = re.findall(
                r'<option\s+value="(\d+)"[^>]*>([^<]*)</option>',
                addr_block.group(0)
            )

        iik_block = re.search(r'name="iik".*?</select>', html, re.DOTALL)
        if iik_block:
            result["iik"] = re.findall(
                r'<option\s+value="(\d+)"[^>]*>([^<]*)</option>',
                iik_block.group(0)
            )

        return result
    except Exception as e:
        log.warning("fetch_company_codes failed: %s", e)
        return {}


@whitelist_only
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "пользователь"
    await update.effective_chat.send_message(
        WELCOME.format(name=name),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@whitelist_only
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(HELP, parse_mode="HTML")


@whitelist_only
async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("conversation_active"):
        context.user_data.clear()
        await update.effective_chat.send_message(CANCELLED)
        return ConversationHandler.END
    await update.effective_chat.send_message(NO_ACTIVE_DIALOG)
    return ConversationHandler.END


@whitelist_only
async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вручную обновить cookie сессии goszakup через ЭЦП.

    Использование:
        /login              — просто обновить cookie
        /login 17027506     — обновить cookie + показать коды iik/subject_address для тендера
    """
    ctx: BotContext = context.application.bot_data["ctx"]

    # Необязательный announce_id для поиска кодов
    args = context.args or []
    announce_id = args[0].strip() if args else None

    if ctx.gz_client is None:
        await update.effective_chat.send_message("❌ Клиент goszakup не инициализирован. Перезапусти бота.")
        return

    if not ctx.account_password:
        await update.effective_chat.send_message(
            "❌ Пароль кабинета не найден.\n"
            "Проверь <code>GOSZAKUP_ATAGOS_ACC_DEFAULT</code> в файле <code>.env</code>.",
            parse_mode="HTML",
        )
        return

    msg = await update.effective_chat.send_message("🔐 Выполняю вход через ЭЦП…")

    try:
        ci = await ctx.gz_client.login(
            account_password=ctx.account_password,
            kalkan_signer=ctx.signer,
        )
        ctx.set_ci_session(ci)
        gz_secrets.set(ctx.config.ci_session_service, "ci_session", ci)
        ctx.last_cookie_probe = None  # сброс кэша проверки

        import time
        ctx.last_login_at = time.time()

        log.info("login_cmd: success user_id=%s", update.effective_user.id)

        # Если передан announce_id — вытащить числовые коды
        if announce_id:
            await msg.edit_text(f"✅ Вход выполнен. Ищу коды для тендера {announce_id}…")
            codes = await _fetch_company_codes(ctx.gz_client, announce_id)

            lines = ["✅ <b>Вход выполнен.</b> Cookie обновлена.\n"]

            addr_options = codes.get("subject_address", [])
            if addr_options:
                lines.append("📍 <b>subject_address (юр. адрес):</b>")
                for val, label in addr_options:
                    lines.append(f"  <code>{val}</code>  {label.strip()}")
                log.info("login_cmd.subject_address options=%s", addr_options)
            else:
                lines.append("📍 <b>subject_address:</b> не найдено на странице")

            iik_options = codes.get("iik", [])
            if iik_options:
                lines.append("\n🏦 <b>iik (банковский счёт):</b>")
                for val, label in iik_options:
                    lines.append(f"  <code>{val}</code>  {label.strip()}")
                log.info("login_cmd.iik options=%s", iik_options)
            else:
                lines.append("\n🏦 <b>iik:</b> не найдено на странице")

            lines.append(
                "\n💡 Скопируй числовые значения в <code>config.yaml</code>:\n"
                "<code>company.subject_address: \"ЧИСЛО\"</code>\n"
                "<code>company.iik: \"ЧИСЛО\"</code>"
            )

            await msg.edit_text("\n".join(lines), parse_mode="HTML")
        else:
            await msg.edit_text(
                "✅ Вход выполнен. Cookie обновлена.\n\n"
                "💡 Чтобы найти коды iik/subject_address:\n"
                "<code>/login 17027506</code>  (укажи номер тендера)",
                parse_mode="HTML",
            )

    except Exception as e:
        log.exception("login_cmd failed: %s", e)
        await msg.edit_text(
            f"❌ Ошибка входа:\n<code>{str(e)[:300]}</code>",
            parse_mode="HTML",
        )
