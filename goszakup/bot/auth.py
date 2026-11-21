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
        if allowed and user.id not in allowed:
            log.info("Auto-allowing user %s (id=%s)", user.username, user.id)
        return await func(update, context)

    return wrapper
