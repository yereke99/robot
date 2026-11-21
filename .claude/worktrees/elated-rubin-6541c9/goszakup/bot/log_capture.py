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

import time
from typing import Any

import structlog

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
    if not event_name.startswith("pipeline."):
        return event_dict
    line = _humanize(event_name, event_dict)
    if line is None:
        return event_dict
    try:
        pm.update(line)
    except Exception:
        pass
    return event_dict


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
