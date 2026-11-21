"""Regression tests for doc 3334 lot sub-document classification.

Root cause (fixed): an upload-only fileless sub-doc page (the standing "attach
your own document" slot) was misread as an actionable pending-file sub-doc
because _describe_unsignable_page reports has_upload=True for the always-present
upload widget. That produced 4 useless retries + a false flow.lot_doc.needs_action
on the Техническая спецификация, even though a captured complete bid (HAR
announce 17049098) reached final submission having signed only the pages with
auto-attached files and left the upload-only page untouched.

These tests pin the corrected behaviour: only generate/agree pages are
actionable; an upload-only page ends the contiguous sub-document list cleanly.
"""

from __future__ import annotations

import types

import pytest

from goszakup import flow
from goszakup.flow import (
    _describe_unsignable_page,
    _docs_next_until_accepted,
    _lot_subdoc_actionable,
    _lot_sign_prepare,
    _lot_sign_save,
)

# --- representative sub-doc pages (>5000 chars so "substantial" matches reality) ---
_PAD = "<div>" + "x" * 6000 + "</div>"

PAGE_UPLOAD_ONLY = (  # doc 3334 page 3: Техническая спецификация, attach slot
    '<h3>Техническая спецификация</h3>'
    '<form id="form_uploading"><input type="file" name="userfile[0]">'
    '<input type="submit" value="Сохранить"></form>' + _PAD
)
PAGE_GENERATE = (  # a sub-doc the bot must generate, then sign
    '<h3>Приложение 15</h3>'
    '<input type="submit" value="Сформировать документ"> сформировать' + _PAD
)
PAGE_AGREE = (  # an agreement the bot must accept, then sign
    '<h3>Соглашение</h3> принять соглашение'
    '<input type="submit" value="Принять соглашение"></form>' + _PAD
)
PAGE_BLANK = "<html><body></body></html>"  # shell past the last sub-doc
PAGE_WITH_FILE = '<a href="/files/download_file/{fid}/">подписать</a>' + _PAD


def test_upload_only_page_is_not_actionable():
    """The bug: upload-only page was treated as actionable -> retries + alarm."""
    sig = _describe_unsignable_page(PAGE_UPLOAD_ONLY)
    assert sig["has_upload"] is True          # widget is present...
    assert sig["has_generate"] is False
    assert sig["has_agree"] is False
    assert _lot_subdoc_actionable(sig) is False  # ...but it is NOT a pending file


def test_generate_and_agree_pages_stay_actionable():
    assert _lot_subdoc_actionable(_describe_unsignable_page(PAGE_GENERATE)) is True
    assert _lot_subdoc_actionable(_describe_unsignable_page(PAGE_AGREE)) is True


def test_blank_shell_is_not_actionable():
    assert _lot_subdoc_actionable(_describe_unsignable_page(PAGE_BLANK)) is False


class _FakeGz:
    """Serves doc 3334 sub-pages by num and records every fetch + sign."""

    def __init__(self, pages: dict[int, str]):
        self.pages = pages
        self.fetches: list[int] = []
        self.signed_nums: list[int] = []

    async def get_show_doc(self, ann, app_id, doc_id, suffix=""):
        if not suffix:
            return ""  # тех.спец parent re-GET (commit barrier) — no num to serve
        num = int(suffix.rsplit("/", 1)[-1])
        self.fetches.append(num)
        if num not in self.pages:
            raise RuntimeError(f"no such page {num}")  # flow treats as unreachable
        return self.pages[num]

    async def sign_lot_doc(self, ann, app_id, doc_id, app_lot, num, signatures):
        self.signed_nums.append(num)


class _FakeNs:
    async def sign_file(self, url, *, sig_cache=None, referer=None, cache_key=None):
        return "SIG"


