"""Throttled progress streaming via editMessageText.

Telegram allows ~1 edit/sec per message; bursts get 429. We buffer pending
lines and flush at most every 1.1s, coalescing rapid updates from the
parallel signing phase.

Usage:
    msg = ProgressMessage.start(bot, chat_id, "🔐 Логинюсь…")
    msg.update("📋 Создаю заявку…")
    ... (lots of updates from inside submit_bid via log_capture)
    await msg.finish("✅ Готово за 15.8s")

The active progress message for a given task is held in a contextvar
(`current_progress`) so structlog log_capture can find it without explicit
plumbing.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from typing import Optional

from telegram import Bot
from telegram.error import RetryAfter, TelegramError

log = logging.getLogger(__name__)

# Reading: log_capture.py emits into this from deep inside the pipeline.
# Writing: each bid handler sets this on entry, unsets on exit.
current_progress: contextvars.ContextVar[Optional["ProgressMessage"]] = (
    contextvars.ContextVar("current_progress", default=None)
)


class ProgressMessage:
    """Single Telegram message that gets re-edited as a bid progresses.

    Internally a buffer + a single flush task. Callers append lines via
    `update(line)`. The flush task wakes at least every `min_interval_s`
    and pushes the latest accumulated text.
    """

    MIN_INTERVAL_S = 1.1
    TEXT_LIMIT = 3900

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        initial_line: str,
        *,
        header: str = "",
    ):
        self.bot = bot
        self.chat_id = chat_id
        self.message_id: Optional[int] = None
        self.header = header
        self._lines = [initial_line]
        self._last_text: str = ""
        self._last_flush_at: float = 0.0
        self._dirty = asyncio.Event()
        self._finished = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    @classmethod
    def start(
        cls,
        bot: Bot,
        chat_id: int,
        initial_line: str,
        *,
        header: str = "",
    ) -> "ProgressMessage":
        """Start buffering immediately while the worker opens the Telegram message."""
        pm = cls(bot, chat_id, initial_line, header=header)
        pm._task = asyncio.create_task(pm._run())
        return pm

    def update(self, line: str) -> None:
        """Append a status line. Safe to call from any task / sync context.

        Coalesces consecutive identical updates so the user doesn't see
        flicker if the same event fires twice.
        """
        if self._lines and self._lines[-1] == line:
            return
        self._lines.append(line)
        self._dirty.set()

    def replace_last(self, line: str) -> None:
        """Mutate the last line (for elapsed-time counter that ticks every flush)."""
        if not self._lines:
            self._lines.append(line)
        else:
            self._lines[-1] = line
        self._dirty.set()

    async def finish(self, final_line: Optional[str] = None) -> None:
        if final_line:
            self.update(final_line)
        self._finished.set()
        self._dirty.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()

    def cancel(self) -> None:
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        await self._flush(force=True)
        while not self._finished.is_set():
            try:
                await asyncio.wait_for(self._dirty.wait(), timeout=self.MIN_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
            self._dirty.clear()
            if not self._finished.is_set():
                await self._flush(force=False)
        await self._flush(force=True)

    async def _flush(self, *, force: bool) -> None:
        now = time.monotonic()
        if not force and now - self._last_flush_at < self.MIN_INTERVAL_S:
            return
        text_body = "\n".join(self._lines)
        text = f"{self.header}\n{text_body}".strip() if self.header else text_body
        if len(text.encode("utf-16-le")) // 2 > self.TEXT_LIMIT:
            # Keep the latest whole updates that fit, prioritizing the result.
            header_size = len(self.header.encode("utf-16-le")) // 2 + 1 if self.header else 0
            remaining = self.TEXT_LIMIT - header_size
            visible = []
            for line in reversed(self._lines):
                size = len(line.encode("utf-16-le")) // 2 + (1 if visible else 0)
                if size > remaining:
                    break
                visible.append(line)
                remaining -= size
            text = "\n".join(([self.header] if self.header else []) + list(reversed(visible)))
        if text == self._last_text:
            return
        try:
            if self.message_id is None:
                message = await self.bot.send_message(
                    text=text, chat_id=self.chat_id, parse_mode="HTML",
                )
                self.message_id = message.message_id
            else:
                await self.bot.edit_message_text(
                    text=text, chat_id=self.chat_id, message_id=self.message_id,
                    parse_mode="HTML",
                )
            self._last_text = text
            self._last_flush_at = now
        except RetryAfter as e:
            log.warning("flood control on progress update, sleeping %s", e.retry_after)
            await asyncio.sleep(float(e.retry_after) + 0.2)
        except TelegramError as e:
            log.warning("progress update failed: %s", e)
