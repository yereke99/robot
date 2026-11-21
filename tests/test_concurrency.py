"""Cross-tender concurrency guards.

The price popup is filled by typing at FIXED screen coordinates, so with several
tenders running at once the popups MUST be serialized process-wide: only one on
screen at a time, each carrying its own price, spaced >= 1.5s apart. These tests
pin that gate (goszakup/tumar.py::_popup_gate).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from websockets.protocol import State

from goszakup import tumar
from goszakup.local_signer import KalkanSigner


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
import threading
import time
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


def _as_windows(monkeypatch, *, find):
    monkeypatch.setattr(popup_filler.sys, "platform", "win32")
    monkeypatch.setattr(popup_filler, "is_elevated", lambda: True)
    monkeypatch.setattr(popup_filler, "_find_popup_hwnd", find)

    def _never(*a, **k):
        raise AssertionError("must not type at fallback coordinates after stop")
    monkeypatch.setattr(popup_filler, "_fill_via_coords", _never)


def test_filler_returns_at_once_when_already_stopped(monkeypatch):
    def find(_needles):
        raise AssertionError("must not even look for the window")
    _as_windows(monkeypatch, find=find)
    stop = threading.Event()
    stop.set()
    assert popup_filler.fill_price_popup(80.0, timeout_s=30.0, stop=stop,
                                         field_xy=[1, 1]) is False


def test_filler_stops_searching_mid_loop(monkeypatch):
    _as_windows(monkeypatch, find=lambda _needles: None)   # window never appears
    stop = threading.Event()
    threading.Timer(0.15, stop.set).start()
    started = time.time()
    ok = popup_filler.fill_price_popup(80.0, timeout_s=30.0, stop=stop, field_xy=[1, 1])
    assert ok is False
    assert time.time() - started < 2.0     # left the 30s search almost immediately


@pytest.mark.parametrize(
    "color_appears, color_disappears, stop_at, first_click_before",
    [
        (0.1, float("inf"), float("inf"), 0.4),  # a newly visible popup
        (0.0, float("inf"), float("inf"), 3.0),  # the baseline matches already
        (0.1, 0.13, 0.4, None),                 # the color vanishes during settling
    ],
)
def test_popup_color_can_enable_early_coordinates(
    monkeypatch, color_appears, color_disappears, stop_at, first_click_before,
):
    monkeypatch.setattr(popup_filler.sys, "platform", "win32")
    monkeypatch.setattr(popup_filler, "is_elevated", lambda: True)
    monkeypatch.setattr(popup_filler, "_find_popup_hwnd", lambda _: None)
    monkeypatch.setattr(popup_filler, "_PRE_TYPE_SETTLE_S", 0.05)
    monkeypatch.setattr(popup_filler, "_set_clipboard", lambda _: False)
    clock = 0.0
    stop = threading.Event()
    clicks = []
    typed = []

    def sleep(duration):
        nonlocal clock
        clock += duration
        if clock >= stop_at:
            stop.set()

    def pixel(_x, _y):
        return (240, 240, 240) if color_appears <= clock < color_disappears else (10, 10, 10)

    monkeypatch.setattr(popup_filler, "time", SimpleNamespace(time=lambda: clock, sleep=sleep))
    monkeypatch.setattr(popup_filler, "_screen_pixel", pixel)
    monkeypatch.setitem(popup_filler.sys.modules, "pyautogui", SimpleNamespace(
        click=lambda x, y: clicks.append((clock, x, y)),
        hotkey=lambda *args: None,
        typewrite=lambda value, **kwargs: typed.append(value),
        press=lambda _: None,
    ))

    filled = popup_filler.fill_price_popup(
        80.0, field_xy=[1, 1], ok_xy=[2, 2], detect_xy=[3, 3],
        detect_rgb=[240, 240, 240], timeout_s=4.0, stop=stop,
    )

    if first_click_before is None:
        assert not filled
        assert clicks == typed == []
    else:
        assert filled
        first_click = clicks[0][0]
        assert color_appears <= first_click < first_click_before
        if color_appears == 0:
            assert first_click >= 2.5
        assert typed == ["80.00"]


def test_make_autofiller_advertises_stop_support():
    fill = popup_filler.make_autofiller()
    assert getattr(fill, "supports_stop", False) is True
