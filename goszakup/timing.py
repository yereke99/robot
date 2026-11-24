"""Timing configuration and per-bid diagnostics."""

from __future__ import annotations

import contextvars
import math
import re
import time
from collections import Counter, defaultdict
from statistics import median
from typing import Callable, NamedTuple


def poll_interval_s(config: dict) -> float:
    """Return the configured opening-probe interval in seconds."""
    value = config.get("defaults", {}).get("poll_interval_ms", 200)
    try:
        interval_ms = float(value)
    except (TypeError, ValueError):
        raise ValueError("defaults.poll_interval_ms must be a positive number") from None
    if isinstance(value, bool) or not math.isfinite(interval_ms) or interval_ms <= 0:
        raise ValueError("defaults.poll_interval_ms must be a positive finite number")
    return interval_ms / 1000.0


# Keep route names and public application/document identifiers. Download links
# include an authorization token after the file ID, sometimes all digits.
_PORTAL_ROUTES = {
    "application": {
        "index", "create", "lots", "docs", "show_doc", "sign_task", "copy_data_docs",
        "priceoffers", "preview", "ajax_create_application", "ajax_add_lots",
        "ajax_lots_next", "ajax_docs_next", "ajax_get_encr_info", "ajax_add_encrypt",
        "ajax_save_gamma_signs", "ajax_priceoffers_next", "ajax_public_application",
        "ajax_check_priceoffers", "ajax_add_priceoffers",
    },
    "announce": {"index"},
    "beneficiary": {"ajax_save_info", "ajax_show_info"},
    "cabinet": {"profile", "preview"},
    "error_report": {"not_found", "access_denied"},
    "files": {"download_file", "check_status", "upload_file", "upload_file_new"},
    "search": {"announce"},
    "user": {"login", "sendkey", "sendsign", "auth_confirm"},
}


def portal_trace_path(path: str) -> str:
    """Remove queries, opaque path segments and paths outside the portal routes."""
    path = path.partition("?")[0].partition("#")[0].strip("/")
    parts = path.split("/")
    prefix = "/" + parts.pop(0) if parts[0] in {"ru", "kz", "en"} else ""
    if parts == [""] or not parts:
        return prefix + "/"
    if len(parts) < 2 or parts[1] not in _PORTAL_ROUTES.get(parts[0], ()):
        return "/[другой маршрут]"
    category, route, *segments = parts
    if category == "application" and route == "show_doc":
        length = 5
    elif category == "application" and route in {"sign_task", "copy_data_docs"}:
        length = 4
    else:
        length = 2 if category in {"application", "cabinet"} else 1
    safe_parts = [category, route]
    if category in {"application", "announce", "cabinet"} or route == "download_file":
        for segment in segments[:length]:
            if not (segment.isascii() and segment.isdigit() and len(segment) <= 20):
                break
            safe_parts.append(segment)
    return prefix + "/" + "/".join(safe_parts)


class HttpTiming(NamedTuple):
    headers_s: float
    version: str
    bytes_received: int
    tcp_s: float = 0.0
    tls_s: float = 0.0


class TraceEvent(NamedTuple):
    start: float
    duration: float
    kind: str
    name: str
    status: str
    http: HttpTiming | None = None


