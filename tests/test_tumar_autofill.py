"""Confirm the automated price is accepted before returning the sealed offer."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
from websockets.protocol import State

from goszakup import tumar


class _Socket:
    def __init__(self, *, reply_immediately: bool = False) -> None:
        self.state = State.OPEN
        self.closed = False
        self.recv_started = threading.Event()
        self.reply_delivered = asyncio.Event()
        self.recv_cancelled = False
        self._can_reply = asyncio.Event()
        if reply_immediately:
            self._can_reply.set()
        self.sent = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def recv(self) -> str:
        self.recv_started.set()
        try:
            await self._can_reply.wait()
        except asyncio.CancelledError:
            self.recv_cancelled = True
            raise
        self.reply_delivered.set()
        return json.dumps({"result": True, "encryptData": "sealed", "encryptKey": "key"})

    async def close(self) -> None:
        self.closed = True
        self.state = State.CLOSED


def _info() -> tumar.EncrInfo:
    # The portal's rounding can differ from a local pl_sum * 0.8 calculation.
    return tumar.EncrInfo(
        sign="server-signature", salt="salt", pl_sum=100.0, d_sum=80.01,
        id_priceoffer="1_2", public_key="recipient-certificate",
    )


def _provider(filler, socket):
    filler.requires_confirmation = True
    filler.supports_stop = True
    provider = tumar.TumarCSP(api_key="k", autofill=filler)
    provider._ws = socket
    return provider


@pytest.fixture(autouse=True)
def _fresh_popup_gate(monkeypatch):
    monkeypatch.setattr(tumar, "_POPUP_LOCK", asyncio.Lock())
    monkeypatch.setattr(tumar, "_popup_last_release", float("-inf"))


@pytest.mark.parametrize("early_reply", [False, True], ids=["pending-reply", "early-reply"])
async def test_auto_aborts_when_filler_fails(early_reply):
    socket = _Socket(reply_immediately=early_reply)

    def fill(price, stop, *, ready):
        ready()
        assert socket.recv_started.wait(1.0)
        return False

    provider = _provider(fill, socket)

    with pytest.raises(tumar.TumarError):
        await asyncio.wait_for(provider.encrypt_offer_price(_info()), timeout=2.0)

    assert socket.recv_cancelled == (not early_reply)
    assert socket.closed
    assert provider._ws is None


async def test_auto_requires_verified_input_even_after_provider_reply():
    socket = _Socket(reply_immediately=True)
    baseline_started = threading.Event()
    allow_baseline = threading.Event()
    allow_confirmation = threading.Event()
    filled = []

    def fill(price, stop, *, ready):
        filled.append(price)
        baseline_started.set()
        if not allow_baseline.wait(2.0):
            return False
        ready()
        return allow_confirmation.wait(2.0) and not stop.is_set()

    provider = _provider(fill, socket)
    operation = asyncio.create_task(provider.encrypt_offer_price(_info()))
    try:
        assert await asyncio.to_thread(baseline_started.wait, 1.0)
        await asyncio.wait({operation}, timeout=0.06)
        assert socket.sent == []
        allow_baseline.set()

        await asyncio.wait_for(socket.reply_delivered.wait(), timeout=1.0)
        finished, _ = await asyncio.wait({operation}, timeout=0.05)
        assert not finished
    finally:
        allow_baseline.set()
        allow_confirmation.set()
        if not socket.reply_delivered.is_set():
            operation.cancel()
        results = await asyncio.wait_for(asyncio.gather(operation, return_exceptions=True), timeout=2.0)

    assert filled == [80.01]
    assert len(socket.sent) == 1
    assert socket.sent[0]["Param"]["d_sum"] == 80.01
    assert results == [tumar.EncryptResult("sealed", "key", "", "")]
    assert not socket.closed


@pytest.mark.parametrize("cancel", [False, True], ids=["timeout", "cancel"])
async def test_auto_signals_worker_and_closes_socket_after_abort(monkeypatch, cancel):
    if not cancel:
        monkeypatch.setattr(tumar, "_AUTO_POPUP_TIMEOUT_S", 0.25)
    socket = _Socket(reply_immediately=True)
    worker_stopped = threading.Event()

    def fill(price, stop, *, ready):
        ready()
        if stop.wait(1.0):
            worker_stopped.set()
        return False

    provider = _provider(fill, socket)
    operation = asyncio.create_task(provider.encrypt_offer_price(_info()))
    if cancel:
        await asyncio.wait_for(socket.reply_delivered.wait(), timeout=1.0)
        await asyncio.wait({operation}, timeout=0.02)
        operation.cancel()

    with pytest.raises(asyncio.CancelledError if cancel else tumar.TumarError):
        await asyncio.wait_for(operation, timeout=2.0)

    assert worker_stopped.is_set()
    assert socket.reply_delivered.is_set()
    assert socket.closed
    assert provider._ws is None


async def test_auto_failure_closes_old_socket_and_keeps_replacement():
    socket = _Socket()
    replacement = _Socket()
    fail = threading.Event()

    def fill(price, stop, *, ready):
        ready()
        assert fail.wait(1.0)
        return False

    provider = _provider(fill, socket)
    operation = asyncio.create_task(provider.encrypt_offer_price(_info()))
    try:
        assert await asyncio.to_thread(socket.recv_started.wait, 1.0)
        provider._ws = replacement
    finally:
        fail.set()

    with pytest.raises(tumar.TumarError):
        await asyncio.wait_for(operation, timeout=2.0)

    assert socket.recv_cancelled and socket.closed
    assert provider._ws is replacement
    assert not replacement.closed


async def test_auto_request_waiting_for_a_popup_does_not_reuse_failed_socket(monkeypatch):
    monkeypatch.setattr(tumar, "_POPUP_MIN_SPACING_S", 0.0)
    socket = _Socket()
    fail = threading.Event()

    def fill(price, stop, *, ready):
        ready()
        assert fail.wait(1.0)
        return False

    provider = _provider(fill, socket)
    first = asyncio.create_task(provider.encrypt_offer_price(_info()))
    try:
        assert await asyncio.to_thread(socket.recv_started.wait, 1.0)
        second = asyncio.create_task(provider.encrypt_offer_price(_info()))
        queued, _ = await asyncio.wait({second}, timeout=0.02)
        assert not queued
    finally:
        fail.set()

    outcomes = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), timeout=2.0)

    assert all(isinstance(outcome, tumar.TumarError) for outcome in outcomes)
    assert len(socket.sent) == 1
    assert socket.closed
