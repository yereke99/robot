"""Cross-tender concurrency guards.

The price popup is filled by typing at FIXED screen coordinates, so with several
tenders running at once the popups MUST be serialized process-wide: only one on
screen at a time, each carrying its own price, spaced >= 1.5s apart. These tests
pin that gate (goszakup/tumar.py::_popup_gate).
"""

from __future__ import annotations

import asyncio

import pytest

from goszakup import tumar


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
