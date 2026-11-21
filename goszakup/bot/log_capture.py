"""structlog processor that forwards pipeline.* events to the active
ProgressMessage.

Pipeline emits structlog events (`pipeline.created`, `pipeline.sign_group.start`,
`pipeline.sign_progress`, `pipeline.public_application.result`, …). This
processor matches event names and pushes humanized Russian lines into the
ProgressMessage held in the `current_progress` contextvar.

The processor is sync (structlog processors must be) — it never awaits.
It only mutates the progress buffer; the dedicated flush task in
ProgressMessage handles the actual editMessageText calls.
"""

from __future__ import annotations

import logging
import queue
import re
from datetime import datetime, timezone
from logging.handlers import QueueListener, RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import structlog

from .. import timing
from ..timing import HttpTiming, current_trace, portal_trace_path
from .progress import current_progress


def _humanize(event: str, kw: dict[str, Any]) -> str | None:
    """High-level milestones only — no per-step timings, no internal stage names.

    Five "user-meaningful" steps total: login → lot → created → signed →
    submitted. Everything else is internal and stays in the structured logs.
    """
    if event == "pipeline.auto_login.start":
        return "🔐 Логин в кабинет…"
    if event == "pipeline.auto_login.done":
        return "🔐 Логин выполнен"
    if event == "pipeline.created":
        return f"📋 Заявка создана: <code>{kw.get('app_id')}</code>"
    if event == "pipeline.sign_group.start":
        return "📝 Подписываю документы…"
    if event == "pipeline.sign_group.done":
        return "✅ Документы подписаны"
    if event == "pipeline.priceoffer_pairs":
        return "💰 Готовлю ценовое предложение…"
    if event == "pipeline.public_application.result":
        result = kw.get("result", {})
        if isinstance(result, dict) and result.get("status") == "ok":
            return "✅ Заявка подана"
        return f"❌ Сервер ответил: {str(result)[:200]}"
    # --- flow.py (repairs / gamma price) milestones -------------------------
    if event == "poll.ready":
        return "🟢 Тендер открыт — подаю…"
    if event == "flow.created":
        return f"📋 Заявка создана: <code>{kw.get('app_id')}</code>"
    if event == "flow.app_lot_map":
        return "📝 Подписываю документы…"
    if event == "flow.docs_next.settling":
        return "⏳ Портал проверяет подписи…"
    if event == "flow.docs_next.resave.targeted":
        return "🔁 Портал не увидел подпись — переподписываю документ…"
    if event == "flow.docs_next.resave.full":
        return "🔁 Переподписываю все документы…"
    if event == "flow.gamma.pairs":
        return "💰 Шифрую цену (TumarCSP)…"
    if event == "flow.gamma.done":
        return "📨 Отправляю заявку…"
    return None


def progress_processor(logger, method_name, event_dict):
    """structlog processor. Must return event_dict (or raise DropEvent).

    Pulls the active ProgressMessage from contextvar and updates it.
    """
    pm = current_progress.get()
    if pm is None:
        return event_dict
    event_name = event_dict.get("event")
    if not isinstance(event_name, str):
        return event_dict
    if not event_name.startswith(("pipeline.", "flow.", "poll.")):
        return event_dict
    line = _humanize(event_name, event_dict)
    if line is None:
        return event_dict
    try:
        pm.update(line)
    except Exception:
        pass
    return event_dict


# The per-bid journal is sent out of process. Keep only known diagnostic fields;
# response bodies, raw exception text and arbitrary nested values can echo secrets.
_TRACE_SKIP = ("http.get", "http.post", "login.")
_TRACE_EVENT = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*")
_TRACE_NUMBER_FIELDS = {
    "attempt", "attempts", "strike", "level", "status", "len", "bytes", "body_len",
    "n", "doc", "doc_id", "fid", "num", "pooled", "count", "accepted", "ok", "resumed",
    "pid", "dry_run", "parallel", "max_pending",
}
_TRACE_ID_FIELDS = {"announce", "announce_id", "app_id", "app_lot", "lp_id"}
_TRACE_DURATION_FIELDS = {"dt_s", "elapsed_s", "elapsed_ms", "waited_s", "spacing_s",
                          "since_start_s", "total_s", "duration_s", "retry_s", "ms"}
_TRACE_PATH_FIELDS = {"path", "location", "final", "url", "dl"}
_TRACE_CODE_FIELDS = {"verdict", "phase", "step", "source", "mode", "error_type",
                      "pipeline_type", "release", "python", "version", "platform"}
_APPENDIX = re.compile(r"приложени[еяю]\s*№?\s*([0-9]{1,3})\b", re.IGNORECASE)


def _trace_fields(event: str, event_dict: dict) -> list[str]:
    parts = []
    for key, value in event_dict.items():
        if key in _TRACE_NUMBER_FIELDS | _TRACE_DURATION_FIELDS and isinstance(value, (int, float, bool)):
            parts.append(f"{key}={value}")
        elif key in _TRACE_ID_FIELDS and isinstance(value, (int, str)) and re.fullmatch(r"[0-9]{1,20}", str(value)):
            parts.append(f"{key}={value}")
        elif key in _TRACE_PATH_FIELDS and isinstance(value, str):
            # urlsplit can raise for malformed URLs. Ignore only that field.
            try:
                parts.append(f"{key}={portal_trace_path(urlsplit(value).path)}")
            except ValueError:
                pass
        elif key in _TRACE_CODE_FIELDS and isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,40}", value):
            parts.append(f"{key}={value}")
        elif key == "docs" and isinstance(value, (tuple, list)) and all(type(v) is int for v in value):
            parts.append(f"docs={value[:15]}")
    if event.startswith("flow.docs_next"):
        message = event_dict.get("message")
        if isinstance(message, str):
            if "подпиш" in message.lower():
                parts.append("message=Подпишите")
            if match := _APPENDIX.search(message):
                parts.append(f"Приложение {match.group(1)}")
    return parts


