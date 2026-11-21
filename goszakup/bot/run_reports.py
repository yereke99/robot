"""Send reports about a bot process without delaying bid handling."""

from __future__ import annotations

import asyncio
import io
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import structlog
from telegram import InputFile

from ..timing import BidTrace
from .git_sync import REPORT_NAME, GitSyncError, publish_reports
from .log_capture import RuntimeLog, public_bid_report

log = structlog.get_logger("bot")
_PUSH_BATCH_SIZE = 24


def _report_filename(kind: str, created: datetime) -> str:
    return f"{kind}_{created:%Y%m%dT%H%M%SZ}_{uuid4().hex[:12]}.txt"


def _save_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


class RunReports:
    def __init__(
        self, runtime: RuntimeLog, log_dir: Path, chat_id: int | None,
        *, repo: Path, revision: str = "",
    ) -> None:
        self.runtime = runtime
        self.log_dir = log_dir
        self.chat_id = chat_id
        self.repo = repo
        self.pending = log_dir / "github_pending"
        self.revision = revision if re.fullmatch(r"[0-9a-f]{40,64}", revision) else ""
        self.started_at = datetime.now(timezone.utc)
        self.started_mono = time.monotonic()
        self.filename = _report_filename("run", self.started_at)
        self._changed = asyncio.Event()
        self._stopping = False
        self._task: asyncio.Task | None = None
        self._notified = False

    def start(self, application) -> None:
        self._task = asyncio.create_task(self._run(application), name="atagos-run-reports")

    async def save_bid(
        self, trace: BidTrace, result: str, scheduled_elapsed: float | None = None,
    ) -> str | None:
        """Keep a separate report until a Git push has succeeded."""
        name = _report_filename("bid", datetime.now(timezone.utc))
        try:
            await asyncio.to_thread(self._save_public_bid, name, trace, result, scheduled_elapsed)
        except OSError as exc:
            log.warning("bot.bid_report_save_failed", error_type=type(exc).__name__)
            return None
        self._changed.set()
        return name

    def _save_public_bid(
        self, name: str, trace: BidTrace, result: str, scheduled_elapsed: float | None,
    ) -> None:
        report = public_bid_report(trace, result, scheduled_elapsed)
        if self.revision:
            report += f"\nВерсия {self.revision}\n"
        _save_file(self.pending / name, report.encode("utf-8"))

    async def stop(self) -> None:
        self._stopping = True
        self._changed.set()
        if self._task is not None:
            await self._task

    async def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self, application) -> None:
        # PTB calls post_init before Updater.start_polling. Report a launch only
        # after polling and application startup have both succeeded.
        while not application.running and not self._stopping:
            await asyncio.sleep(0.1)
        if self._stopping:
            return
        ok = await self._checkpoint(application.bot, "Бот запущен", send_message=True)
        while not self._stopping:
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=900 if ok else 60)
            except TimeoutError:
                pass
            self._changed.clear()
            if not self._stopping:
                ok = await self._checkpoint(application.bot, "Бот работает", send_message=not self._notified)
        await self._checkpoint(application.bot, "Бот остановлен", send_message=True)

    async def _checkpoint(self, bot, status: str, *, send_message: bool) -> bool:
        uptime = int(time.monotonic() - self.started_mono)
        updated = datetime.now(timezone.utc)
        header = (
            f"{status}\n"
            f"Запуск {self.started_at:%Y-%m-%d %H:%M:%S} UTC\n"
            f"Отчёт {updated:%Y-%m-%d %H:%M:%S} UTC\n"
            f"Время работы {uptime} с\n"
        )
        if self.revision:
            header += f"Версия {self.revision}\n"
        detailed, public = await asyncio.to_thread(self.runtime.report_logs)
        details = (header + "\n" + detailed + "\n").encode("utf-8")
        anonymous = (header + "\n" + public + "\n").encode("utf-8")
        jobs = [asyncio.to_thread(self._save_and_push, details, anonymous)]
        if send_message and self.chat_id is not None:
            jobs.append(self._send_document(bot, details, f"{status} · время работы {uptime} с"))
        return all(await asyncio.gather(*jobs))

    def _save_and_push(self, details: bytes, anonymous: bytes) -> bool:
        ok = True
        try:
            _save_file(self.log_dir / "runs" / self.filename, details)
        except OSError as exc:
            log.warning("bot.run_report_save_failed", error_type=type(exc).__name__)
            ok = False
        try:
            _save_file(self.pending / self.filename, anonymous)
            return self._push_pending() and ok
        except (GitSyncError, OSError) as exc:
            error = str(exc) if isinstance(exc, GitSyncError) else type(exc).__name__
            log.warning("bot.run_report_push_failed", error_type=error)
            return False

    def _push_pending(self) -> bool:
        pending = sorted(
            path for path in self.pending.glob("*.txt")
            if REPORT_NAME.fullmatch(path.name) and not path.is_symlink() and path.is_file()
        )
        batch = pending[:_PUSH_BATCH_SIZE]
        publish_reports(self.repo, {path.name: path.read_bytes() for path in batch})
        for path in batch:
            path.unlink(missing_ok=True)
        return len(pending) <= _PUSH_BATCH_SIZE

    async def _send_document(self, bot, file: bytes, caption: str) -> bool:
        try:
            await asyncio.wait_for(
                bot.send_document(
                    chat_id=self.chat_id,
                    document=InputFile(io.BytesIO(file), filename=self.filename),
                    caption=caption,
                ),
                timeout=12,
            )
        except Exception as exc:
            log.warning("bot.run_report_telegram_failed", error_type=type(exc).__name__)
            return False
        self._notified = True
        return True