async def test_lot_sign_prepare_then_save_upload_only(caplog):
    """Pages 1-2 have files, page 3 is the upload-only slot.

    prepare: probes the first `probe_width` pages CONCURRENTLY, takes the
    contiguous signable prefix [1, 2], stops at 3 (end), signs nothing (no POST),
    no retry storm, no needs_action. save: POSTs 1 & 2.
    """
    gz = _FakeGz({
        1: PAGE_WITH_FILE.format(fid=111),
        2: PAGE_WITH_FILE.format(fid=222),
        3: PAGE_UPLOAD_ONLY,
        # no page 4 -> probed concurrently within the window, returns "unreachable"
    })
    ns = _FakeNs()

    with caplog.at_level("INFO"):
        saves = await _lot_sign_prepare(gz, ns, "17046469", "71284422", 3334, 85114643,
                                        "https://x", probe_width=4)

    assert [num for num, _ in saves] == [1, 2]      # contiguous signable prefix
    assert gz.signed_nums == []                      # prepare does NOT save
    assert gz.fetches.count(3) == 1                  # page 3 probed once (no retry storm)
    assert 5 not in gz.fetches                        # never scanned past the probe window
    assert "flow.lot_doc.needs_action" not in caplog.text  # no false alarm

    # save POSTs the prepared signatures (this is the last, uninterleaved write).
    await _lot_sign_save(gz, "17046469", "71284422", 3334, 85114643, saves)
    assert gz.signed_nums == [1, 2]


async def test_lot_sign_prepare_falls_back_past_probe_window():
    """The bounded prefetch window still collects the full contiguous prefix."""
    gz = _FakeGz({
        1: PAGE_WITH_FILE.format(fid=111),
        2: PAGE_WITH_FILE.format(fid=222),
        3: PAGE_WITH_FILE.format(fid=333),
        4: PAGE_UPLOAD_ONLY,
    })
    ns = _FakeNs()
    saves = await _lot_sign_prepare(gz, ns, "17046469", "71284422", 3334, 85114643,
                                    "https://x", probe_width=2)
    assert [num for num, _ in saves] == [1, 2, 3]
    assert gz.fetches == [1, 2, 3, 4]


async def test_lot_page_authentication_failure_reaches_document_recovery():
    class ExpiredPortal(_FakeGz):
        async def get_show_doc(self, *args, suffix="", **kwargs):
            if suffix.endswith("/1"):
                raise flow.SessionExpired("login required")
            return await super().get_show_doc(*args, suffix=suffix, **kwargs)

    gz = ExpiredPortal({2: PAGE_UPLOAD_ONLY, 3: PAGE_BLANK})
    with pytest.raises(flow.SessionExpired, match="login required"):
        await _lot_sign_prepare(gz, _FakeNs(), "1", "2", 3334, 3, "https://x")
    assert gz.signed_nums == []


# --- docs_next polling (the Приложение 15 async-validation race) ---------------

_DN_OK = '{"status":"ok"}'
# Real shape: server says "sign the attached Приложение 15" while validation lags.
_DN_ERR = ('{"status":"error","message":"Подпишите прикрепленный документ в '
           'категории Приложение 15 (Техническая спецификация) у лота № 85996082-ОК2"}')


class _FakeGzDocsNext:
    """Returns a scripted sequence of docs_next bodies; clamps to the last one."""

    def __init__(self, replies: list[str]):
        self.replies = replies
        self.calls = 0

    async def docs_next(self, ann, app_id):
        body = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return types.SimpleNamespace(text=body, status_code=200)


@pytest.fixture
def _no_sleep(monkeypatch):
    """No real waiting, and no time-based patience: with the clock standing still
    the poll COUNTS alone decide (the seconds-based rules have their own test)."""
    async def _noop(_s):
        return None
    monkeypatch.setattr(flow.asyncio, "sleep", _noop)
    monkeypatch.setattr(flow, "_TECH_SPEC_PATIENCE_S", 0.0)
    monkeypatch.setattr(flow, "_SETTLE_S_AFTER_RESAVE", 0.0)
    monkeypatch.setattr(flow, "_DOCS_NEXT_MAX_WAIT_S", 0.0)