def trace_processor(logger, method_name, event_dict):
    """structlog processor: copy this line into the active bid's report
    (timing.BidTrace). No-op outside a bid; never raises."""
    trace = current_trace.get()
    if trace is None:
        return event_dict
    try:
        event = event_dict.get("event")
        if not isinstance(event, str) or not _TRACE_EVENT.fullmatch(event) or event.startswith(_TRACE_SKIP):
            return event_dict
        trace.log(method_name, " ".join([event, *_trace_fields(event, event_dict)]))
    except Exception:
        pass
    return event_dict


_RUNTIME_EVENT_PREFIXES = (
    "bot.", "runtime.", "telegram.", "flow.", "pipeline.", "scheduled.", "poll.",
    "opt1.", "opt4.", "tumar.", "kalkan.", "popup.", "create_application.",
    "discover_app_id.", "lots_next.", "rediscover.", "local_signer.", "get_encr_info.",
)
_RUNTIME_HTTP_METHODS = {"GET", "POST", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"}
_RUNTIME_OPERATIONS = {
    "wait": {
        "Очередь Kalkan", "Очередь окна TumarCSP", "Повтор проверки подписей",
        "Повтор чтения техспецификации", "Повтор получения параметров шифрования",
    },
    "repair": {"Пересохранение документов по ответу портала"},
    "tumar": {
        "Подключение WebSocket TumarCSP", "TumarCSP SetAPIKey", "TumarCSP GetVersion",
        "EncryptOfferPrice (окно цены)", "Завершение автозаполнения",
    },
    "kalkan": {"LOAD", "PING", "SIGN_FILE", "SIGN_BYTES", "SIGN_TEXT_TSA", "SIGN_XML"},
}
_LOG_LEVELS = {"debug", "info", "warning", "error", "critical"}
_HTTP_STATUS = re.compile(r"(?:[0-9]{3}(?:/[A-Za-z]+)?|[A-Za-z]{1,40})")


class _RuntimeQueueListener(QueueListener):
    def enqueue_sentinel(self) -> None:
        # Shutdown drains the bounded queue even if it was full at close.
        self.queue.put(self._sentinel)


class RuntimeLog:
    """Rotating, sanitized UTF-8 diagnostics independent of the launch console."""

    def __init__(self, log_dir: Path, *, max_bytes: int = 2_000_000, backups: int = 3):
        log_dir.mkdir(parents=True, exist_ok=True)
        self._handler = RotatingFileHandler(log_dir / "runtime.log", maxBytes=max_bytes,
                                            backupCount=backups, encoding="utf-8", delay=True)
        self._queue: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=4096)
        self._listener = _RuntimeQueueListener(self._queue, self._handler)
        self._closed = False
        self._listener.start()
        self._sink = self.record_operation
        timing.runtime_event_sink = self._sink

    def _write(self, level: str, event: str, fields: list[str], label: str = "") -> None:
        if self._closed:
            return
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        entries = [timestamp, level if level in _LOG_LEVELS else "info", event]
        if re.fullmatch(r"#[0-9]{1,20}", label):
            entries.append(f"tender={label[1:]}")
        record = logging.LogRecord("goszakup.runtime", logging.INFO, "", 0,
                                   " ".join(entries + fields), (), None)
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            # A blocked diagnostics disk must not stall bidding.
            pass

    def process(self, logger, method_name, event_dict):
        """Copy selected structured events without altering console processing."""
        try:
            event = event_dict.get("event")
            if (isinstance(event, str) and event.startswith(_RUNTIME_EVENT_PREFIXES)
                    and _TRACE_EVENT.fullmatch(event)):
                trace = current_trace.get()
                self._write(method_name, event, _trace_fields(event, event_dict),
                            trace.label if trace else "")
        except Exception:
            pass
        return event_dict

    def record_operation(self, kind: str, name: str, duration: float, status: str,
                         http: HttpTiming | None, label: str) -> None:
        if kind in _RUNTIME_HTTP_METHODS:
            fields = [f"method={kind}", f"route={portal_trace_path(name)}",
                      f"duration_s={duration:.3f}"]
            if _HTTP_STATUS.fullmatch(status):
                fields.append(f"status={status}")
            if http:
                fields.extend([f"headers_s={http.headers_s:.3f}",
                               f"body_s={max(0.0, duration - http.headers_s):.3f}",
                               f"bytes={http.bytes_received}",
                               f"tcp_s={http.tcp_s:.3f}", f"tls_s={http.tls_s:.3f}"])
                if re.fullmatch(r"HTTP/[0-9.]{1,3}", http.version):
                    fields.append(f"protocol={http.version}")
            self._write("info", "http.response", fields, label)
        elif name in _RUNTIME_OPERATIONS.get(kind, ()):
            self._write("info", f"operation.{kind}", [f"name={name}", f"duration_s={duration:.3f}"],
                        label)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if timing.runtime_event_sink is self._sink:
            timing.runtime_event_sink = None
        try:
            self._listener.stop()
        finally:
            self._handler.close()


def install() -> None:
    """Add `progress_processor` to structlog's processor chain.

    Safe to call multiple times — re-runs structlog.configure with the
    current processor list plus our addition (deduplicated).
    """
    cfg = structlog.get_config()
    procs = list(cfg.get("processors", []))
    if progress_processor in procs:
        return
    # Insert before the renderer (last) so we still see structured kwargs.
    if procs:
        procs.insert(-1, progress_processor)
    else:
        procs = [progress_processor]
    structlog.configure(processors=procs)
