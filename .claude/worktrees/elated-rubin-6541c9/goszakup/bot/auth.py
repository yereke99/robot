"""Whitelist enforcement.

Every handler entry-point goes through `whitelist_only`. Non-whitelisted
users get a rejection message; admin (first id in allowed_user_ids if
admin_user_ids unset) is DM'd about the attempt.
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import Awaitable, Callable, Optional

from telegram import Update
from telegram.ext import ContextTypes

from .texts import DENIED

log = logging.getLogger(__name__)


def whitelist_only(
    func: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Optional[int]]],
):
    """Decorator: blocks calls from user_ids not in the configured whitelist."""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None:
            return None
        ctx = context.application.bot_data.get("ctx")
        if ctx is None:
            return None  # boot race; ignore
        allowed = ctx.config.telegram.allowed_user_ids
        if user.id not in allowed:
            log.warning("denied user_id=%s username=%s", user.id, user.username)
            if update.effective_chat:
                await update.effective_chat.send_message(
                    DENIED.format(user_id=user.id),
                    parse_mode="HTML",
                )
            for admin_id in ctx.config.telegram.admin_user_ids:
                if admin_id == user.id:
                    continue
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=(
                            f"⚠️ Попытка доступа: user_id={user.id}, "
                            f"username=@{user.username}, name={user.full_name}"
                        ),
                    )
                except Exception as e:
                    log.warning("failed to notify admin %s: %s", admin_id, e)
            return None
        return await func(update, context)

    return wrapper
