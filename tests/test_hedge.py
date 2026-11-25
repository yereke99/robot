"""Reads that overlap a portal write (goszakup/hedge.py), on a virtual clock."""

from __future__ import annotations

import asyncio

import pytest

from goszakup.hedge import HedgedRead, first_of
from tools.sim_portal import VirtualLoop


def _run(coro):
    loop = VirtualLoop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _now() -> float:
    return asyncio.get_event_loop().time()


def test_first_probe_evaluated_after_the_commit_wins_and_launching_stops():
    async def main():
        sent = []
        commit_at = 1.0

        async def fetch(n):
            sent.append(round(_now(), 2))
            await asyncio.sleep(0.6)                    # the portal looks at 60% ...
            seen = _now() >= commit_at
            await asyncio.sleep(0.4)                    # ... and answers at 100%
            return f"page-{n}" if seen else None

        read = HedgedRead(fetch, interval_s=0.3, max_inflight=4, max_total=9).start()
        got = await read.first()
        finished = round(_now(), 2)
        await read.aclose()                             # the probes still out are dropped
        return got, sent, finished, read

    got, sent, finished, read = _run(main())
    assert sent[:3] == [0.0, 0.3, 0.6]
    assert got == "page-3"                              # sent 0.6: looked at 1.2 >= 1.0
    assert finished == pytest.approx(1.6)               # a read sent at the commit would end at 2.0
    assert read.hit == "page-3" and read.hit_at == pytest.approx(1.6)
    assert len(sent) <= 6                               # nothing launched after the hit


def test_caps_bound_the_load_and_a_freed_slot_is_reused_at_once():
    async def main():
        inflight = peak = 0
        sent = []

        async def fetch(n):
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            sent.append(round(_now(), 2))
            await asyncio.sleep(1.0)
            inflight -= 1
            return None

        read = HedgedRead(fetch, interval_s=0.2, max_inflight=2, max_total=5).start()
        assert await read.first() is None               # every probe missed
        return sent, peak, read.launched

    sent, peak, launched = _run(main())
    assert peak == 2 and launched == 5
    # 0.0 and 0.2 fill the cap; the third goes out when the first answers (1.0),
    # not at the next 0.2 s tick after that.
    assert sent == [0.0, 0.2, 1.0, 1.2, 2.0]


def test_stop_ends_launching_but_a_probe_already_out_can_still_win():
    async def main():
        async def fetch(n):
            await asyncio.sleep(1.0)
            return "late" if n == 2 else None

        read = HedgedRead(fetch, interval_s=0.5, max_inflight=3, max_total=9).start()
        await asyncio.sleep(0.6)                        # probes 1 and 2 are out
        read.stop()
        assert read.hit is None
        got = await read.first()
        return got, read.launched, round(_now(), 2)

    got, launched, finished = _run(main())
    assert (got, launched) == ("late", 2)
    assert finished == pytest.approx(1.5)


def test_stop_with_nothing_in_flight_resolves_none_and_failures_are_misses():
    async def main():
        async def boom(n):
            raise RuntimeError("portal down")

        read = HedgedRead(boom, interval_s=0.1, max_total=3).start()
        assert await read.first() is None
        assert read.launched == 3

        idle = HedgedRead(boom, first_delay_s=5.0).start()
        idle.stop()
        assert await idle.first() is None
        assert idle.launched == 0
        await idle.aclose()

        never_started = HedgedRead(boom)
        assert await never_started.first() is None and never_started.hit is None

    _run(main())


def test_aclose_cancels_probes_in_flight():
    async def main():
        cancelled = []

        async def fetch(n):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append(n)
                raise

        read = HedgedRead(fetch, interval_s=0.1, max_inflight=2).start()
        await asyncio.sleep(0.25)
        await read.aclose()
        assert sorted(cancelled) == [1, 2]
        assert await read.first() is None

    _run(main())


def test_first_of_uses_an_early_result_without_starting_the_usual_read():
    async def main():
        calls = []

        async def usual():
            calls.append("usual")
            return "usual"

        ready = asyncio.get_running_loop().create_future()
        ready.set_result("early")
        assert await first_of(ready, usual) == ("early", True)
        assert calls == []
        assert await first_of(None, usual) == ("usual", False)

        missed = asyncio.get_running_loop().create_future()
        missed.set_result(None)
        assert await first_of(missed, usual) == ("usual", False)

    _run(main())


def test_first_of_races_probes_still_out_against_the_usual_read():
    async def main():
        usual_cancelled = []

        async def usual():
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                usual_cancelled.append(True)
                raise
            return "usual"

        async def early(delay, value):
            await asyncio.sleep(delay)
            return value

        assert await first_of(early(0.4, "early"), usual) == ("early", True)
        assert usual_cancelled == [True] and _now() == pytest.approx(0.4)

        # every probe missed -> the usual read is awaited, not restarted
        assert await first_of(early(0.2, None), usual) == ("usual", False)
        assert _now() == pytest.approx(1.4)

        # the usual read answered first -> it wins even though probes are out
        assert await first_of(early(5.0, "early"), usual) == ("usual", False)
        assert _now() == pytest.approx(2.4)

    _run(main())


def test_first_of_passes_on_an_error_of_the_usual_read():
    async def main():
        async def usual():
            raise RuntimeError("draft already at the price stage")

        async def early():
            await asyncio.sleep(1.0)
            return "early"

        with pytest.raises(RuntimeError, match="price stage"):
            await first_of(early(), usual)

    _run(main())
