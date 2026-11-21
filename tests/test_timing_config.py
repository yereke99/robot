"""Timing settings and startup warmup use mocks, with no external services."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram.error import NetworkError
from websockets.protocol import State

from goszakup import popup_filler, tumar
from goszakup.bot import main
from goszakup.bot.handlers import bid, scheduled
from goszakup.state import SubmissionState
from goszakup.timing import poll_interval_s


@pytest.mark.parametrize("raw, expected", [
    ({}, 0.2),
    ({"defaults": {"poll_interval_ms": 200}}, 0.2),
    ({"defaults": {"poll_interval_ms": "350"}}, 0.35),
])
def test_poll_interval_reads_milliseconds(raw, expected):
    assert poll_interval_s(raw) == expected


@pytest.mark.parametrize("value", [0, -1, True, None, "bad", float("inf"), float("nan")])
def test_invalid_poll_interval_is_rejected(value):
    with pytest.raises(ValueError, match="defaults.poll_interval_ms"):
        poll_interval_s({"defaults": {"poll_interval_ms": value}})


@pytest.fixture
def fake_tumar(monkeypatch):
    class FakeTumar:
        instances = []
        fail_connect = False

        def __init__(self, **kwargs):
            self._ws = None
            self.version = "test"
            self.connects = 0
            self.closed = False
            self.instances.append(self)

        async def connect(self):
            self.connects += 1
            self._ws = SimpleNamespace(state=State.OPEN)
            if self.fail_connect:
                raise RuntimeError("test provider unavailable")

        async def close(self):
            self.closed = True
            self._ws = None

    monkeypatch.setattr(tumar, "TumarCSP", FakeTumar)
    monkeypatch.setattr(popup_filler, "is_supported", lambda: False)
    return FakeTumar


async def _startup(monkeypatch, defaults, *, menu_error=None):
    """Call real startup while replacing every service, secret and disk read."""
    monkeypatch.setattr(popup_filler, "is_elevated", lambda: True)
    client = SimpleNamespace(_http=SimpleNamespace(head=AsyncMock()))
    monkeypatch.setattr(main, "GoszakupClient", Mock(return_value=client))
    ctx = SimpleNamespace(
        config=SimpleNamespace(
            raw={"defaults": defaults, "tumarcsp": {"api_key": "test-only"}},
            base_url="https://invalid.test", proxy_url=None,
            telegram=SimpleNamespace(allowed_user_ids=[1]),
        ),
        signer=SimpleNamespace(start=AsyncMock()),
        ci_session=Mock(return_value="test-cookie"),
        agreement_cookie=Mock(return_value=None),
        history=SimpleNamespace(mark_interrupted_pending=Mock(return_value=0)),
        load_caches=Mock(), current_bid=None, bid_lock=asyncio.Lock(), tumar=None,
    )
    app = SimpleNamespace(bot_data={"ctx": ctx},
                          bot=SimpleNamespace(set_my_commands=AsyncMock(side_effect=menu_error)))
    try:
        await main._post_init(app)
    finally:
        task = app.bot_data.get("heartbeat_task")
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    return ctx


async def test_startup_tumar_is_reused_on_first_bid(monkeypatch, fake_tumar):
    ctx = await _startup(monkeypatch, {"pipeline_type": "repairs", "pre_t0_warmup": True})
    assert len(fake_tumar.instances) == 1
    warmed = ctx.tumar
    assert await bid._ensure_tumar(ctx, ctx.config.raw["tumarcsp"]) is warmed
    assert len(fake_tumar.instances) == 1
    assert warmed.connects == 1


async def test_transient_telegram_menu_failure_does_not_block_startup(monkeypatch, fake_tumar):
    ctx = await _startup(monkeypatch, {"pipeline_type": "repairs"},
                         menu_error=NetworkError("getaddrinfo failed"))
    assert ctx.tumar is fake_tumar.instances[0]


@pytest.mark.parametrize("defaults", [
    {"pipeline_type": "repairs", "pre_t0_warmup": False},
    {"pipeline_type": "catering", "pre_t0_warmup": True},
])
async def test_startup_warmup_respects_optout_and_pipeline(monkeypatch, fake_tumar, defaults):
    ctx = await _startup(monkeypatch, defaults)
    assert ctx.tumar is None
    assert fake_tumar.instances == []


async def test_closed_startup_connection_is_replaced(monkeypatch, fake_tumar):
    ctx = await _startup(monkeypatch, {"pipeline_type": "repairs"})
    old = ctx.tumar
    old._ws.state = State.CLOSED
    replacement = await bid._ensure_tumar(ctx, ctx.config.raw["tumarcsp"])
    assert old.closed
    assert replacement is not old
    assert replacement is ctx.tumar
    assert replacement._ws.state == State.OPEN
    assert len(fake_tumar.instances) == 2


async def test_failed_startup_warmup_closes_and_can_retry(monkeypatch, fake_tumar):
    fake_tumar.fail_connect = True
    ctx = await _startup(monkeypatch, {"pipeline_type": "repairs"})
    assert ctx.tumar is None
    assert fake_tumar.instances[0].closed
    fake_tumar.fail_connect = False
    assert await bid._ensure_tumar(ctx, ctx.config.raw["tumarcsp"]) is ctx.tumar
    assert len(fake_tumar.instances) == 2


async def test_cancelled_warmup_closes_unpublished_connection(monkeypatch, fake_tumar):
    monkeypatch.setattr(fake_tumar, "connect", AsyncMock(side_effect=asyncio.CancelledError))
    ctx = SimpleNamespace(tumar=None)
    with pytest.raises(asyncio.CancelledError):
        await bid._ensure_tumar(ctx, {"api_key": "test-only"})
    assert ctx.tumar is None
    assert fake_tumar.instances[0].closed


@pytest.mark.parametrize("override, expected", [(None, 0.35), (0.1, 0.1)])
@pytest.mark.parametrize("socket_state", [State.OPEN, State.CLOSED])
async def test_bid_forwards_configured_or_explicit_poll_interval(
    monkeypatch, override, expected, socket_state,
):
    ctx = SimpleNamespace(
        config=SimpleNamespace(
            raw={"defaults": {"pipeline_type": "repairs", "poll_interval_ms": 350}, "company": {}},
            state_dir=None, base_url="https://invalid.test", proxy_url=None,
        ),
        history=SimpleNamespace(insert_pending=Mock(return_value=1), finish=Mock()),
        signer=object(), account_password=None, ci_session=lambda: "test-cookie",
        agreement_cookie=lambda: None, get_doc_pool_if_fresh=Mock(return_value=[]),
        save_doc_pool=Mock(), save_caches=Mock(), lp_id_cache={}, tz_sig_cache={},
        gz_client=object(), tumar=SimpleNamespace(_ws=SimpleNamespace(state=socket_state)),
    )
    monkeypatch.setattr(bid, "StateStore", Mock())
    monkeypatch.setattr(bid, "_ensure_logged_in", AsyncMock(return_value=True))
    open_provider = AsyncMock()
    monkeypatch.setattr(bid, "_ensure_tumar", open_provider)
    submit = AsyncMock(return_value=SimpleNamespace(step="dry_run_complete", application_id="test-app"))
    monkeypatch.setattr(bid.flow_repairs, "submit_bid", submit)
    ok, _, _, _ = await bid._run_pipeline(
        ctx, 1, 1, None, object(), announce_id="17058849", lot_ids=[123],
        price=0, dry_run=True, poll_interval_s=override,
    )
    assert ok
    assert submit.await_args.kwargs["poll_interval_s"] == expected
    expected_provider = ctx.tumar if socket_state == State.OPEN else None
    assert submit.await_args.kwargs["params"].tumar is expected_provider
    open_provider.assert_not_awaited()


@pytest.mark.parametrize("phase", ["prepare", "fire"])
async def test_schedule_forwards_configured_poll_interval(monkeypatch, phase):
    ctx = SimpleNamespace(
        config=SimpleNamespace(
            raw={"defaults": {"poll_interval_ms": 350}, "company": {}},
            state_dir=None, base_url="https://invalid.test", proxy_url=None,
        ),
        bid_settings={}, signer=object(), account_password=None,
        ci_session=lambda: "test-cookie", agreement_cookie=lambda: None,
        lp_id_cache={}, tz_sig_cache={},
    )
    monkeypatch.setattr(scheduled, "StateStore", Mock())
    result = SimpleNamespace(
        step="docs_ready" if phase == "prepare" else "public_ok",
        application_id="test-app", error=None,
    )
    submit = AsyncMock(return_value=result)
    monkeypatch.setattr(scheduled.flow_repairs, "submit_bid", submit)
    tender = SimpleNamespace(announce_id="17058849", lot_ids=[123])
    warmed = {"gz": object(), "doc_pool": []}
    if phase == "prepare":
        ok, _ = await scheduled._prepare_one(
            ctx, tender, warmed, 1, object(), max_poll_wait_s=10, notify=Mock(),
        )
        assert ok
    else:
        status, _ = await scheduled._fire_one(ctx, tender, warmed, 1, object())
        assert status == "public_ok"
    assert submit.await_args.kwargs["poll_interval_s"] == 0.35


# --- "Время подачи" measures the bid, not the wait --------------------------------
# The result message used to report one number taken from the moment the command
# was handled — so a bid queued before the tender opened reported its waiting time
# as submission time (the "44.1 сек" case).

def test_timing_text_reports_time_from_open_with_phase_breakdown():
    bid._BID_TIMESTAMPS[7] = {
        "start": 1000.0, "t0": 1030.2, "created": 1031.0, "lots_done": 1032.2,
        "docs_signed": 1038.0, "docs_accepted": 1039.0, "gamma_done": 1042.5,
        "signed": 1042.5, "public_start": 1042.5, "public_done": 1043.3,
    }
    text = bid._timing_text(7, 44.1)
    assert "От открытия до подачи:</b> 13.1 сек" in text
    assert "заявка+лоты 2.0 · документы 6.8 · цена 3.5 · отправка 0.8" in text
    assert "Начало запроса публикации: 12.3 сек · ответ портала: 0.8 сек" in text
    assert "Ждал открытия тендера: 30.2 сек" in text
    assert "44.1" not in text                # the old inflated figure is gone
    assert 7 not in bid._BID_TIMESTAMPS      # consumed


def test_timing_text_hides_a_negligible_wait():
    bid._BID_TIMESTAMPS[8] = {
        "start": 0.0, "t0": 0.4, "lots_done": 2.0, "docs_accepted": 7.0,
        "gamma_done": 9.0, "public_done": 9.6,
    }
    text = bid._timing_text(8, 9.9)
    assert "9.2 сек" in text
    assert "Ждал открытия" not in text       # tender was already open


def test_timing_text_dry_run_wording():
    bid._BID_TIMESTAMPS[9] = {
        "start": 0.0, "t0": 1.0, "lots_done": 3.0, "docs_accepted": 8.0,
        "gamma_done": 11.0, "signed": 11.0, "dry_run_complete": 11.0,
    }
    text = bid._timing_text(9, 12.0, dry=True)
    assert "От открытия до готовности:</b> 10.0 сек" in text
    assert "отправка" not in text


def test_timing_text_falls_back_when_no_timestamps():
    assert bid._timing_text(12345, 3.2) == "⏱ <b>Время:</b> 3.2 сек"
    assert bid._timing_text(None, 3.2) == "⏱ <b>Время:</b> 3.2 сек"


def test_timing_text_escapes_diagnostics_and_bounds_the_result(monkeypatch):
    summary = Mock(return_value="POST ajax_docs_next 0.42 с <400>\n" + "&" * 800 + "\n")
    trace = SimpleNamespace(render_summary=summary)
    monkeypatch.setitem(bid._BID_TRACES, 12345, trace)
    text = bid._timing_text(12345, 3.2, failed=True)

    summary.assert_called_once_with(max_chars=1800)
    assert "Запросы и задержки" in text
    assert "POST ajax_docs_next 0.42 с &lt;400&gt;" in text
    assert "&amp;" not in text
    assert "Полный отчёт доступен через /timing." in text
    assert bid._BID_TRACES.get(12345) is trace  # the file report still needs it


async def test_telegram_progress_keeps_the_result_with_a_long_history():
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    progress = bid.ProgressMessage.start(bot, 1, "Старый статус", header="<b>Подача</b>")
    for i in range(40):
        progress.update(f"Статус {i} " + "📋" * 70)
    await progress.finish("✅ Принято\n<b>Запросы и задержки</b>\nPOST ajax_public_application")

    text = bot.send_message.await_args.kwargs["text"]
    assert len(text.encode("utf-16-le")) // 2 <= 3900
    assert text.startswith("<b>Подача</b>") and "Старый статус" not in text
    assert text.endswith("POST ajax_public_application")
    assert "✅ Принято\n<b>Запросы и задержки</b>" in text


@pytest.mark.parametrize("outcome", ["success", "generation_lost", "save_timeout", "old_draft"])
async def test_run_pipeline_hands_timestamps_and_draft_to_the_result_message(monkeypatch, tmp_path, outcome):
    ctx = SimpleNamespace(
        config=SimpleNamespace(
            raw={"defaults": {"pipeline_type": "repairs", "poll_interval_ms": 200}, "company": {}},
            state_dir=tmp_path, base_url="https://invalid.test", proxy_url=None,
        ),
        history=SimpleNamespace(insert_pending=Mock(return_value=41), finish=Mock()),
        signer=object(), account_password=None, ci_session=lambda: "test-cookie",
        agreement_cookie=lambda: None, get_doc_pool_if_fresh=Mock(return_value=[]),
        save_doc_pool=Mock(), save_caches=Mock(), invalidate_doc_pool=Mock(),
        lp_id_cache={}, tz_sig_cache={},
        gz_client=object(),
    )
    monkeypatch.setattr(bid, "_ensure_logged_in", AsyncMock(return_value=True))
    monkeypatch.setattr(bid, "_ensure_tumar", AsyncMock(return_value=None))
    stamps = {"start": 0.0, "t0": 20.0, "lots_done": 22.0, "docs_accepted": 27.0,
              "gamma_done": 30.0, "public_done": 31.0}

    announce_id = "17905992"
    older_state = SubmissionState(announce_id=announce_id, application_id="42",
                                  timestamps={"created": 1.0})
    bid.StateStore(tmp_path, namespace="1").save(older_state)

    async def submit(*, state_store, **_kwargs):
        if outcome == "success":
            return SimpleNamespace(step="public_ok", application_id="12345", timestamps=stamps, error=None)

        trace = gz_timing.current_trace.get()
        trace.marks.append((2.4, "t0"))
        monkeypatch.setattr(trace, "mark", lambda phase: trace.marks.append((8.88, phase)))
        if outcome == "generation_lost":
            raise bid.flow_repairs.DocumentGenerationUnconfirmed("12345", 3730)
        if outcome == "save_timeout":
            state_store.save(SubmissionState(announce_id=announce_id, application_id="12345",
                                             timestamps={"created": time.time()}))
        raise httpx.ReadTimeout("")

    monkeypatch.setattr(bid.flow_repairs, "submit_bid", submit)
    ok, app_id, error, row_id = await bid._run_pipeline(
        ctx, 1, 1, None, object(), announce_id=announce_id, lot_ids=[1],
        price=0, dry_run=False,
    )
    assert app_id == (None if outcome == "old_draft" else "12345") and row_id == 41
    assert ctx.history.finish.call_args.kwargs["app_id"] == app_id
    text = bid._timing_text(row_id, 31.5, failed=not ok)
    if outcome == "success":
        assert ok
        assert "От открытия до подачи:</b> 11.0 сек" in text
        assert "Ждал открытия тендера: 20.0 сек" in text
    else:
        assert not ok
        if outcome == "generation_lost":
            assert "3730" in error
        else:
            assert error == "ReadTimeout"
        assert ctx.history.finish.call_args.kwargs["error"] == error
        assert "От открытия до остановки:</b> 6.5 сек" in text


async def test_bid_progress_does_not_delay_portal_or_hold_the_bid_lock(monkeypatch):
    send_started = asyncio.Event()
    submitted = asyncio.Event()
    ctx = SimpleNamespace(
        bid_lock=asyncio.Lock(), current_bid=None, queue_waiters=0,
        bid_settings={"dry_run": False},
        config=SimpleNamespace(raw={"defaults": {"pipeline_type": "repairs"}}),
    )

    async def send_message(**kwargs):
        if kwargs["text"].startswith("🚀"):
            send_started.set()
            await submitted.wait()
        return SimpleNamespace(message_id=42)

    async def edit_message_text(**kwargs):
        assert not ctx.bid_lock.locked()
        assert "Заявка успешно подана" in kwargs["text"]
        assert "Цена за единицу: 80% от плановой" in kwargs["text"]
        assert "Запросы и задержки" in kwargs["text"]
        assert "ajax_public_application" in kwargs["text"]

    async def run_pipeline(ctx, user_id, chat_id, username, pm, **kwargs):
        await send_started.wait()
        assert ctx.bid_lock.locked()
        assert ctx.current_bid.announce_id == "1"
        pm.update("📨 Отправляю заявку…")
        trace = gz_timing.BidTrace("#1")
        trace.marks.append((0.0, "t0"))
        trace.events.append((0.1, 1.2, "POST", "/ru/application/ajax_public_application/1/42", "200"))
        monkeypatch.setitem(bid._BID_TRACES, 0, trace)
        submitted.set()
        return True, "42", None, 0

    monkeypatch.setattr(bid, "_run_pipeline", run_pipeline)
    bot = SimpleNamespace(send_message=send_message, edit_message_text=edit_message_text,
                          send_document=AsyncMock())
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"ctx": ctx}), bot=bot, user_data={},
    )
    await asyncio.wait_for(
        bid._do_submit(None, context, 1, 1, None, "1", [2]), timeout=2,
    )
    assert submitted.is_set()
    assert ctx.current_bid is None
    assert bot.send_document.await_count == 1


async def test_multi_bid_adds_each_tenders_own_diagnostics_to_its_result(monkeypatch):
    outcomes = {"1": (True, "41", "dry_run_complete", 321),
                "2": (False, None, "Портал вернул <500>", 322)}
    progress = {announce_id: SimpleNamespace(update=Mock(), finish=AsyncMock(), cancel=Mock())
                for announce_id in outcomes}

    async def run_pipeline(*args, announce_id, **kwargs):
        ok, app_id, message, row_id = outcomes[announce_id]
        trace = gz_timing.BidTrace(f"#{announce_id}")
        route = "ajax_priceoffers_next" if ok else "ajax_docs_next"
        trace.events.append((0.1, 1.2, "POST", f"/ru/application/{route}/{announce_id}", "200"))
        monkeypatch.setitem(bid._BID_TRACES, row_id, trace)
        return ok, app_id, message, row_id

    monkeypatch.setattr(bid, "_run_pipeline", run_pipeline)
    monkeypatch.setattr(bid.ProgressMessage, "start", Mock(side_effect=progress.values()))
    monkeypatch.setattr(bid, "_send_timing_report", AsyncMock())
    ctx = SimpleNamespace(
        gz_client=SimpleNamespace(discover_announce_lots=AsyncMock(return_value=[{"lot_id": 42}])),
        bid_settings={"dry_run": True},
    )
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"ctx": ctx}), bot=object())
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=3),
                             effective_user=SimpleNamespace(id=3, username=None))

    await bid._multi_bid(update, context, list(outcomes))

    ready = progress["1"].finish.await_args.args[0]
    failed = progress["2"].finish.await_args.args[0]
    assert "dry-run OK" in ready and "ajax_priceoffers_next" in ready
    assert "Портал вернул &lt;500&gt;" in failed and "ajax_docs_next" in failed
    assert "ajax_docs_next" not in ready and "ajax_priceoffers_next" not in failed


# --- per-request timing trace (sent to Telegram after each bid) --------------------

import httpx
import respx

from goszakup import timing as gz_timing
from goszakup.client import GoszakupClient


async def test_bid_trace_records_every_http_request():
    trace = gz_timing.BidTrace("#1")
    token = gz_timing.current_trace.set(trace)
    try:
        async with respx.mock(base_url="https://v3bl.goszakup.gov.kz",
                              assert_all_called=False) as r:
            r.get("/ru/cabinet/profile").mock(return_value=httpx.Response(200, html="ok"))
            r.post(url__regex=r".*/ajax_docs_next/1/2").mock(
                return_value=httpx.Response(200, json={"status": "ok"}))
            async with GoszakupClient(ci_session="dummy") as gz:
                await gz._http.get("/ru/cabinet/profile")
                await gz.docs_next("1", "2")
    finally:
        gz_timing.current_trace.reset(token)
    assert [(e[2], e[4]) for e in trace.events] == [("GET", "200"), ("POST", "200")]
    assert "ajax_docs_next/1/2" in trace.events[1][3]
    assert all(e[1] >= 0 for e in trace.events)


def test_bid_trace_is_a_noop_outside_a_bid():
    gz_timing.trace_event("GET", "/x", 0.0, 200)     # no active trace: nothing kept, no error
    gz_timing.trace_mark("t0")


def test_bid_trace_render_is_relative_to_tender_open():
    trace = gz_timing.BidTrace("#17905992")
    trace.events.append((1.0, 0.5, "GET", "/ru/application/create/1", "302"))   # long before open
    trace.marks.append((30.0, "t0"))
    trace.events.append((30.1, 0.7, "POST", "/ru/application/ajax_create_application/1", "200"))
    trace.events.append((31.0, 1.6, "GET", "/ru/application/show_doc/1/2/3334", "200"))
    trace.marks.append((40.0, "public_done"))
    text = trace.render(title="Тендер #17905992 · app 2")
    assert "Тендер #17905992" in text
    assert "    0.10   0.70  POST" in text           # start shown from the opening
    assert "/ru/application/create/1" not in text    # pre-open polling left out
    assert "САМЫЕ ДОЛГИЕ" in text and "show_doc/1/2/3334" in text


async def test_timing_report_is_sent_as_a_file_and_kept_for_the_command(tmp_path):
    trace = gz_timing.BidTrace("#17905992")
    trace.marks.append((0.0, "t0"))
    trace.events.append((0.1, 0.7, "POST", "/ru/application/ajax_create_application/1", "200"))
    bid._BID_TRACES[77] = trace
    bot = SimpleNamespace(send_document=AsyncMock())
    ctx = SimpleNamespace(config=SimpleNamespace(raw={"defaults": {}}, log_dir=tmp_path))
    await bid._send_timing_report(ctx, bot, 555, 77, "17905992", "73859197")
    assert bot.send_document.await_count == 1
    assert bot.send_document.await_args.kwargs["chat_id"] == 555
    name, text = bid._LAST_REPORT[555]
    assert name.startswith("timing_17905992_") and "ajax_create_application" in text
    assert (tmp_path / "timing" / name).read_text(encoding="utf-8") == text   # copy on disk
    assert 77 not in bid._BID_TRACES


async def test_timing_report_can_be_switched_off(tmp_path):
    bid._BID_TRACES[78] = gz_timing.BidTrace("#1")
    bot = SimpleNamespace(send_document=AsyncMock())
    ctx = SimpleNamespace(config=SimpleNamespace(
        raw={"defaults": {"send_timing_report": False}}, log_dir=tmp_path))
    await bid._send_timing_report(ctx, bot, 556, 78, "1", None)
    assert bot.send_document.await_count == 0
    assert 556 in bid._LAST_REPORT                   # still available via /timing


async def test_timing_report_also_goes_to_report_chat_ids_and_carries_the_journal(tmp_path):
    trace = gz_timing.BidTrace("#1")
    trace.log("warning", "flow.docs_next.retry message=Подпишите ... Приложение 15")
    bid._BID_TRACES[79] = trace
    bot = SimpleNamespace(send_document=AsyncMock())
    ctx = SimpleNamespace(config=SimpleNamespace(
        raw={"defaults": {}, "telegram": {"report_chat_ids": [800, 557]}}, log_dir=tmp_path))
    await bid._send_timing_report(ctx, bot, 557, 79, "1", "2")
    assert [c.kwargs["chat_id"] for c in bot.send_document.await_args_list] == [557, 800]
    report = bid._LAST_REPORT[557][1]
    assert "ЖУРНАЛ" in report and "Приложение 15" in report


async def test_timing_report_survives_an_unreachable_recipient(tmp_path):
    bid._BID_TRACES[80] = gz_timing.BidTrace("#1")
    sent = []

    async def send_document(**kw):
        if kw["chat_id"] == 800:
            raise RuntimeError("Forbidden: bot can't initiate conversation with a user")
        sent.append(kw["chat_id"])
    ctx = SimpleNamespace(config=SimpleNamespace(
        raw={"defaults": {}, "telegram": {"report_chat_ids": [800, 801]}}, log_dir=tmp_path))
    await bid._send_timing_report(ctx, SimpleNamespace(send_document=send_document),
                                  558, 80, "1", "2")
    assert sent == [558, 801]            # the one that failed didn't stop the others


def test_trace_processor_copies_log_lines_but_not_secrets():
    from goszakup.bot import log_capture
    trace = gz_timing.BidTrace("#1")
    token = gz_timing.current_trace.set(trace)
    try:
        log_capture.trace_processor(None, "warning", {
            "event": "flow.docs_next.retry", "level": "warning",
            "message": "Подпишите", "attempt": 1})
        log_capture.trace_processor(None, "info", {
            "event": "login.cookies", "cookies": {"ci_session": "SECRET"}})
        log_capture.trace_processor(None, "info", {
            "event": "flow.get_encr_info.raw", "resp": "SALT-SECRET", "lp_id": 5})
        log_capture.trace_processor(None, "warning", {
            "event": "flow.docs_next.retry", "body": "BODY-SECRET",
            "message": "Подпишите Приложение 15 - MESSAGE-SECRET", "err": "EXC-SECRET",
            "dl": "/files/download_file/123/987654321/?token=QUERY-SECRET"})
        log_capture.trace_processor(None, "info", {"event": "http.post", "path": "/x"})
    finally:
        gz_timing.current_trace.reset(token)
    text = "\n".join(line for _, _, line in trace.logs)
    assert "flow.docs_next.retry" in text and "Подпишите" in text
    assert "SECRET" not in text and "987654321" not in text and "http.post" not in text
    assert "Приложение 15" in text and "dl=/files/download_file/123" in text
    assert "lp_id=5" in text
    assert log_capture.trace_processor(None, "info", {"event": "x"}) == {"event": "x"}   # no bid: no-op


async def test_bid_trace_measures_reading_the_body_without_prefetching(monkeypatch):
    import time
    from goszakup import client

    seconds = 10.0
    clock = SimpleNamespace(monotonic=lambda: seconds, time=lambda: seconds,
                            strftime=time.strftime, localtime=time.localtime)
    monkeypatch.setattr(client, "time", clock)
    monkeypatch.setattr(gz_timing, "time", clock)

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal seconds
            seconds += 1.5
            yield b"payload"

    async def response(_transport, request):
        nonlocal seconds
        connection_trace = request.extensions["trace"]
        await connection_trace("connection.connect_tcp.started", {})
        seconds += 0.1
        await connection_trace("connection.connect_tcp.complete", {})
        await connection_trace("connection.start_tls.started", {})
        seconds += 0.2
        await connection_trace("connection.start_tls.complete", {})
        seconds += 0.3
        return httpx.Response(200, stream=Body(), extensions={"http_version": b"HTTP/2"})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", response)
    trace = gz_timing.BidTrace()
    token = gz_timing.current_trace.set(trace)
    try:
        async with GoszakupClient() as gz:
            async with gz._http.stream("GET", "/files/download_file/123/456789?token=SECRET") as reply:
                assert trace.events == []
                assert await reply.aread() == b"payload"
    finally:
        gz_timing.current_trace.reset(token)
    (event,) = trace.events
    assert event.name == "/files/download_file/123"
    assert event.duration == pytest.approx(2.1)
    assert event.http.headers_s == pytest.approx(0.6)
    assert event.http.bytes_received == 7
    assert event.http.version == "HTTP/2"
    assert event.http.tcp_s == pytest.approx(0.1)
    assert event.http.tls_s == pytest.approx(0.2)
    assert "тело 1.500 с / 7 Б" in trace.render()


async def test_bid_trace_reports_transport_failures_without_error_payload(monkeypatch):
    async def response(_transport, _request):
        raise httpx.ConnectTimeout("token=SECRET")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", response)
    trace = gz_timing.BidTrace()
    token = gz_timing.current_trace.set(trace)
    try:
        async with GoszakupClient() as gz:
            with pytest.raises(httpx.ConnectTimeout):
                await gz._http.get("/ru/application/docs/1/2?key=SECRET")
    finally:
        gz_timing.current_trace.reset(token)
    (event,) = trace.events
    assert event.status == "ConnectTimeout"
    assert event.name == "/ru/application/docs/1/2"
    assert "SECRET" not in trace.render()


async def test_bid_trace_records_one_read_failure_and_closes_the_body(monkeypatch):
    closed = False

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial"
            raise httpx.ReadError("SECRET")

        async def aclose(self):
            nonlocal closed
            closed = True

    async def response(_transport, _request):
        return httpx.Response(200, stream=Body())

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", response)
    trace = gz_timing.BidTrace()
    token = gz_timing.current_trace.set(trace)
    try:
        async with GoszakupClient() as gz:
            with pytest.raises(httpx.ReadError):
                await gz._http.get("/ru/application/docs/1/2")
    finally:
        gz_timing.current_trace.reset(token)
    (event,) = trace.events
    assert closed
    assert event.status == "200/ReadError" and event.http.bytes_received == 7
    assert "SECRET" not in trace.render()


def test_publication_timeout_still_reports_when_the_request_started():
    trace = gz_timing.BidTrace()
    trace.marks.extend([(2.0, "t0"), (24.95, "public_start"), (30.26, "bid_finished")])
    trace.events.append(gz_timing.TraceEvent(
        24.95, 5.31, "POST", "/ru/application/ajax_public_application/1/2", "ReadTimeout",
    ))
    expected = "Начало запроса публикации · 22.95 с · ответ портала не получен"
    assert expected in trace.render_summary()
    assert expected in trace.render()


def test_bid_trace_summary_uses_wall_time_for_overlapping_requests():
    event = gz_timing.TraceEvent
    trace = gz_timing.BidTrace()
    trace.marks.extend([(2.0, "t0"), (4.0, "lots_done"), (8.0, "docs_signed"),
                        (11.0, "docs_accepted"), (11.0, "gamma_start"),
                        (15.0, "gamma_done"), (15.0, "public_start"), (16.0, "public_done")])
    trace.events.extend([
        event(0.0, 1.0, "GET", "/ru/user/login", "200"),
        event(2.0, 1.5, "POST", "/ru/application/ajax_create_application/1", "200"),
        event(4.0, 3.0, "GET", "/ru/application/docs/1/2", "200"),
        event(5.0, 3.0, "GET", "/ru/application/show_doc/1/2/3334", "200"),
        event(8.0, 1.0, "POST", "/ru/application/ajax_docs_next/1/2", "200"),
        event(9.0, 1.0, "wait", "Повтор проверки подписей", ""),
        event(10.0, 1.0, "POST", "/ru/application/ajax_docs_next/1/2", "200"),
        event(11.0, 0.6, "GET", "/ru/application/priceoffers/1/2", "200"),
        event(11.6, 0.6, "POST", "/ru/application/ajax_get_encr_info/1/2", "200"),
        event(12.2, 0.1, "tumar", "EncryptOfferPrice (окно цены)", ""),
        event(12.3, 0.6, "POST", "/ru/application/ajax_add_encrypt/1/2", "200"),
        event(12.3, 0.2, "kalkan", "SIGN_TEXT_TSA", ""),
        event(13.0, 0.6, "POST", "/ru/application/ajax_save_gamma_signs/1/2", "200"),
        event(13.6, 0.6, "POST", "/ru/application/ajax_priceoffers_next/1/2", "200"),
        event(15.0, 1.0, "POST", "/ru/application/ajax_public_application/1/2", "200"),
    ])
    summary = trace.render_summary()
    assert "11 зап. · 11.50 с без двойного счёта" in summary
    assert "Проверка подписей · 3.00 с · 2 POST · паузы 1.00 с" in summary
    assert "EncryptOfferPrice (окно цены) 0.10 с" in summary and "Kalkan + TSA 0.20 с" in summary
    assert "ajax_save_gamma_signs" in summary and "ajax_priceoffers_next" in summary
    assert "user/login" not in summary
    assert len(trace.render_summary(max_chars=100)) <= 100


async def test_runtime_log_records_safe_phases_and_http_without_a_bid_trace(tmp_path):
    from goszakup.bot.log_capture import RuntimeLog

    runtime = RuntimeLog(tmp_path)
    try:
        event = {"event": "flow.timing", "phase": "docs_signed", "dt_s": 4.5,
                 "body": "BODY-SECRET", "err": "EXCEPTION-SECRET",
                 "cookies": {"ci_session": "COOKIE-SECRET"}}
        assert runtime.process(None, "info", event) == event
        gz_timing.trace_event("wait", "Повтор проверки подписей", gz_timing.time.monotonic())
        gz_timing.trace_event("wait", "UNTRUSTED-SECRET", gz_timing.time.monotonic())

        async with respx.mock(base_url="https://v3bl.goszakup.gov.kz",
                              assert_all_called=False) as router:
            router.get(url__regex=r".*/download_file/123/987654321").mock(
                return_value=httpx.Response(200, text="HTTP-SECRET"))
            async with GoszakupClient(ci_session="AUTH-SECRET") as gz:
                await gz._http.get("/files/download_file/123/987654321?token=QUERY-SECRET")
    finally:
        runtime.close()
    text = (tmp_path / "runtime.log").read_text(encoding="utf-8")
    assert "+00:00 info flow.timing phase=docs_signed dt_s=4.5" in text
    assert "operation.wait name=Повтор проверки подписей duration_s=" in text
    assert "http.response method=GET route=/files/download_file/123 duration_s=" in text
    assert "status=200" in text and "headers_s=" in text and "body_s=" in text
    assert "987654321" not in text and "SECRET" not in text
    assert gz_timing.runtime_event_sink is None


def test_runtime_log_rotates_and_flushes_on_close(tmp_path):
    from goszakup.bot.log_capture import RuntimeLog

    runtime = RuntimeLog(tmp_path, max_bytes=140, backups=2)
    try:
        for attempt in range(10):
            runtime.process(None, "info", {"event": "poll.tick", "attempt": attempt})
    finally:
        runtime.close()
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "runtime.log", "runtime.log.1", "runtime.log.2",
    ]
    assert "attempt=9" in (tmp_path / "runtime.log").read_text(encoding="utf-8")
