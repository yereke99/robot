"""Read-only probes that overlap a slow portal write.

Every stage of a bid ends with a POST that flips the application to the next
stage, and the first thing the bot needs afterwards is a page of that next
stage (the draft's id, the app_lot map, the price page). Sent only after the
POST has answered, that read costs one more full round-trip on the critical
path (~1 s on the live portal).

``HedgedRead`` repeats the read on a short cadence WHILE the write is still in
flight. A probe that the portal evaluates before the write has committed is
answered with a redirect and ignored; the first probe evaluated after it
returns the page — up to one round-trip earlier than a read sent after the
POST's response. Only reads are repeated: nothing is ever written early, so a
portal that gates the page until the very end simply makes every probe miss
and the usual read (sent when the POST answers) wins the race, as before.

The probes must not disturb the session the write depends on: they are sent
with GoszakupClient.peek — flagged as AJAX (never regenerates the session id)
and with their Set-Cookie answers discarded (never replaces it).
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Generic, Optional, TypeVar

import structlog

log = structlog.get_logger("hedge")

T = TypeVar("T")


class HedgedRead(Generic[T]):
    """Repeat ``fetch(attempt)`` until one call returns a non-None result.

    ``fetch`` must be read-only and must not raise (anything it does raise is
    treated as a miss). At most ``max_inflight`` calls overlap and at most
    ``max_total`` are ever made, so a slow portal is not flooded.
    """

    def __init__(
        self,
        fetch: Callable[[int], Awaitable[Optional[T]]],
        *,
        interval_s: float = 0.3,
        first_delay_s: float = 0.0,
        max_inflight: int = 3,
        max_total: int = 6,
        label: str = "",
    ):
        self._fetch = fetch
        self._interval_s = max(0.01, float(interval_s))
        self._first_delay_s = max(0.0, float(first_delay_s))
        self._max_inflight = max(1, int(max_inflight))
        self._max_total = max(1, int(max_total))
        self.label = label
        self._tasks: set[asyncio.Task] = set()
        self._launcher: Optional[asyncio.Task] = None
        self._first: Optional[asyncio.Future] = None
        self._slot: Optional[asyncio.Event] = None       # set when a probe answers
        self._stopped = False
        self.launched = 0
        self.hit_at: Optional[float] = None      # loop time of the first hit

    # ------------------------------------------------------------------ control
    def start(self) -> "HedgedRead[T]":
        if self._launcher is None:
            loop = asyncio.get_running_loop()
            self._first = loop.create_future()
            self._slot = asyncio.Event()
            self._launcher = loop.create_task(self._launch_loop())
        return self

    def stop(self) -> None:
        """Launch no more probes. The ones already in flight still count."""
        self._stopped = True
        if self._slot is not None:
            self._slot.set()
        self._finish_if_idle()

    @property
    def hit(self) -> Optional[T]:
        first = self._first
        if first is not None and first.done() and not first.cancelled():
            return first.result()
        return None

    @property
    def finished(self) -> bool:
        return self._first is None or self._first.done()

    async def first(self) -> Optional[T]:
        """The first result a probe returned, or None once launching has ended
        and every probe in flight has answered without one."""
        if self._first is None:
            return None
        return await asyncio.shield(self._first)

    async def aclose(self) -> None:
        self._stopped = True
        pending = [task for task in (self._launcher, *self._tasks) if task and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        if self._first is not None and not self._first.done():
            self._first.set_result(None)

    # ---------------------------------------------------------------- internals
    def _launching_over(self) -> bool:
        return self._stopped or self.launched >= self._max_total

    async def _launch_loop(self) -> None:
        try:
            if self._first_delay_s:
                await asyncio.sleep(self._first_delay_s)
            while not self._launching_over() and not self._first.done():
                # At the cap, the next probe goes out the moment one answers —
                # not a whole interval later, which could step right over the
                # instant the portal starts serving the page.
                while len(self._tasks) >= self._max_inflight:
                    self._slot.clear()
                    await self._slot.wait()
                    if self._launching_over() or self._first.done():
                        return
                self.launched += 1
                task = asyncio.ensure_future(self._probe(self.launched))
                self._tasks.add(task)
                task.add_done_callback(self._probe_done)
                if self._launching_over():
                    break
                await asyncio.sleep(self._interval_s)
        finally:
            self._stopped = True
            self._finish_if_idle()

    async def _probe(self, attempt: int) -> Optional[T]:
        try:
            return await self._fetch(attempt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a failed probe is just a miss
            log.debug("hedge.probe_failed", label=self.label, error_type=type(exc).__name__)
            return None

    def _probe_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if self._slot is not None:
            self._slot.set()
        first = self._first
        if first is None or first.done():
            return
        result = None if task.cancelled() else task.result()
        if result is not None:
            self.hit_at = task.get_loop().time()
            first.set_result(result)
            return
        self._finish_if_idle()

    def _finish_if_idle(self) -> None:
        first = self._first
        if first is not None and not first.done() and self._stopped and not self._tasks:
            first.set_result(None)


async def first_of(early: Optional[Awaitable[Optional[T]]],
                   usual: Callable[[], Awaitable[T]]) -> tuple[T, bool]:
    """``early`` (resolves to a result or None) raced against the usual read.

    Returns ``(result, from_early)``. The usual read starts immediately unless
    the early result is already there — waiting on probes that may all miss
    would only add their latency to the critical path. A usual read that is
    no longer needed is cancelled.
    """
    early_future = asyncio.ensure_future(early) if early is not None else None
    if early_future is not None and early_future.done():
        got = None if early_future.cancelled() else early_future.result()
        if got is not None:
            return got, True
        early_future = None
    if early_future is None:
        return await usual(), False
    usual_task = asyncio.ensure_future(usual())
    try:
        await asyncio.wait({early_future, usual_task}, return_when=asyncio.FIRST_COMPLETED)
        if early_future.done() and not early_future.cancelled():
            got = early_future.result()
            if got is not None:
                return got, True
        return await usual_task, False
    finally:
        if not usual_task.done():
            usual_task.cancel()
            await asyncio.gather(usual_task, return_exceptions=True)
        if not early_future.done():
            early_future.cancel()