@pytest.fixture
def _fake_clock(monkeypatch):
    """A clock that only moves when the code sleeps — makes the seconds-based
    patience deterministic. Each docs_next poll also "takes" `poll_s`."""
    clock = types.SimpleNamespace(now=1000.0, poll_s=0.35, sleeps=[])

    async def _sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds
    monkeypatch.setattr(flow.asyncio, "sleep", _sleep)
    monkeypatch.setattr(flow, "_clock", lambda: clock.now)
    return clock


async def test_docs_next_polls_through_transient_rejection(_no_sleep):
    """The run-2 failure: error, error, then ok -> we must keep polling, not bail."""
    gz = _FakeGzDocsNext([_DN_ERR, _DN_ERR, _DN_OK])
    body, accepted, message = await _docs_next_until_accepted(gz, "17046469", "71285728")
    assert body == _DN_OK
    assert accepted is True
    assert message is None
    assert gz.calls == 3                 # polled past both transient rejections


async def test_docs_next_accepts_immediately(_no_sleep):
    gz = _FakeGzDocsNext([_DN_OK])
    body, accepted, message = await _docs_next_until_accepted(gz, "17046469", "71285753")
    assert body == _DN_OK
    assert accepted is True
    assert message is None
    assert gz.calls == 1                 # no needless extra polls on a clean run


async def test_docs_next_gives_up_after_max_tries(_no_sleep):
    """A persistent rejection returns its reason after the bounded retry count."""
    gz = _FakeGzDocsNext([_DN_ERR])
    body, accepted, message = await _docs_next_until_accepted(gz, "17046469", "1")
    assert body == _DN_ERR
    assert accepted is False
    assert message and "Приложение 15" in message
    assert gz.calls == flow._DOCS_NEXT_TRIES   # bounded, doesn't loop forever


# --- re-save the тех.спец when docs_next says Приложение 15 is unsigned ---------

_DN_ERR_OTHER = '{"status":"error","message":"Какая-то другая ошибка валидации"}'


_DN_ERR_PRIL1 = ('{"status":"error","message":"Подпишите прикрепленный документ '
                 'в категории Приложение 1 у лота № 85996082-ОК2"}')


async def test_docs_next_does_not_resave_on_transient_tech_spec_lag(_no_sleep):
    """THE 44-second bug. The server says "Подпишите … Приложение 15" for a moment
    after the тех.спец is saved, simply because it is still validating the files.
    That must be polled through — NOT answered with a re-commit of every document
    (which is what happened on the first such answer, costing ~25s)."""
    gz = _FakeGzDocsNext([_DN_ERR, _DN_ERR, _DN_ERR, _DN_OK])
    calls = []

    async def on_reject(msg, level):
        calls.append(level)

    body, accepted, message = await _docs_next_until_accepted(
        gz, "17046469", "1", on_reject=on_reject)
    assert accepted is True
    assert message is None
    assert gz.calls == 4
    assert calls == []                   # lag cleared by itself -> nothing re-saved


async def test_docs_next_escalates_when_tech_spec_stays_rejected(_no_sleep):
    """If the тех.спец is STILL reported unsigned after the patience window, the
    save really didn't stick: re-commit — the тех.спец alone first (level 0),
    then everything (level 1+), each only after the previous one had time to
    settle, and never more than _MAX_RESAVES rounds."""
    gz = _FakeGzDocsNext([_DN_ERR])      # rejected forever
    calls = []

    async def on_reject(msg, level):
        calls.append((gz.calls, level))  # (docs_next attempts so far, level)

    body, accepted, message = await _docs_next_until_accepted(
        gz, "17046469", "1", on_reject=on_reject)
    assert accepted is False
    assert gz.calls == flow._DOCS_NEXT_TRIES
    first = flow._TECH_SPEC_PATIENCE
    settle = flow._SETTLE_POLLS_AFTER_RESAVE
    assert calls == [(first, 0), (first + settle, 1), (first + 2 * settle, 2)]
    assert len(calls) <= flow._MAX_RESAVES