_HTTP_METHODS = {"GET", "POST", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"}
_FINAL_MARKS = {"public_done", "dry_run_complete", "docs_ready", "bid_finished"}


def _http_elapsed(events: list[TraceEvent], start: float, end: float) -> float:
    """Length of the union of HTTP intervals, clipped to this phase."""
    elapsed = 0.0
    last_end = start
    for event in sorted(events, key=lambda entry: entry.start):
        left = max(start, event.start, last_end)
        right = min(end, event.start + event.duration)
        if right > left:
            elapsed += right - left
            last_end = right
    return elapsed


def _network_lines(events: list[TraceEvent]) -> list[str]:
    received = [event for event in events if event.http and re.fullmatch(r"[1-5][0-9]{2}", event.status)]
    if not received:
        return []
    metrics = [event.http for event in received]
    versions = ", ".join(sorted({item.version for item in metrics if item.version}))
    parts = [
        f"Сеть {versions}" if versions else "Сеть",
        f"до заголовков медиана {median(item.headers_s for item in metrics):.2f} с ({len(metrics)} зап.)",
    ]
    for label, values in (
        ("TCP", [item.tcp_s for item in metrics if item.tcp_s > 0]),
        ("TLS", [item.tls_s for item in metrics if item.tls_s > 0]),
    ):
        if values:
            parts.append(f"{label} {median(values):.3f} с ({len(values)} зам.)")
    lines = [" · ".join(parts)]
    files = [event for event in received if event.kind == "GET" and "/files/download_file" in event.name
             and event.status in {"200", "206"} and event.http.bytes_received >= 256 * 1024
             and event.duration - event.http.headers_s >= 0.05]
    if files:
        biggest = max(files, key=lambda event: event.http.bytes_received)
        size_mb = biggest.http.bytes_received / 1_000_000
        body_s = biggest.duration - biggest.http.headers_s
        lines.append(f"Крупнейшее тело файла · {size_mb:.2f} МБ за {body_s:.2f} с "
                     f"· {size_mb * 8 / body_s:.1f} Мбит/с на этом запросе")
    return lines


def _operation(event: TraceEvent) -> str:
    if event.kind in _HTTP_METHODS:
        parts = event.name.strip("/").split("/")
        if parts[0] in {"ru", "kz", "en"}:
            parts = parts[1:]
        if parts[:2] == ["application", "show_doc"] and len(parts) >= 5:
            name = "/".join([parts[1], *parts[4:]])
        elif parts[:2] == ["files", "download_file"]:
            name = "/".join(parts)
        elif len(parts) >= 2:
            name = parts[1]
        else:
            name = event.name
        return f"{event.kind} {name}"
    if event.kind == "kalkan":
        return "Kalkan + TSA" if event.name == "SIGN_TEXT_TSA" else f"Kalkan {event.name}"
    return event.name


class BidTrace:
    MAX_EVENTS = 20_000

    def __init__(self, label: str = ""):
        self.label = label
        self.t0 = time.monotonic()
        self.wall0 = time.time()
        self.events: list[TraceEvent] = []
        self.marks: list[tuple[float, str]] = []
        self.logs: list[tuple[float, str, str]] = []

    def add(self, kind: str, name: str, started_mono: float, status="", *,
            http: HttpTiming | None = None) -> None:
        now = time.monotonic()
        if len(self.events) >= self.MAX_EVENTS:
            del self.events[:self.MAX_EVENTS // 2]
        if kind in _HTTP_METHODS:
            name = portal_trace_path(name)
        self.events.append(TraceEvent(started_mono - self.t0, now - started_mono,
                                      kind, name, str(status), http))

    def log(self, level: str, text: str) -> None:
        if len(self.logs) < 3000:
            self.logs.append((time.monotonic() - self.t0, level, text))

    def mark(self, phase: str) -> None:
        self.marks.append((time.monotonic() - self.t0, phase))

    def _events(self) -> list[TraceEvent]:
        # The first five fields are the original event format.
        return [event if isinstance(event, TraceEvent) else TraceEvent(*event)
                for event in self.events]

    def _phases(self, origin: float, end: float) -> list[tuple[str, float, float, int]]:
        marks = dict((name, when) for when, name in self.marks)
        phases = []
        for name, begin, finish, limit in (
            ("Заявка и лоты", "t0", "lots_done", 5),
            ("Документы", "lots_done", "docs_accepted", 5),
            ("Цена", "gamma_start", "gamma_done", 8),
            ("Публикация", "public_start", "public_done", 1),
        ):
            left = marks.get(begin)
            right = marks.get(finish, end)
            if left is not None and right > left:
                # On a failure, show only the phase that was actually started.
                if name == "Заявка и лоты" and (
                    "resume_price" in marks or "gamma_start" in marks and finish not in marks
                ):
                    continue
                phases.append((name, left, right, limit))
        return phases or [("Операции", origin, end, 8)]

    def render_summary(self, *, max_chars: int = 1800) -> str:
        """Plain text for the final status. Durations of parallel requests overlap."""
        events = self._events()
        if not events or max_chars <= 0:
            return ""
        origin = next((when for when, phase in self.marks if phase == "t0"), 0.0)
        end = next((when for when, phase in reversed(self.marks) if phase in _FINAL_MARKS),
                   max(event.start + event.duration for event in events))
        current = [event for event in events if origin <= event.start < end]
        requests = [event for event in events if event.kind in _HTTP_METHODS
                    and event.start < end and event.start + event.duration > origin]
        if not current and not requests:
            return ""
        duration = _http_elapsed(requests, origin, end)
        lines = [f"HTTP после старта подачи · {len(requests)} зап. · {duration:.2f} с без двойного счёта"]
        lines.extend(_network_lines([event for event in requests if event.start >= origin]))
        polls = [event for event in requests if "/ajax_docs_next/" in event.name + "/"]
        marks = {name: when for when, name in self.marks}
        if "public_start" in marks and "public_done" not in marks:
            lines.append(f"Начало запроса публикации · {marks['public_start'] - origin:.2f} с · "
                         "ответ портала не получен")
        if polls and "docs_signed" in marks:
            check_start = marks["docs_signed"]
            check_end = marks.get("docs_accepted", end)
            retry_s = sum(event.duration for event in current
                          if event.kind == "wait" and event.name == "Повтор проверки подписей")
            lines.append(f"Проверка подписей · {check_end - check_start:.2f} с · "
                         f"{len(polls)} POST · паузы {retry_s:.2f} с")
        for phase, start, finish, limit in self._phases(origin, end):
            phase_events = [event for event in current if start <= event.start < finish]
            if not phase_events:
                continue
            phase_http = [event for event in requests if event.start < finish
                          and event.start + event.duration > start]
            busy = _http_elapsed(phase_http, start, finish)
            lines.append(f"{phase} · {finish - start:.2f} с · HTTP {len(phase_http)} зап. / {busy:.2f} с")
            labels = Counter(_operation(event) for event in phase_events)
            slowest = {}
            for event in sorted(phase_events, key=lambda entry: -entry.duration):
                slowest.setdefault(_operation(event), event)
            if phase == "Цена":
                priority = ("GET priceoffers", "POST ajax_get_encr_info", "EncryptOfferPrice (окно цены)",
                            "POST ajax_add_encrypt", "Kalkan + TSA", "POST ajax_save_gamma_signs",
                            "POST ajax_priceoffers_next")
                order = dict.fromkeys([*priority, *slowest])
                selected_events = [(label, slowest[label]) for label in order if label in slowest][:limit]
            else:
                selected_events = list(slowest.items())[:limit]
            for label, event in selected_events:
                count = f" (макс. из {labels[label]})" if labels[label] > 1 else ""
                status = f" [{event.status}]" if event.status else ""
                lines.append(f"  {label[:70]} {event.duration:.2f} с{status}{count}")
        selected = []
        available = max_chars
        for line in lines:
            if len(line) > available:
                break
            selected.append(line)
            available -= len(line) + 1
        return "\n".join(selected)

    def render(self, *, title: str = "", runtime: str = "") -> str:
        events = self._events()
        opened = next((t for t, name in self.marks if name == "t0"), None)
        base = opened if opened is not None else 0.0
        origin = "обнаружения открытия" if opened is not None else "старта"
        lines = [title or self.label,
                 time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(self.wall0)), ""]
        if runtime:
            lines.extend(["СРЕДА ЗАПУСКА", runtime, "Запросы портала выполняет Python/httpx", ""])
        marks = {name: at for at, name in self.marks}
        public_start = marks.get("public_start")
        public_done = marks.get("public_done")
        if public_start is not None and public_done is not None:
            lines.extend([f"Начало запроса публикации через {public_start - base:.2f} с от {origin}",
                          f"Ответ на запрос публикации через {public_done - public_start:.2f} с", ""])
        overview = self.render_summary()
        if overview:
            lines.extend(["ОБЗОР", overview, ""])
        if opened is not None:
            before = [e for e in events if e.start < base and e.kind in _HTTP_METHODS]
            lines.extend([f"ДО ОБНАРУЖЕНИЯ ОТКРЫТИЯ · {len(before)} HTTP-запросов · "
                          f"{_http_elapsed(before, 0.0, base):.2f} с без двойного счёта", ""])
        lines.append(f"ЭТАПЫ (сек от {origin})")
        lines.extend(f"{t - base:8.2f}  {name}" for t, name in self.marks)
        lines.extend(["", f"ОПЕРАЦИИ · старт от {origin}, полная длительность, тип, статус, маршрут",
                      "HTTP · время до заголовков, время и размер тела до распаковки"])
        for event in sorted(events, key=lambda entry: entry.start):
            if event.start < base - 5.0:
                continue
            line = (f"{event.start - base:8.2f} {event.duration:6.2f}  {event.kind:<8} "
                    f"{event.status:<4} {event.name[:120]}")
            if event.http:
                metrics = event.http
                body_s = max(0.0, event.duration - metrics.headers_s)
                line += (f" | {metrics.version} · заголовки {metrics.headers_s:.3f} с "
                         f"· тело {body_s:.3f} с / {metrics.bytes_received} Б")
                if metrics.tcp_s or metrics.tls_s:
                    line += f" · TCP {metrics.tcp_s:.3f} с · TLS {metrics.tls_s:.3f} с"
            lines.append(line)

        after = [event for event in events if event.start >= base]
        counts = Counter(event.kind for event in after)
        seconds: dict[str, float] = defaultdict(float)
        for event in after:
            seconds[event.kind] += event.duration
        lines.extend(["", "ПО ТИПАМ · количество и сумма длительностей с пересечениями"])
        lines.extend(f"  {kind:<8} {counts[kind]:>4}  {elapsed:8.2f} с"
                     for kind, elapsed in sorted(seconds.items(), key=lambda item: -item[1]))
        lines.extend(["", "САМЫЕ ДОЛГИЕ"])
        lines.extend(f"  {event.duration:6.2f}  {event.kind} {event.name[:110]}"
                     for event in sorted(after, key=lambda entry: -entry.duration)[:10])
        journal = [entry for entry in self.logs if entry[0] >= base - 5.0]
        if journal:
            lines.extend(["", "ЖУРНАЛ (предупреждения и ошибки отмечены !)"])
            lines.extend(f"{t - base:8.2f} {'!' if lvl in ('warning', 'error', 'critical') else ' '} {txt[:300]}"
                         for t, lvl, txt in journal[-600:])
        return "\n".join(lines) + "\n"


current_trace: contextvars.ContextVar[BidTrace | None] = contextvars.ContextVar(
    "current_trace", default=None)

# Installed only by the Telegram bot. The client and crypto providers also run
# as CLI tools without a runtime log.
RuntimeEventSink = Callable[[str, str, float, str, HttpTiming | None, str], None]
runtime_event_sink: RuntimeEventSink | None = None


def emit_runtime_event(kind: str, name: str, started_mono: float, status="", *,
                       http: HttpTiming | None = None) -> None:
    sink = runtime_event_sink
    if sink is None:
        return
    trace = current_trace.get()
    try:
        sink(kind, name, time.monotonic() - started_mono, str(status), http,
             trace.label if trace else "")
    except Exception:
        # Diagnostics cannot interrupt an in-flight request or signing.
        pass


def trace_event(kind: str, name: str, started_mono: float, status="") -> None:
    trace = current_trace.get()
    if trace is not None:
        trace.add(kind, name, started_mono, status)
    emit_runtime_event(kind, name, started_mono, status)


def trace_mark(phase: str) -> None:
    trace = current_trace.get()
    if trace is not None:
        trace.mark(phase)
