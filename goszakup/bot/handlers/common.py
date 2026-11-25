"""Common handlers: /start /help /cancel /login.

Stripped down to the bare minimum after the bot was scoped to a single
job: accept a tender URL and bid on it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes, ConversationHandler

from ..auth import whitelist_only
from ..context import BotContext
from ..search import probe_portal_session
from ..texts import CANCELLED, DENIED, HELP, NO_ACTIVE_DIALOG, WELCOME
from ... import secrets as gz_secrets
from ...client import PortalLoginRequired

log = logging.getLogger(__name__)


async def _can_replace_portal_session(update: Update, ctx: BotContext) -> bool:
    if update.effective_user.id not in ctx.config.telegram.allowed_user_ids:
        await update.effective_chat.send_message(
            DENIED.format(user_id=update.effective_user.id), parse_mode="HTML",
        )
        return False
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_chat.send_message("🔐 Обновите сессию командой /cookie в личном чате с ботом.")
        return False
    if ctx.bid_lock.locked():
        await update.effective_chat.send_message("⚠️ Бот выполняет операцию с кабинетом. Повторите команду через несколько секунд.")
        return False
    return True


def _save_portal_cookie(service: str, header: str, ci_session: str) -> None:
    # BotContext reads cookie_header first on reboot, including after a bare /cookie.
    gz_secrets.set(service, "cookie_header", header)
    gz_secrets.set(service, "ci_session", ci_session)


async def activate_portal_cookie(ctx: BotContext, header: str, ci_session: str) -> str:
    ctx.set_ci_session(header)
    ctx.last_login_at = time.time()
    ctx.last_cookie_probe = (ctx.last_login_at, True)
    try:
        await asyncio.to_thread(_save_portal_cookie, ctx.config.ci_session_service, header, ci_session)
    except Exception as exc:
        log.warning("portal cookie persistence failed (%s)", type(exc).__name__)
        return "\n⚠️ Сессия активна, но сохранить её для перезапуска не удалось."
    return ""


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
async def codes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать доступные коды subject_address и iik для компании."""
    args = context.args or []
    announce_id = args[0].strip() if args else "17911926"
    ctx: BotContext = context.application.bot_data["ctx"]
    if ctx.gz_client is None:
        await update.effective_chat.send_message("❌ Клиент не инициализирован.")
        return
    msg = await update.effective_chat.send_message(f"🔍 Запрашиваю реквизиты компании для тендера #{announce_id}…")
    codes = await _fetch_company_codes(ctx.gz_client, announce_id)
    lines = [f"📋 <b>Реквизиты ТОО для тендера #{announce_id}:</b>\n"]
    addr_options = codes.get("subject_address", [])
    if addr_options:
        lines.append("📍 <b>Юридический адрес (subject_address):</b>")
        for val, label in addr_options:
            lines.append(f"  • <code>{val}</code> — {label.strip()}")
    else:
        lines.append("📍 <b>Юридический адрес:</b> не найден")

    iik_options = codes.get("iik", [])
    if iik_options:
        lines.append("\n🏦 <b>Банковский счет (iik):</b>")
        for val, label in iik_options:
            lines.append(f"  • <code>{val}</code> — {label.strip()}")
    else:
        lines.append("\n🏦 <b>Банковский счет:</b> не найден")

    await msg.edit_text("\n".join(lines), parse_mode="HTML")


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
    if not await _can_replace_portal_session(update, ctx):
        return

    # Необязательный announce_id для поиска кодов
    args = context.args or []
    announce_id = args[0].strip() if args else None

    if ctx.gz_client is None:
        await update.effective_chat.send_message("❌ Клиент goszakup не инициализирован. Перезапусти бота.")
        return

    if not ctx.account_password:
        await update.effective_chat.send_message(
            "Войдите в кабинет на zakup.gov.kz в браузере и обновите сессию через /cookie.",
        )
        return

    msg = await update.effective_chat.send_message("🔐 Выполняю вход через ЭЦП…")

    try:
        async with ctx.bid_lock:
            ci = await ctx.gz_client.relogin_isolated(
                account_password=ctx.account_password,
                kalkan_signer=ctx.signer,
            )
            storage_warning = await activate_portal_cookie(ctx, f"ci_session={ci}", ci)

        log.info("login_cmd: success user_id=%s", update.effective_user.id)

        # Если передан announce_id — вытащить числовые коды
        if announce_id:
            await msg.edit_text(f"✅ Вход выполнен. Ищу коды для тендера {announce_id}…")
            codes = await _fetch_company_codes(ctx.gz_client, announce_id)

            lines = [f"✅ <b>Вход выполнен.</b> Cookie обновлена.{storage_warning}\n"]

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
                f"✅ Вход выполнен. Cookie обновлена.{storage_warning}\n\n"
                "💡 Чтобы найти коды iik/subject_address:\n"
                "<code>/login 17027506</code>  (укажи номер тендера)",
                parse_mode="HTML",
            )

    except PortalLoginRequired as e:
        await msg.edit_text(str(e), disable_web_page_preview=True)
    except Exception as e:
        log.warning("login_cmd failed (%s)", type(e).__name__)
        await msg.edit_text(
            "❌ Не удалось войти через ЭЦП. Войдите в кабинет в браузере и обновите сессию через /cookie.",
        )


