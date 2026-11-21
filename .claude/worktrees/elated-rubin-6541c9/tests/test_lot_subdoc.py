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
    async def sign_file(self, url):
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
    """More signable sub-docs than the concurrent window -> sequential fallback
    still collects the full contiguous prefix."""
    gz = _FakeGz({
        1: PAGE_WITH_FILE.format(fid=111),
        2: PAGE_WITH_FILE.format(fid=222),
        3: PAGE_WITH_FILE.format(fid=333),
        4: PAGE_UPLOAD_ONLY,
    })
    ns = _FakeNs()
    # width=2 -> only pages 1,2 probed concurrently; 3 (signable) reached via fallback.
    saves = await _lot_sign_prepare(gz, ns, "17046469", "71284422", 3334, 85114643,
                                    "https://x", probe_width=2)
    assert [num for num, _ in saves] == [1, 2, 3]
    assert 4 in gz.fetches            # probed the end marker (sequentially)


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
    async def _noop(_s):
        return None
    monkeypatch.setattr(flow.asyncio, "sleep", _noop)


async def test_docs_next_polls_through_transient_rejection(_no_sleep):
    """The run-2 failure: error, error, then ok -> we must keep polling, not bail."""
    gz = _FakeGzDocsNext([_DN_ERR, _DN_ERR, _DN_OK])
    body, accepted = await _docs_next_until_accepted(gz, "17046469", "71285728")
    assert accepted is True
    assert gz.calls == 3                 # polled past both transient rejections


async def test_docs_next_accepts_immediately(_no_sleep):
    gz = _FakeGzDocsNext([_DN_OK])
    body, accepted = await _docs_next_until_accepted(gz, "17046469", "71285753")
    assert accepted is True
    assert gz.calls == 1                 # no needless extra polls on a clean run


async def test_docs_next_gives_up_after_max_tries(_no_sleep):
    """A genuine (non-transient) rejection stays non-fatal but bounded."""
    gz = _FakeGzDocsNext([_DN_ERR])
    body, accepted = await _docs_next_until_accepted(gz, "17046469", "1")
    assert accepted is False
    assert gz.calls == flow._DOCS_NEXT_TRIES   # bounded, doesn't loop forever


# --- re-save the тех.спец when docs_next says Приложение 15 is unsigned ---------

_DN_ERR_OTHER = '{"status":"error","message":"Какая-то другая ошибка валидации"}'


_DN_ERR_PRIL1 = ('{"status":"error","message":"Подпишите прикрепленный документ '
                 'в категории Приложение 1 у лота № 85996082-ОК2"}')


async def test_docs_next_resaves_tech_spec_on_reject(_no_sleep):
    """Приложение 15 rejection -> re-save callback fires, then it accepts."""
    gz = _FakeGzDocsNext([_DN_ERR, _DN_OK])
    calls = {"n": 0}

    async def on_reject():
        calls["n"] += 1

    body, accepted = await _docs_next_until_accepted(
        gz, "17046469", "1", on_reject=on_reject)
    assert accepted is True
    assert calls["n"] == 1               # re-saved once, between reject and accept


async def test_docs_next_resaves_on_pril1_reject(_no_sleep):
    """Приложение 1 (beneficiary) rejection now ALSO fires the re-save — the bug
    that let Приложение 1 ship unsigned was that only Приложение 15 was healed."""
    gz = _FakeGzDocsNext([_DN_ERR_PRIL1, _DN_OK])
    calls = {"n": 0}

    async def on_reject():
        calls["n"] += 1

    body, accepted = await _docs_next_until_accepted(
        gz, "17046469", "1", on_reject=on_reject)
    assert accepted is True
    assert calls["n"] == 1


async def test_docs_next_no_resave_on_unrelated_reject(_no_sleep):
    """A rejection that isn't about signing a doc must NOT trigger a re-save."""
    gz = _FakeGzDocsNext([_DN_ERR_OTHER])
    calls = {"n": 0}

    async def on_reject():
        calls["n"] += 1

    body, accepted = await _docs_next_until_accepted(
        gz, "17046469", "1", on_reject=on_reject)
    assert accepted is False
    assert calls["n"] == 0               # unrelated message -> no re-save


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