async def test_docs_next_resaves_tech_spec_once_then_accepts(_no_sleep):
    """Rejected through the whole patience window, re-saved, then accepted."""
    replies = [_DN_ERR] * flow._TECH_SPEC_PATIENCE + [_DN_OK]
    gz = _FakeGzDocsNext(replies)
    calls = []

    async def on_reject(msg, level):
        calls.append(level)

    body, accepted, message = await _docs_next_until_accepted(
        gz, "17046469", "1", on_reject=on_reject)
    assert accepted is True
    assert calls == [0]                  # one TARGETED re-save was enough


async def test_docs_next_resaves_on_pril1_reject(_no_sleep):
    """Приложение 1 (beneficiary) rejection fires the re-save RIGHT AWAY — small
    generated docs don't lag, so this one means the save was lost (the bug that
    let Приложение 1 ship unsigned). Targeted (level 0): only that document."""
    gz = _FakeGzDocsNext([_DN_ERR_PRIL1, _DN_OK])
    calls = []

    async def on_reject(msg, level):
        calls.append((msg, level))

    body, accepted, message = await _docs_next_until_accepted(
        gz, "17046469", "1", on_reject=on_reject)
    assert accepted is True
    assert message is None
    assert len(calls) == 1
    assert "Приложение 1" in calls[0][0] and calls[0][1] == 0


async def test_docs_next_pril1_escalates_to_full_if_targeted_did_not_help(_no_sleep):
    gz = _FakeGzDocsNext([_DN_ERR_PRIL1, _DN_ERR_PRIL1, _DN_OK])
    calls = []

    async def on_reject(msg, level):
        calls.append(level)

    body, accepted, message = await _docs_next_until_accepted(
        gz, "17046469", "1", on_reject=on_reject)
    assert accepted is True
    assert calls == [0, 1]               # targeted, then the full re-commit


async def test_docs_next_tracks_each_document_separately(_no_sleep):
    """Fixing Приложение 1 reveals the (lagging) Приложение 15: that is a NEW
    complaint with its own patience — it must not inherit the escalation level."""
    gz = _FakeGzDocsNext([_DN_ERR_PRIL1, _DN_ERR, _DN_ERR, _DN_OK])
    calls = []

    async def on_reject(msg, level):
        calls.append(("15" in msg, level))

    body, accepted, message = await _docs_next_until_accepted(
        gz, "17046469", "1", on_reject=on_reject)
    assert accepted is True
    assert calls == [(False, 0)]         # only Приложение 1 was re-saved


async def test_docs_next_no_resave_on_unrelated_reject(_no_sleep):
    """A rejection that isn't about signing a doc must NOT trigger a re-save."""
    gz = _FakeGzDocsNext([_DN_ERR_OTHER])
    calls = {"n": 0}

    async def on_reject(msg, level):
        calls["n"] += 1

    body, accepted, message = await _docs_next_until_accepted(
        gz, "17046469", "1", on_reject=on_reject)
    assert accepted is False
    assert message == "Какая-то другая ошибка валидации"
    assert calls["n"] == 0               # unrelated message -> no re-save


async def test_docs_next_survives_a_failing_resave(_no_sleep):
    """The re-save is best-effort: if it raises we keep polling."""
    gz = _FakeGzDocsNext([_DN_ERR_PRIL1, _DN_OK])

    async def on_reject(msg, level):
        raise RuntimeError("save POST failed")

    body, accepted, message = await _docs_next_until_accepted(
        gz, "17046469", "1", on_reject=on_reject)
    assert accepted is True


def test_is_tech_spec_reject_matches_real_message():
    assert flow._is_tech_spec_reject(
        "Подпишите прикрепленный документ в категории Приложение 15 "
        "(Техническая спецификация) у лота № 85996082-ОК2") is True
    assert flow._is_tech_spec_reject("Какая-то другая ошибка") is False
    assert flow._is_tech_spec_reject(None) is False