@whitelist_only
async def cookie_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Установить cookie сессии вручную из браузера.

    Использование:
        /cookie <значение_ci_session>
    """
    args = context.args or []
    if not args:
        await update.effective_chat.send_message(
            "ℹ️ <b>Как передать cookie из браузера:</b>\n\n"
            "1. Откройте кабинет Госзакупок в браузере и войдите через <code>zakup.gov.kz</code>.\n"
            "2. Нажмите <code>F12</code> → вкладка <b>Application</b> (Приложение) → <b>Cookies</b> → <code>https://v3bl.goszakup.gov.kz</code>.\n"
            "3. Найдите строку <b><code>ci_session</code></b> и скопируйте её значение.\n"
            "4. Отправьте в личный чат бота:\n"
            "<code>/cookie ВСТАВЬТЕ_СКОПИРОВАННОЕ_ЗНАЧЕНИЕ</code>",
            parse_mode="HTML",
        )
        return

    await accept_portal_cookie(update, context, " ".join(args))


async def accept_portal_cookie(update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str) -> None:
    ctx: BotContext = context.application.bot_data["ctx"]
    if not await _can_replace_portal_session(update, ctx):
        return
    if ctx.gz_client is None:
        await update.effective_chat.send_message("❌ Клиент портала ещё не запущен. Повторите /cookie позже.")
        return

    header = raw.strip()
    if header.lower().startswith("curl "):
        match = re.search(r"""(?:^|\s)(?:-b|--cookie)\s+(?:"([^"]+)"|'([^']+)'|(\S+))""", header)
        header = next((value for value in match.groups() if value), "") if match else ""
        header = header.replace("^", "")
    else:
        header = header.strip('"\'')
    if re.fullmatch(r"[!-~]{10,}", header) and ";" not in header and "=" not in header.rstrip("="):
        header = f"ci_session={header}"
    match = re.search(r"(?:^|;\s*)ci_session=([^;\s]{10,})(?:;|$)", header)
    if not match or not re.fullmatch(r"[ -~]+", header):
        await update.effective_chat.send_message("❌ Не найдено значение ci_session. Отправьте /cookie для инструкции.")
        return

    revision = ctx.portal_session_changed
    candidate = ctx.gz_client.clone_unauthenticated()
    async with candidate:
        ctx.gz_client.sync_cookies_to(candidate)
        candidate.adopt_ci_session(header)
        status = await probe_portal_session(candidate)
    if status in ("expired", "sso"):
        await update.effective_chat.send_message(
            "⚠️ Эта сессия истекла или требует входа. Войдите в кабинет в браузере и повторите /cookie."
        )
        return
    if status == "unavailable":
        await update.effective_chat.send_message(
            "⚠️ Портал не ответил на проверку кабинета. Повторите /cookie через несколько секунд."
        )
        return

    if not await _can_replace_portal_session(update, ctx):
        return
    async with ctx.bid_lock:
        if revision.is_set():
            message = "⚠️ Сессия уже обновилась во время проверки. При необходимости повторите /cookie."
        else:
            warning = await activate_portal_cookie(ctx, header, match[1])
            message = f"✅ Сессия проверена. Личный кабинет доступен.{warning}"
    await update.effective_chat.send_message(message)
