"""Scheduled elapsed time includes all work after T0, including documents."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from goszakup.bot.handlers import scheduled


class _Clock:
    def __init__(self):
        self.started_at = datetime(2026, 10, 31, 9, 0)
        self.elapsed = 0.0
        self.wall_offset = 0.0

    def now(self):
        return self.started_at + timedelta(seconds=self.elapsed + self.wall_offset)

    def monotonic(self):
        return 1000.0 + self.elapsed

    async def sleep(self, seconds):
        self.elapsed += seconds


async def _run_scenario(monkeypatch, *, docs_done_at, fire_status="public_ok",
                        wall_jump=0, block_notifications=False):
    clock = _Clock()
    messages = []
    events = []
    send_started = asyncio.Event()
    fire_finished = asyncio.Event()

    async def send_message(**kwargs):
        if block_notifications:
            send_started.set()
            await fire_finished.wait()
        messages.append(kwargs["text"])

    async def warmup(ctx, tender, stack):
        events.append("warmup")
        clock.elapsed += 2
        return {}

    async def prepare(ctx, tender, warmed, chat_id, bot, **kwargs):
        events.append("prepare")
        clock.elapsed = docs_done_at
        return True, "app-123"

    async def fire(ctx, tender, warmed, chat_id, bot, **kwargs):
        if block_notifications:
            await send_started.wait()
            assert not messages
        events.append(("fire", kwargs))
        clock.elapsed += 3
        clock.wall_offset += wall_jump
        fire_finished.set()
        return fire_status, "server error" if fire_status == "public_error" else "app-123"

    monkeypatch.setattr(scheduled, "_now", clock.now)
    monkeypatch.setattr(scheduled, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(scheduled, "asyncio", SimpleNamespace(
        gather=asyncio.gather, sleep=clock.sleep, Queue=asyncio.Queue,
        create_task=asyncio.create_task,
    ))
    monkeypatch.setattr(scheduled, "_warmup_one", warmup)
    monkeypatch.setattr(scheduled, "_prepare_one", prepare)
    monkeypatch.setattr(scheduled, "_fire_one", fire)

    ctx = SimpleNamespace(config=SimpleNamespace(raw={"defaults": {"fast_t0": True}}))
    tender = scheduled.ScheduledTender(
        announce_id="17058849", t0=clock.started_at + timedelta(seconds=30),
        lot_ids=[123],
    )
    await asyncio.wait_for(
        scheduled._run_scheduled(ctx, [tender], 1, SimpleNamespace(send_message=send_message)),
        timeout=2,
    )
    assert events == ["warmup", "prepare", ("fire", {
        "resume_app_id": "app-123", "pre_t0_poll": False,
    })]
    return messages[-1]


@pytest.mark.parametrize("fire_status", ["public_ok", "public_error"])
async def test_scheduled_timing_counts_documents_after_t0(monkeypatch, fire_status):
    # Documents finish 6 seconds after T0; the existing fire phase takes 3 more.
    message = await _run_scenario(monkeypatch, docs_done_at=36, fire_status=fire_status)
    assert "9.0с от T0" in message
    assert "фаза подачи: 3.0с" in message


@pytest.mark.parametrize("fire_status", ["public_ok", "public_error"])
async def test_scheduled_timing_excludes_waiting_before_t0(monkeypatch, fire_status):
    # Prepared early: time spent warming, preparing and waiting before T0 is excluded.
    message = await _run_scenario(monkeypatch, docs_done_at=6, fire_status=fire_status)
    assert "3.0с от T0" in message
    assert "фаза подачи: 3.0с" in message


async def test_scheduled_timing_is_stable_when_wall_clock_changes(monkeypatch):
    message = await _run_scenario(monkeypatch, docs_done_at=36, wall_jump=3600)
    assert "9.0с от T0" in message
    assert "фаза подачи: 3.0с" in message


async def test_scheduled_submission_runs_while_telegram_is_unavailable(monkeypatch):
    message = await _run_scenario(monkeypatch, docs_done_at=36, block_notifications=True)
    assert "заявка подана!" in message
    assert "9.0с от T0" in message
    assert "фаза подачи: 3.0с" in message


async def test_scheduled_dry_run_reports_a_draft(monkeypatch):
    message = await _run_scenario(monkeypatch, docs_done_at=36, fire_status="dry_run_complete")
    assert "черновик готов (dry-run)" in message
    assert "app <code>app-123</code>" in message
    assert "заявка подана" not in message
