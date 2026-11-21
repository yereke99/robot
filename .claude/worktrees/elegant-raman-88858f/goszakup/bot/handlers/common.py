"""Common handlers: /start /help /cancel.

Stripped down to the bare minimum after the bot was scoped to a single
job: accept a tender URL and bid on it.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from ..auth import whitelist_only
from ..texts import CANCELLED, HELP, NO_ACTIVE_DIALOG, WELCOME


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