def test_needs_resave_covers_any_signature_reject():
    # broadened: fires for Приложение 1 too, not only the тех.спец
    assert flow._needs_resave(
        "Подпишите прикрепленный документ в категории Приложение 1 …") is True
    assert flow._needs_resave(
        "Подпишите ... Приложение 15 (Техническая спецификация)") is True
    assert flow._needs_resave("Какая-то другая ошибка валидации") is False
    assert flow._needs_resave(None) is False


# --- тех.спец signature cache: a known file is not downloaded again -------------

async def test_lot_page_passes_file_id_cache_key():
    """Each тех.спец file is signed with cache_key "fid:<id>" so a signature made
    on an earlier attempt is reused without re-downloading the multi-MB file."""
    seen = []

    class _Ns:
        async def sign_file(self, url, *, sig_cache=None, referer=None, cache_key=None):
            seen.append((url, cache_key, sig_cache is cache))
            return "SIG"

    cache: dict = {}
    gz = _FakeGz({1: PAGE_WITH_FILE.format(fid=111), 2: PAGE_UPLOAD_ONLY})
    saves = await _lot_sign_prepare(gz, _Ns(), "17046469", "71284422", 3334, 85114643,
                                    "https://x", sig_cache=cache)
    assert [num for num, _ in saves] == [1]
    assert seen == [("https://x/files/download_file/111/", "fid:111", True)]


async def test_lot_pages_probe_ahead_when_window_is_all_files():
    """All `probe_width` pages are signable -> the next page is fetched in the
    background while the caller is still saving, not only after it."""
    gz = _FakeGz({
        1: PAGE_WITH_FILE.format(fid=111),
        2: PAGE_WITH_FILE.format(fid=222),
        3: PAGE_UPLOAD_ONLY,
    })
    fetched_when_page2_arrives = None
    async with flow.aclosing(flow._lot_sign_pages(
            gz, _FakeNs(), "17046469", "71284422", 3334, 85114643, "https://x",
            probe_width=2)) as pages:
        async for num, _sigs in pages:
            if num == 2:
                await flow.asyncio.sleep(0)   # the caller is "saving" page 2
                await flow.asyncio.sleep(0)
                fetched_when_page2_arrives = list(gz.fetches)
    assert 3 in fetched_when_page2_arrives   # page 3 already requested by then
    assert gz.fetches.count(3) == 1          # ...and not fetched a second time


async def test_lot_page_read_ahead_starts_before_last_initial_signature():
    fourth_page_started = flow.asyncio.Event()

    class Portal(_FakeGz):
        async def get_show_doc(self, *args, **kwargs):
            html = await super().get_show_doc(*args, **kwargs)
            if self.fetches[-1] == 4:
                fourth_page_started.set()
            return html

    class Signer(_FakeNs):
        async def sign_file(self, url, **kwargs):
            if url.endswith("/333/"):
                await fourth_page_started.wait()
            return await super().sign_file(url, **kwargs)

    portal = Portal({
        1: PAGE_WITH_FILE.format(fid=111),
        2: PAGE_WITH_FILE.format(fid=222),
        3: PAGE_WITH_FILE.format(fid=333),
        4: PAGE_WITH_FILE.format(fid=444),
        5: PAGE_UPLOAD_ONLY,
    })
    pages = await flow.asyncio.wait_for(
        _lot_sign_prepare(portal, Signer(), "1", "2", 3334, 3, "https://x"),
        timeout=1.0,
    )
    assert [number for number, _signatures in pages] == [1, 2, 3, 4]
    assert portal.fetches == [1, 2, 3, 4, 5]


# --- patience is measured in SECONDS too (a fast portal must not be impatient) -----

class _TimedDocsNext:
    """docs_next that reports the тех.спец unsigned until `ready_at` on the fake clock."""

    def __init__(self, clock, ready_at, *, rejection=_DN_ERR):
        self.clock, self.ready_at, self.calls = clock, ready_at, 0
        self.rejection = rejection

    async def docs_next(self, ann, app_id):
        self.calls += 1
        self.clock.now += self.clock.poll_s          # the request itself takes time
        body = _DN_OK if self.clock.now >= self.ready_at else self.rejection
        return types.SimpleNamespace(text=body, status_code=200)


