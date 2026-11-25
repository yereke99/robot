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
from collections import deque
from collections.abc import Iterator
from datetime import datetime, timezone
from logging.handlers import QueueListener, RotatingFileHandler
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlsplit

import structlog

from .. import timing
from ..timing import BidTrace, HttpTiming, current_trace, portal_trace_path
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
    "attempt", "attempts", "strike", "level", "status", "len", "bytes", "body_len", "suffix_bytes",
    "n", "doc", "doc_id", "fid", "num", "pooled", "count", "accepted", "ok", "resumed",
    "pid", "dry_run", "parallel", "max_pending",
    "hit", "named", "presigned", "listed",
}
_TRACE_ID_FIELDS = {"announce", "announce_id", "app_id", "app_lot", "lp_id"}
_TRACE_DURATION_FIELDS = {"dt_s", "elapsed_s", "elapsed_ms", "waited_s", "spacing_s",
                          "since_start_s", "total_s", "duration_s", "retry_s", "ms", "lead_s"}
_TRACE_PATH_FIELDS = {"path", "location", "final", "url", "dl"}
# "reply" is client.reply_shape() (key names and short codes of an AJAX answer),
# "ctx" is flow._id_context() (the attribute or URL segment an id sits in).
_TRACE_CODE_FIELDS = {"verdict", "phase", "step", "source", "mode", "error_type",
                      "pipeline_type", "release", "python", "version", "platform",
                      "reply", "ctx"}
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
_PUBLIC_EVENTS = {
    "bot.starting", "bot.ready", "bot.signer_ready", "bot.portal_prewarm",
    "bot.portal_prewarm_failed", "telegram.polling_error", "telegram.startup_failed",
    "flow.timing", "flow.timing.summary", "pipeline.timing", "flow.docs_next",
    "flow.docs_next.retry", "flow.docs_next.resaved", "flow.gamma.done",
    "local_signer.range.eligibility", "local_signer.range.used",
    "http.response", *(f"operation.{kind}" for kind in _RUNTIME_OPERATIONS),
}
_PUBLIC_NUMBERS = _TRACE_DURATION_FIELDS | {
    "bytes", "suffix_bytes", "attempt", "accepted", "ok", "headers_s", "body_s", "tcp_s", "tls_s",
}
_PUBLIC_DOC_TYPES = {"1253", "3327", "3328", "3334", "3335", "3359", "3715", "3730"}
_PUBLIC_SPEC_PAGES = {str(page) for page in range(1, 11)}
_PUBLIC_RANGE_VERDICTS = {
    "status", "small_or_unknown_length", "encoded", "no_byte_ranges", "no_strong_etag", "eligible",
}
_PUBLIC_BID_JOURNAL = {
    "flow.docs_next", "flow.docs_next.retry", "flow.docs_next.resaved",
    "local_signer.range.eligibility", "local_signer.range.used",
    "tumar.encrypt.autofill.confirmed", "tumar.encrypt.provider_reply",
}
_PUBLIC_PHASES = {
    "t0", "created", "lots_selected", "lots_done", "docs_signed", "docs_accepted",
    "docs_ready", "gamma_start", "gamma_done", "signed", "dry_run_complete",
    "public_start", "public_done", "bid_finished",
}
_PUBLIC_HTTP_ERRORS = {
    "CancelledError", "ConnectError", "ConnectTimeout", "PoolTimeout", "ProxyError",
    "ReadError", "ReadTimeout", "RemoteProtocolError", "WriteError", "WriteTimeout", "closed",
}
_PUBLIC_RESULTS = {
    "public_ok": "Заявка подана",
    "dry_run_complete": "Черновик готов",
    "submit_declined": "Подача отменена",
    "already_prepared": "Заявка уже подготовлена",
    "public_error": "Портал отклонил заявку",
    "failed": "Попытка завершилась ошибкой",
}


def _public_route(value: str) -> str:
    route = portal_trace_path(value)
    prefix, marker, suffix = route.partition("/application/show_doc/")
    if marker:
        segments = suffix.split("/")
        if len(segments) >= 3 and (doc_type := segments[2]) in _PUBLIC_DOC_TYPES:
            page = segments[4] if len(segments) == 5 else ""
            path = prefix + marker + doc_type
            if doc_type == "3334" and page in _PUBLIC_SPEC_PAGES:
                path += "/" + page
            return path
    return re.sub(r"/[0-9]+(?=/|$)", "", route)


def _public_http_status(status: str) -> str:
    if status in _PUBLIC_HTTP_ERRORS:
        return status
    code, _, error = status.partition("/")
    if re.fullmatch(r"[1-5][0-9]{2}", code):
        return status if error in _PUBLIC_HTTP_ERRORS else code
    return ""


