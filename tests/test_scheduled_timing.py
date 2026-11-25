"""Scheduled elapsed time includes all work after T0, including documents."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from goszakup.bot.handlers import scheduled
from goszakup.client import PortalLoginRequired


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


def _patch_clock(monkeypatch, clock):
    monkeypatch.setattr(scheduled, "_now", clock.now)
    monkeypatch.setattr(scheduled, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(scheduled, "asyncio", SimpleNamespace(
        gather=asyncio.gather, sleep=clock.sleep, Queue=asyncio.Queue,
        create_task=asyncio.create_task, wait=asyncio.wait,
        FIRST_COMPLETED=asyncio.FIRST_COMPLETED,
        timeout=asyncio.timeout, timeout_at=asyncio.timeout_at,
        get_running_loop=asyncio.get_running_loop,
    ))


class _Portal:
    def __init__(self):
        self.cookie = "old-credential"
        self._create_form_html = {}
        self.poll_announce_ready = AsyncMock()
        self.relogin_isolated = AsyncMock()

    def get_cookie(self, name):
        assert name == "ci_session"
        return self.cookie

    def adopt_ci_session(self, fresh):
        self.cookie = fresh


class _Context:
    def __init__(self, *, fast_t0=True):
        self.config = SimpleNamespace(raw={"defaults": {"fast_t0": fast_t0}})
        self.gz_client = None
        self.bid_lock = asyncio.Lock()
        self.portal_session_changed = asyncio.Event()
        self.credential = "old-credential"
        self.signer = None
        self.account_password = None

    def ci_session(self):
        return self.credential

    def set_ci_session(self, fresh):
        self.credential = fresh
        self.portal_session_changed.set()
        self.portal_session_changed = asyncio.Event()


def _new_session(monkeypatch, clock, *, t0_after=300.0, deadline_after=600.0):
    _patch_clock(monkeypatch, clock)
    ctx = _Context()
    portal = _Portal()
    messages = []
    tender = scheduled.ScheduledTender(
        "17058849", clock.started_at + timedelta(seconds=t0_after), [123],
    )
    session = scheduled._ScheduledSession(
        ctx, tender, {"gz": portal, "credential_event": ctx.portal_session_changed, "doc_pool": []},
        messages.append, clock.monotonic() + t0_after, clock.monotonic() + deadline_after,
    )
    return ctx, portal, messages, session


async def _run_scenario(monkeypatch, *, docs_done_at, fire_status="public_ok",
                        wall_jump=0, block_notifications=False, prepare_auth_error=False):
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
        return {"gz": _Portal(), "credential_event": ctx.portal_session_changed, "doc_pool": []}

    async def prepare(ctx, tender, warmed, chat_id, bot, **kwargs):
        events.append("prepare")
        await kwargs["poll_ready"]()
        assert ctx.bid_lock.locked()
        clock.elapsed = docs_done_at
        if prepare_auth_error:
            raise scheduled._ScheduledWriteAuthError("сессия Госзакупок истекла при подготовке заявки.")
        return True, "app-123"

    async def fire(ctx, tender, warmed, chat_id, bot, **kwargs):
        if block_notifications:
            await send_started.wait()
            assert not messages
        assert ctx.bid_lock.locked()
        events.append(("fire", kwargs))
        clock.elapsed += 3
        clock.wall_offset += wall_jump
        fire_finished.set()
        return fire_status, "server error" if fire_status == "public_error" else "app-123"

    _patch_clock(monkeypatch, clock)
    monkeypatch.setattr(scheduled, "probe_portal_session", AsyncMock(return_value="active"))
    monkeypatch.setattr(scheduled, "_warmup_one", warmup)
    monkeypatch.setattr(scheduled, "_prepare_one", prepare)
    monkeypatch.setattr(scheduled, "_fire_one", fire)

    ctx = _Context()
    tender = scheduled.ScheduledTender(
        announce_id="17058849", t0=clock.started_at + timedelta(seconds=30),
        lot_ids=[123],
    )
    await asyncio.wait_for(
        scheduled._run_scheduled(ctx, [tender], 1, SimpleNamespace(send_message=send_message)),
        timeout=2,
    )
    expected = ["warmup", "prepare"]
    if not prepare_auth_error:
        expected.append(("fire", {"resume_app_id": "app-123", "pre_t0_poll": False}))
    assert events == expected
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


async def test_scheduled_auth_expiration_during_prepare_does_not_restart_writes(monkeypatch):
    message = await _run_scenario(monkeypatch, docs_done_at=36, prepare_auth_error=True)
    assert "сессия Госзакупок истекла при подготовке" in message
    assert "/cookie" in message
    assert "не повторял запросы на подачу" in message


async def test_scheduled_checks_cabinet_each_minute_and_before_t0(monkeypatch):
    clock = _Clock()
    ctx, portal, messages, session = _new_session(monkeypatch, clock)
    profile_times = []

    async def probe(gz, *, timeout_s):
        assert gz is portal and 0 < timeout_s <= 5
        assert not ctx.bid_lock.locked()
        profile_times.append(clock.elapsed)
        return "active"

    async def poll(announce_id, *, interval_s, max_wait_s):
        assert not ctx.bid_lock.locked()
        assert announce_id == "17058849" and interval_s > 0
        if clock.elapsed + max_wait_s < 300:
            clock.elapsed += max_wait_s
            # An ambiguous 404 is just not-ready during this polling interval.
            raise TimeoutError
        clock.elapsed = 300

    monkeypatch.setattr(scheduled, "probe_portal_session", probe)
    portal.poll_announce_ready.side_effect = poll
    async with scheduled.AsyncExitStack() as stack:
        await session.wait_for_write(stack, 1300.0, poll_open=True)
        assert ctx.bid_lock.locked()
        assert clock.elapsed == 300
    assert not ctx.bid_lock.locked()
    assert profile_times == [0, 60, 120, 180, 240, 285]
    assert not messages


async def test_scheduled_recovers_same_job_after_sso_redirect(monkeypatch):
    clock = _Clock()
    ctx, portal, messages, session = _new_session(monkeypatch, clock)
    ctx.account_password, ctx.signer = "unused", object()
    monkeypatch.setattr(scheduled, "probe_portal_session", AsyncMock(return_value="active"))
    portal.poll_announce_ready.side_effect = [PortalLoginRequired(), None]
    portal._create_form_html["17058849"] = "old credential HTML"

    async def update_cookie(seconds):
        assert not ctx.bid_lock.locked()
        assert len(messages) == 1 and "/cookie" in messages[0]
        assert "личном чате" in messages[0] and "https://zakup.gov.kz/" in messages[0]
        ctx.set_ci_session("fresh-credential")
        await clock.sleep(seconds)

    monkeypatch.setattr(scheduled.asyncio, "sleep", update_cookie)
    async with scheduled.AsyncExitStack() as stack:
        await session.wait_for_write(stack, 1300.0, poll_open=True)
        assert portal.cookie == "fresh-credential"
        assert "17058849" not in portal._create_form_html
    assert portal.poll_announce_ready.await_count == 2
    portal.relogin_isolated.assert_not_awaited()
    assert "Продолжаю эту запланированную подачу" in messages[1]


@pytest.mark.parametrize("updated_cookie", ["fresh-credential", "old-credential"])
async def test_scheduled_create_probe_is_interrupted_on_verified_cookie_update(monkeypatch, updated_cookie):
    clock = _Clock()
    ctx, portal, messages, session = _new_session(monkeypatch, clock)
    poll_started, old_poll_cancelled = asyncio.Event(), asyncio.Event()
    health = AsyncMock(return_value="active")
    monkeypatch.setattr(scheduled, "probe_portal_session", health)

    async def poll(announce_id, **kwargs):
        if portal.poll_announce_ready.await_count == 1:
            portal.cookie = "anonymous-cookie"  # a rejected portal response may have rotated the job jar
            poll_started.set()
            try:
                await asyncio.Future()  # a 404-only probe would wait out the whole chunk
            finally:
                old_poll_cancelled.set()
        else:
            assert portal.cookie == updated_cookie
            assert old_poll_cancelled.is_set()

    portal.poll_announce_ready.side_effect = poll
    async with scheduled.AsyncExitStack() as stack:
        waiting = asyncio.create_task(session.wait_for_write(stack, 1300.0, poll_open=True))
        await asyncio.wait_for(poll_started.wait(), timeout=1)
        ctx.set_ci_session(updated_cookie)
        await asyncio.wait_for(waiting, timeout=1)
    assert portal.poll_announce_ready.await_count == 2
    assert health.await_count == 2
    assert not messages


@pytest.mark.parametrize("status", ["expired", "sso"])
async def test_scheduled_distinguishes_expiration_from_create_page_404(monkeypatch, status):
    clock = _Clock()
    ctx, portal, messages, session = _new_session(
        monkeypatch, clock, t0_after=30.0, deadline_after=31.0,
    )
    if status == "sso":
        ctx.account_password, ctx.signer = "unused", object()
    monkeypatch.setattr(scheduled, "probe_portal_session", AsyncMock(side_effect=[
        "active", status, status,
    ]))

    async def poll(announce_id, *, max_wait_s, **kwargs):
        clock.elapsed += max_wait_s
        raise TimeoutError  # no opening signal; this cannot prove auth is valid

    portal.poll_announce_ready.side_effect = poll
    async with scheduled.AsyncExitStack() as stack:
        with pytest.raises(scheduled._ScheduledWaitError, match="сессия Госзакупок истекла"):
            await session.wait_for_write(stack, 1030.0, poll_open=True)
        assert not ctx.bid_lock.locked()
    assert portal.poll_announce_ready.await_count == 1
    portal.relogin_isolated.assert_not_awaited()
    assert len(messages) == 1 and "истекла" in messages[0]


async def test_scheduled_network_failure_does_not_claim_cookie_expired(monkeypatch):
    clock = _Clock()
    ctx, portal, messages, session = _new_session(
        monkeypatch, clock, t0_after=10.0, deadline_after=310.0,
    )
    monkeypatch.setattr(scheduled, "probe_portal_session", AsyncMock(return_value="unavailable"))

    async with scheduled.AsyncExitStack() as stack:
        await session.wait_for_write(stack, 1010.0, poll_open=True)
        assert ctx.bid_lock.locked()
    assert portal.poll_announce_ready.await_count == 1
    portal.relogin_isolated.assert_not_awaited()
    assert len(messages) == 1 and "не подтверждает её истечение" in messages[0]
