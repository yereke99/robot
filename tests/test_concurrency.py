"""Cross-tender concurrency guards.

The price popup is filled by typing at FIXED screen coordinates, so with several
tenders running at once the popups MUST be serialized process-wide: only one on
screen at a time, each carrying its own price, spaced >= 1.5s apart. These tests
pin that gate (goszakup/tumar.py::_popup_gate).
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from websockets.protocol import State

from goszakup import flow, tumar
from goszakup.client import GoszakupClient
from goszakup.local_signer import KalkanSigner
from goszakup.state import StateStore


async def test_idle_provider_reconnects_once_for_concurrent_bids(monkeypatch):
    provider = tumar.TumarCSP(api_key="unused")
    closed = 0
    connected = 0

    async def close():
        nonlocal closed
        closed += 1

    async def connect():
        nonlocal connected
        connected += 1
        await asyncio.sleep(0)
        provider._ws = SimpleNamespace(state=State.OPEN)

    provider._ws = SimpleNamespace(state=State.CLOSED, close=close)
    monkeypatch.setattr(provider, "connect", connect)
    await asyncio.gather(provider.ensure_connected(), provider.ensure_connected())
    assert provider.connected
    assert (closed, connected) == (1, 1)


async def test_disconnected_provider_reconnects_while_price_page_loads(monkeypatch):
    provider = tumar.TumarCSP(api_key="unused", autofill=lambda _: False)
    provider.version = "1.0.0.0"
    reconnect_started = asyncio.Event()
    page_loaded = asyncio.Event()

    async def connect():
        reconnect_started.set()
        await page_loaded.wait()
        provider._ws = SimpleNamespace(state=State.OPEN)
        provider.version = "2.0.0.0"

    async def get_page(path):
        assert path == "/ru/application/priceoffers/100/200"
        await reconnect_started.wait()
        page_loaded.set()
        return ('<input id="public_key" value="KEY">'
                '<input app_lot_id="300" buy_lot_point_id="400">')

    async def get_encr_info(announce, app, *, lp_id, version):
        assert page_loaded.is_set() and provider.connected
        assert (announce, app, lp_id, version) == ("100", "200", 400, "2.0.0.0")
        return {"plnSum": 100, "minPrice": 80, "salt": "fresh", "sign": "SIGN"}

    monkeypatch.setattr(provider, "connect", AsyncMock(side_effect=connect))
    monkeypatch.setattr(provider, "encrypt_offer_price", AsyncMock(
        return_value=tumar.EncryptResult(encrypted_data="ENC", session_key="SESSION",
                                        info="SN", sign="SIGN")))
    client = SimpleNamespace(_get_page=get_page, get_encr_info=AsyncMock(side_effect=get_encr_info),
                             add_encrypt=AsyncMock(), save_gamma_signs=AsyncMock(),
                             priceoffers_next_gamma=AsyncMock())
    signer = SimpleNamespace(sign_xml_batch=AsyncMock(return_value={400: "CMS"}))
    params = flow.AtagosBidParams(announce_id="100", tender_lot_ids=[1], price=None, tumar=provider)

    async with AsyncExitStack() as stack:
        result = await asyncio.wait_for(flow._gamma_price(client, signer, params, "200", stack), 2)

    assert result == ([400], {400: 80})
    provider.connect.assert_awaited_once()
    client.get_encr_info.assert_awaited_once()
    assert client.add_encrypt.await_args.kwargs["salt"] == "fresh"


@pytest.mark.parametrize("candidate", [
    "valid", "expired", "invalid", "changed_point", "changed_csrf", "changed_provider",
])
async def test_repeat_tender_prefetches_fresh_gamma_info_while_price_page_loads(monkeypatch, candidate):
    price_started = asyncio.Event()
    prefetch_started = asyncio.Event()
    prefetch_finished = asyncio.Event()
    requests = []
    provider = tumar.TumarCSP(api_key="unused", autofill=lambda _: False)
    provider._ws = SimpleNamespace(state=State.OPEN)
    provider.version = "2.0.0.0"
    monkeypatch.setattr(provider, "encrypt_offer_price", AsyncMock(
        return_value=tumar.EncryptResult(encrypted_data="ENC", session_key="SESSION",
                                        info="SN", sign="SIGN")))
    signer = SimpleNamespace(sign_xml_batch=AsyncMock(return_value={400: "CMS"}))
    params = flow.AtagosBidParams(announce_id="100", tender_lot_ids=[1], tumar=provider)
    base_url = "https://portal.test"

    async def price_page(request):
        price_started.set()
        await prefetch_started.wait()
        headers = {"set-cookie": "csrf_cookie=new; Path=/"} if candidate == "changed_csrf" else {}
        return httpx.Response(
            200, headers=headers,
            html='<input id="public_key" value="CURRENT-KEY">'
                 '<input app_lot_id="300" buy_lot_point_id="400">',
        )

    async def get_encr_info(request):
        payload = parse_qs(request.content.decode())
        point, = payload["lpId"]
        version = "3.0.0.0" if requests and candidate == "changed_provider" else "2.0.0.0"
        assert payload["version"] == [version]
        requests.append((point, request.headers.get("cookie")))
        if len(requests) == 1:
            prefetch_started.set()
            try:
                await price_started.wait()
                if candidate == "expired":
                    return httpx.Response(
                        302, headers={"location": "/ru/user/login",
                                      "set-cookie": "ci_session=expired; Path=/"})
                if candidate == "invalid":
                    return httpx.Response(200, json={"info": "5_999;100;80;", "salt": "bad"})
                if candidate == "changed_provider":
                    provider.version = "3.0.0.0"
                return httpx.Response(200, json={"info": f"5_{point};100;80;",
                                                "salt": "prefetched", "sign": "SIGN"})
            finally:
                prefetch_finished.set()
        assert prefetch_finished.is_set()
        return httpx.Response(200, json={"info": "5_400;100;80;", "salt": "normal", "sign": "SIGN"})

    async with respx.mock(base_url=base_url, assert_all_called=False) as router:
        router.get("/ru/application/priceoffers/100/200").mock(side_effect=price_page)
        router.post("/ru/application/ajax_get_encr_info/100/200").mock(side_effect=get_encr_info)
        async with GoszakupClient(base_url=base_url, ci_session="ci_session=original; csrf_cookie=old") as client:
            for method in ("add_encrypt", "save_gamma_signs", "priceoffers_next_gamma"):
                monkeypatch.setattr(client, method, AsyncMock(return_value=httpx.Response(200, json={"status": "ok"})))
            async with AsyncExitStack() as stack:
                result = await asyncio.wait_for(flow._gamma_price(
                    client, signer, params, "200", stack,
                    cached_lp_id=401 if candidate == "changed_point" else 400,
                ), 2)

            assert result == ([400], {400: 80})
            assert client.get_cookie("ci_session") == "original"
            provider_info = provider.encrypt_offer_price.await_args.args[0]
            assert provider_info.public_key == "CURRENT-KEY"
            assert provider_info.id_priceoffer == "5_400"
            assert client.add_encrypt.await_args.kwargs["salt"] == (
                "prefetched" if candidate == "valid" else "normal")
    expected_points = {
        "valid": ["400"],
        "changed_point": ["401", "400"],
    }.get(candidate, ["400", "400"])
    assert [point for point, _ in requests] == expected_points
    if candidate == "changed_csrf":
        assert "csrf_cookie=new" in requests[-1][1]


def test_gamma_point_cache_key_requires_exact_single_selected_lot():
    assert flow._single_lot_price_cache_key("100", [2, 2]) == "100:2"
    assert flow._single_lot_price_cache_key("100", [2]) != flow._single_lot_price_cache_key("100", [3])
    assert flow._single_lot_price_cache_key("100", [2]) != flow._single_lot_price_cache_key("300", [2])
    assert flow._single_lot_price_cache_key("100", [2, 3]) is None


@pytest.mark.parametrize(("stop_after_docs", "warmup_fails", "attempts"), [
    (False, False, 1), (False, True, 2), (True, False, 0),
])
async def test_bid_reconnects_during_draft_creation(monkeypatch, tmp_path, stop_after_docs,
                                                     warmup_fails, attempts):
    provider = tumar.TumarCSP(api_key="unused", autofill=lambda _: False)
    provider._ws = SimpleNamespace(state=State.OPEN)
    reconnect_started = asyncio.Event()
    price_page_loaded = asyncio.Event()

    async def poll(*args, **kwargs):
        provider._ws = None

    async def connect():
        if not reconnect_started.is_set():
            reconnect_started.set()
            await price_page_loaded.wait()
            if warmup_fails:
                raise tumar.TumarError("provider temporarily unavailable")
        provider._ws = SimpleNamespace(state=State.OPEN)
        provider.version = "2.0.0.0"

    async def create(*args, **kwargs):
        if not stop_after_docs:
            await reconnect_started.wait()
        return "200"

    async def get_page(path):
        if path.endswith("/3334"):
            return '<input data-app-lot-id="300">'
        if path == "/ru/application/docs/100/200":
            return ""
        assert path == "/ru/application/priceoffers/100/200"
        price_page_loaded.set()
        return ('<input id="public_key" value="KEY">'
                '<input app_lot_id="300" buy_lot_point_id="400">')

    mock_connect = AsyncMock(side_effect=connect)
    monkeypatch.setattr(provider, "connect", mock_connect)
    monkeypatch.setattr(provider, "encrypt_offer_price", AsyncMock(
        return_value=tumar.EncryptResult(encrypted_data="ENC", session_key="SESSION",
                                        info="SN", sign="SIGN")))
    client = SimpleNamespace(
        _http=None, _get_page=get_page, poll_announce_ready=poll, create_application=create,
        add_lots=AsyncMock(), lots_next=AsyncMock(),
        docs_next=AsyncMock(return_value=httpx.Response(200, json={"status": "ok"})),
        get_encr_info=AsyncMock(return_value={"plnSum": 100, "minPrice": 80,
                                              "salt": "fresh", "sign": "SIGN"}),
        add_encrypt=AsyncMock(), save_gamma_signs=AsyncMock(), priceoffers_next_gamma=AsyncMock(),
    )
    params = flow.AtagosBidParams(
        announce_id="100", tender_lot_ids=[1], dry_run=True, stop_after_docs=stop_after_docs,
        tumar=provider, signer=SimpleNamespace(sign_text_with_tsa=AsyncMock(return_value="CMS")),
        doc_plan=[],
    )
    cache = {"100": [401]}
    state = await asyncio.wait_for(flow.submit_bid(
        params=params, ci_session="dummy", gz_client=client,
        company={"subject_address": "address", "iik": "account"},
        beneficiary=None, state_store=StateStore(tmp_path),
        lp_id_cache=cache,
    ), timeout=2)

    assert state.step == ("docs_ready" if stop_after_docs else "dry_run_complete")
    assert mock_connect.await_count == attempts
    if stop_after_docs:
        client.get_encr_info.assert_not_awaited()
        assert cache == {"100": [401]}
    else:
        client.get_encr_info.assert_awaited_once_with("100", "200", lp_id=400, version="2.0.0.0")
        assert cache == {"100": [401], "100:1": [400]}


async def test_cancelled_signing_drains_its_reply_before_the_next_command():
    commands: asyncio.Queue[bytes] = asyncio.Queue()
    replies = asyncio.StreamReader()

    async def drain():
        pass

    signer = KalkanSigner(p12_path="unused.p12", password="unused")
    signer._proc = SimpleNamespace(
        stdin=SimpleNamespace(write=commands.put_nowait, drain=drain),
        stdout=replies,
    )

    first = asyncio.create_task(signer.sign_bytes(b"first"))
    assert await asyncio.wait_for(commands.get(), 1) == b"SIGN_BYTES Zmlyc3Q=\n"
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    second = asyncio.create_task(signer.sign_bytes(b"second"))
    replies.feed_data(b"OK first-signature\n")
    assert await asyncio.wait_for(commands.get(), 1) == b"SIGN_BYTES c2Vjb25k\n"
    replies.feed_data(b"OK second-signature\n")
    assert await asyncio.wait_for(second, 1) == "second-signature"


async def test_popup_gate_serializes_concurrent_holders(monkeypatch):
    """Three tenders hitting the price step at once -> never two popups at once."""
    # Neutralize the spacing sleep so the test is fast; the LOCK is what we check.
    async def _fast(_d):
        return None
    monkeypatch.setattr(tumar.asyncio, "sleep", _fast)
    loop = asyncio.get_event_loop()
    tumar._popup_last_release = loop.time() - 1000.0   # far past -> no spacing wait

    active = 0
    max_active = 0

    async def worker(label):
        nonlocal active, max_active
        async with tumar._popup_gate(label=label):
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0)          # real yield: lets siblings try to enter
            await asyncio.sleep(0)
            active -= 1

    await asyncio.gather(*(worker(x) for x in "ABC"))
    assert max_active == 1                  # exclusive: one popup driven at a time


async def test_popup_gate_enforces_minimum_spacing(monkeypatch):
    """A popup that 'just' closed forces the next one to wait out the spacing."""
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def _rec(d):
        sleeps.append(d)
        await real_sleep(0)                 # record the requested delay, don't burn it
    monkeypatch.setattr(tumar.asyncio, "sleep", _rec)

    loop = asyncio.get_event_loop()
    tumar._popup_last_release = loop.time()  # previous popup released right now

    async with tumar._popup_gate(label="X"):
        pass

    assert sleeps, "expected a spacing wait before opening the next popup"
    assert max(sleeps) >= tumar._POPUP_MIN_SPACING_S - 0.2


async def test_popup_gate_no_wait_when_idle(monkeypatch):
    """No artificial delay when nothing ran recently (single-tender fast path)."""
    waits: list[float] = []

    async def _rec(d):
        waits.append(d)
        await asyncio.sleep(0)
    monkeypatch.setattr(tumar.asyncio, "sleep", _rec)

    loop = asyncio.get_event_loop()
    tumar._popup_last_release = loop.time() - 1000.0   # long idle

    async with tumar._popup_gate(label="solo"):
        pass

    assert all(w <= 0.01 for w in waits)   # gate did not impose a spacing sleep


# --- the popup filler stops when TumarCSP has answered -----------------------------
# EncryptOfferPrice returning means the popup is finished (filled by us, typed by
# hand, or it never opened because TumarCSP returned an error). The filler thread
# used to keep searching for up to 60s and then type the price blind at the
# fallback coordinates — into whatever window was in front by then.

import json
from goszakup import popup_filler


class _FakeWs:
    def __init__(self, reply: dict, delay_s: float = 0.0):
        self.reply = reply
        self.delay_s = delay_s
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return json.dumps(self.reply)


def _info():
    return tumar.EncrInfo(sign="s", salt="salt", pl_sum=100.0, d_sum=80.0,
                          id_priceoffer="1_2", public_key="PK")


@pytest.mark.parametrize("failure", [tumar.TumarError("bad API key"), asyncio.CancelledError()])
async def test_connect_closes_websocket_when_handshake_fails(monkeypatch, failure):
    closed = False

    async def close():
        nonlocal closed
        closed = True

    async def connect(*args, **kwargs):
        return SimpleNamespace(close=close)

    async def set_api_key():
        raise failure

    t = tumar.TumarCSP(api_key="k")
    monkeypatch.setattr(tumar.websockets, "connect", connect)
    monkeypatch.setattr(t, "_set_api_key", set_api_key)
    with pytest.raises(type(failure)):
        async with t:
            pytest.fail("accepted an unsuccessful handshake")
    assert closed
    assert t._ws is None


async def test_encrypt_signals_the_filler_to_stop():
    loop = asyncio.get_event_loop()
    tumar._popup_last_release = loop.time() - 1000.0
    seen = {}

    def autofill(price, stop):
        seen["price"] = price
        seen["stopped"] = stop.wait(timeout=5.0)      # "window never found"
        return False
    autofill.supports_stop = True

    t = tumar.TumarCSP(api_key="k", autofill=autofill)
    t._ws = _FakeWs({"result": "true", "encryptData": "ENC", "encryptKey": "KEY",
                     "sn": "SN", "sign": "SIGN", "code": "0"})
    started = loop.time()
    result = await t.encrypt_offer_price(_info())
    elapsed = loop.time() - started

    assert result.encrypted_data == "ENC"
    assert seen == {"price": 80.0, "stopped": True}  # told to stop, with OUR price
    assert elapsed < 2.0                               # no 5-second wait on the filler


async def test_encrypt_supports_a_plain_filler_without_stop():
    """A filler without ``supports_stop`` (older / custom callable) still works."""
    loop = asyncio.get_event_loop()
    tumar._popup_last_release = loop.time() - 1000.0
    calls = []

    def autofill(price):
        calls.append(price)
        return True

    t = tumar.TumarCSP(api_key="k", autofill=autofill)
    t._ws = _FakeWs({"result": "true", "encryptData": "ENC", "encryptKey": "KEY"})
    result = await t.encrypt_offer_price(_info())
    assert result.session_key == "KEY"
    assert calls == [80.0]


async def test_encrypt_error_stops_the_filler_too():
    """TumarCSP rejects the call (no popup ever opens): the filler must be released
    instead of hunting for a window that will never appear."""
    loop = asyncio.get_event_loop()
    tumar._popup_last_release = loop.time() - 1000.0
    seen = {}

    def autofill(price, stop):
        seen["stopped"] = stop.wait(timeout=5.0)
        return False
    autofill.supports_stop = True

    t = tumar.TumarCSP(api_key="k", autofill=autofill)
    t._ws = _FakeWs({"result": "false", "error": "No such node (public_key)"})
    with pytest.raises(tumar.TumarError):
        await t.encrypt_offer_price(_info())
    assert seen == {"stopped": True}


async def test_encrypt_send_failure_stops_the_filler(monkeypatch):
    loop = asyncio.get_event_loop()
    monkeypatch.setattr(tumar, "_popup_last_release", loop.time() - 1000.0)
    seen = {}

    def autofill(price, stop):
        seen["stopped"] = stop.wait(timeout=5.0)
        return False

    async def fail_send(_raw):
        raise OSError("connection closed")

    autofill.supports_stop = True
    t = tumar.TumarCSP(api_key="k", autofill=autofill)
    t._ws = SimpleNamespace(send=fail_send)
    with pytest.raises(OSError, match="connection closed"):
        await t.encrypt_offer_price(_info())
    assert seen == {"stopped": True}


def test_make_autofiller_advertises_stop_support():
    fill = popup_filler.make_autofiller()
    assert getattr(fill, "supports_stop", False) is True
    assert getattr(fill, "requires_confirmation", False) is True