def _public_fields(event: str, fields: tuple[str, ...]) -> Iterator[str]:
    """Keep numeric measurements and known operation labels for public Git."""
    for field in fields:
        name, _, value = field.partition("=")
        if name in _PUBLIC_NUMBERS and re.fullmatch(r"[0-9.]+|True|False", value):
            yield field
        elif name == "route":
            yield f"route={_public_route(value)}"
        elif name == "phase" and value in _PUBLIC_PHASES:
            yield field
        elif name == "step" and value in {"public_ok", "public_error", "dry_run_complete", "docs_ready"}:
            yield field
        elif name == "method" and value in _RUNTIME_HTTP_METHODS:
            yield field
        elif name == "status" and (status := _public_http_status(value)):
            yield f"status={status}"
        elif name == "name" and value in _RUNTIME_OPERATIONS.get(event.removeprefix("operation."), ()):
            yield field
        elif name == "protocol" and re.fullmatch(r"HTTP/[0-9.]{1,3}", value):
            yield field
        elif name == "error_type" and value in {"dns", "other_poller", "conflict", "NetworkError"}:
            yield field
        elif event == "local_signer.range.eligibility":
            if name == "verdict" and value in _PUBLIC_RANGE_VERDICTS:
                yield field
            if name == "source" and value in {"enabled", "disabled"}:
                yield field


def public_bid_report(trace: BidTrace, result: str, scheduled_elapsed: float | None = None) -> str:
    """Render the standard timing report using only public measurements."""
    public = BidTrace()
    public.wall0 = trace.wall0
    public.marks = [(at, phase) for at, phase in trace.marks if phase in _PUBLIC_PHASES]
    for event in trace._events():
        if event.kind in _RUNTIME_HTTP_METHODS:
            name = _public_route(event.name)
        elif event.name in _RUNTIME_OPERATIONS.get(event.kind, ()):
            name = event.name
        else:
            continue
        http = event.http
        if http and not re.fullmatch(r"HTTP/[0-9.]{1,3}", http.version):
            http = http._replace(version="")
        public.events.append(event._replace(name=name, status=_public_http_status(event.status), http=http))
    for elapsed, level, line in trace.logs:
        event, _, fields = line.partition(" ")
        if event in _PUBLIC_BID_JOURNAL:
            public.logs.append((elapsed, level, " ".join([event, *_public_fields(event, tuple(fields.split()))])))

    title = _PUBLIC_RESULTS.get(result, _PUBLIC_RESULTS["failed"])
    if scheduled_elapsed is not None:
        title += f"\nОт T0 по расписанию {scheduled_elapsed:.2f} с"
    else:
        start = next((at for at, phase in public.marks if phase == "t0"), None)
        finish = next((at for at, phase in reversed(public.marks)
                       if phase in {"public_done", "dry_run_complete", "bid_finished"}), None)
        if start is not None and finish is not None:
            title += f"\nОт открытия до завершения {finish - start:.2f} с"
    return public.render(title=title)


class _RuntimeQueueListener(QueueListener):
    def enqueue_sentinel(self) -> None:
        # Shutdown drains the bounded queue even if it was full at close.
        self.queue.put(self._sentinel)


class RuntimeLog:
    """Rotating, sanitized UTF-8 diagnostics independent of the launch console."""

    def __init__(
        self, log_dir: Path, *, max_bytes: int = 2_000_000, backups: int = 3,
        capture_reports: bool = False,
    ):
        log_dir.mkdir(parents=True, exist_ok=True)
        self._handler = RotatingFileHandler(log_dir / "runtime.log", maxBytes=max_bytes,
                                            backupCount=backups, encoding="utf-8", delay=True)
        self._queue: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=4096)
        self._listener = _RuntimeQueueListener(self._queue, self._handler)
        self._closed = False
        self._reports = deque(maxlen=3000) if capture_reports else None
        self._reports_lock = Lock()
        self._listener.start()
        self._sink = self.record_operation
        timing.runtime_event_sink = self._sink

    def _write(self, level: str, event: str, fields: list[str], label: str = "") -> None:
        if self._closed:
            return
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        level = level if level in _LOG_LEVELS else "info"
        entries = [timestamp, level, event]
        if re.fullmatch(r"#[0-9]{1,20}", label):
            entries.append(f"tender={label[1:]}")
        line = " ".join(entries + fields)
        if self._reports is not None:
            with self._reports_lock:
                self._reports.append((timestamp, level, event, tuple(fields), line))
        record = logging.LogRecord("goszakup.runtime", logging.INFO, "", 0,
                                   line, (), None)
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            # A blocked diagnostics disk must not stall bidding.
            pass

    def report_logs(self) -> tuple[str, str]:
        """Return the current run's detailed Telegram log and anonymous Git log."""
        with self._reports_lock:
            events = tuple(self._reports or ())
        detailed = "\n".join(line for _, _, _, _, line in events)
        public = "\n".join(
            " ".join((timestamp, level, event, *_public_fields(event, fields)))
            for timestamp, level, event, fields, _ in events if event in _PUBLIC_EVENTS
        )
        return detailed, public

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