async def test_tech_spec_lag_of_a_few_seconds_is_waited_out(_fake_clock):
    """Server needs 5s to validate the тех.спец. On a fast portal 4 polls pass in
    ~2s — the poll count alone would re-save far too early. The seconds rule must
    hold it back: nothing is re-saved, the bid just proceeds when the lag clears."""
    gz = _TimedDocsNext(_fake_clock, ready_at=_fake_clock.now + 5.0)
    calls = []

    async def on_reject(msg, level):
        calls.append(level)

    body, accepted, _ = await _docs_next_until_accepted(gz, "a", "1", on_reject=on_reject)
    assert accepted is True
    assert calls == []
    assert gz.calls > flow._TECH_SPEC_PATIENCE        # more polls than the count rule alone allows


async def test_tech_spec_still_unsigned_after_patience_window_is_resaved(_fake_clock):
    start = _fake_clock.now
    gz = _TimedDocsNext(_fake_clock, ready_at=float("inf"))
    resaved_at = []

    async def on_reject(msg, level):
        resaved_at.append((round(_fake_clock.now - start, 1), level))
        if level == 0:
            gz.ready_at = _fake_clock.now             # the targeted re-save fixes it

    body, accepted, _ = await _docs_next_until_accepted(gz, "a", "1", on_reject=on_reject)
    assert accepted is True
    assert len(resaved_at) == 1 and resaved_at[0][1] == 0
    assert resaved_at[0][0] >= flow._TECH_SPEC_PATIENCE_S   # not before the window ran out


@pytest.mark.parametrize("rejection,poll_s,fast_delay", [
    (_DN_ERR, 0.35, flow._DOCS_NEXT_MIN_IDLE_S),
    (_DN_ERR, 0.05, 0.20),
    ('{"status":"error","message":"Не заполнены сведения"}', 0.35, flow._DOCS_NEXT_DELAY_S),
])
async def test_docs_next_polls_fast_first_then_backs_off(_fake_clock, rejection, poll_s, fast_delay):
    _fake_clock.poll_s = poll_s
    gz = _TimedDocsNext(_fake_clock, ready_at=float("inf"), rejection=rejection)
    await _docs_next_until_accepted(gz, "a", "1")     # no re-save callback: pure polling
    quick = flow._DOCS_NEXT_QUICK_RETRIES
    fast = flow._DOCS_NEXT_FAST_POLLS - 1
    assert _fake_clock.sleeps[:quick] == pytest.approx([fast_delay] * quick)
    assert _fake_clock.sleeps[quick:fast] == [flow._DOCS_NEXT_DELAY_S] * (fast - quick)
    assert set(_fake_clock.sleeps[fast:]) == {flow._DOCS_NEXT_SLOW_DELAY_S}
    # bounded by time, not left spinning: stops once the wait budget is spent
    assert _fake_clock.now - 1000.0 >= flow._DOCS_NEXT_MAX_WAIT_S
    assert gz.calls <= flow._DOCS_NEXT_MAX_TRIES


async def test_fast_docs_next_preserves_the_pause_after_resaving(_fake_clock, monkeypatch):
    monkeypatch.setattr(flow, "_TECH_SPEC_PATIENCE_S", 0.0)
    monkeypatch.setattr(flow, "_TECH_SPEC_PATIENCE", 1)
    gz = _TimedDocsNext(_fake_clock, ready_at=float("inf"))

    async def on_reject(_message, _level):
        gz.ready_at = _fake_clock.now

    _, accepted, _ = await _docs_next_until_accepted(gz, "a", "1", on_reject=on_reject)
    assert accepted
    assert _fake_clock.sleeps == [flow._DOCS_NEXT_DELAY_S]
