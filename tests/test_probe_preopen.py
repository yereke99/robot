"""tools/probe_preopen.py --post: one create POST to a tender that is not open."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
import respx

from goszakup.client import GoszakupClient
from tools import probe_preopen

BASE = "https://v3bl.goszakup.gov.kz"
ANN = "555"
COMPANY = {"subject_address": "165932", "iik": "2589827", "contact_phone": "87000000000"}
CARD = (
    '<label class="col-sm-4 control-label">Статус объявления</label> <div class="col-sm-7"> '
    '<input type=\'text\' class="form-control" value="Опубликовано" readonly/> </div>'
    '<label class="col-sm-4 control-label">Срок начала приема заявок</label> <div class="col-sm-7"> '
    '<input type=\'text\' class="form-control" value="2026-12-01 09:00:00" readonly/> </div>'
    '<label class="col-sm-4 control-label">Срок окончания приема заявок</label> <div class="col-sm-7"> '
    '<input type=\'text\' class="form-control" value="2026-12-08 09:00:00" readonly/> </div>')
FORM = '<form><select name="subject_address"></select><input name="contact_phone"></form>'
NOT_FOUND = httpx.Response(302, headers={"location": "/ru/error_report/not_found"})


def _portal(router, *, create_page, post, drafts=(None, None)):
    """Routes of one tender. ``drafts``: the draft id the lots page names before
    and after the POST (None = the applicant is sent back to the create form)."""
    seen = {"reads": 0}

    def lots(request):
        app_id = drafts[min(seen["reads"], 1)]
        seen["reads"] += 1
        target = f"/ru/application/lots/{ANN}/{app_id}" if app_id else f"/ru/application/create/{ANN}"
        return httpx.Response(302, headers={"location": target})

    router.get(f"/ru/announce/index/{ANN}").mock(return_value=httpx.Response(200, html=CARD))
    router.get(f"/ru/application/create/{ANN}").mock(return_value=create_page)
    router.get(f"/ru/application/lots/{ANN}").mock(side_effect=lots)
    return router.post(f"/ru/application/ajax_create_application/{ANN}").mock(return_value=post)


async def _probe(**kwargs):
    async with GoszakupClient(ci_session="live-session", base_url=BASE) as gz:
        return await probe_preopen.probe_create_post(gz, ANN, COMPANY, **kwargs)


@pytest.mark.asyncio
async def test_exactly_one_post_goes_to_a_tender_that_is_not_open(capsys):
    refused = httpx.Response(
        200, json={"status": "error", "message": "Прием заявок не начат"},
        headers={"date": "Tue, 01 Dec 2026 03:59:58 GMT"})
    async with respx.mock(base_url=BASE, assert_all_called=False) as router:
        post = _portal(router, create_page=NOT_FOUND, post=refused)
        result = await _probe()
        assert post.call_count == 1
        sent = post.calls.last.request.content.decode()
        assert "subject_address=165932" in sent and "iik=2589827" in sent
        # every read is flagged as AJAX: it cannot rotate the session a bot is using
        reads = [call.request for call in router.calls if call.request.method == "GET"]
        assert len(reads) == 4
        assert all(read.headers.get("x-requested-with") == "XMLHttpRequest" for read in reads)

    assert result["sent"] is True and result["create_page"] == "not_open"
    assert (result["status_text"], result["starts_at"]) == ("Опубликовано", "2026-12-01 09:00:00")
    assert (result["http_status"], result["shape"]) == (200, "status-error.message")
    assert "Прием заявок не начат" in result["body"]
    assert result["draft_before"] == (True, None) and result["draft_after"] == (True, None)
    assert result["server_time"] == "2026-12-01 08:59:58"          # shown in the portal's time
    assert isinstance(result["clock_offset_s"], float)

    probe_preopen._print_post_result(result)
    printed = capsys.readouterr().out
    assert "POST отправлен" in printed and "status-error.message" in printed
    assert "черновик после пробы: нет" in printed
    assert "live-session" not in printed                           # nothing secret is printed


@pytest.mark.asyncio
@pytest.mark.parametrize(("create_page", "state", "reason"), [
    (httpx.Response(200, html=FORM), "open", "not_confirmed_closed"),
    (httpx.Response(200, html="<h1>Портал</h1>"), "page", "not_confirmed_closed"),
    (httpx.Response(302, headers={"location": "/ru/user/login"}), "login", "login"),
    (httpx.Response(302, headers={"location": "/ru/error_report/access_denied"}),
     "access_denied", "access_denied"),
    (httpx.Response(302, headers={"location": f"{BASE}/ru/application/docs/{ANN}/777"}),
     "draft_exists", "draft_exists"),
    (httpx.Response(503), "http503", "not_confirmed_closed"),
])
async def test_no_post_unless_the_portal_says_the_tender_is_not_open(create_page, state, reason, capsys):
    async with respx.mock(base_url=BASE, assert_all_called=False) as router:
        post = _portal(router, create_page=create_page, post=httpx.Response(200, json={"status": "ok"}))
        result = await _probe()
        assert post.call_count == 0
    assert (result["sent"], result["create_page"], result["refused"]) == (False, state, reason)
    probe_preopen._print_post_result(result)
    assert "POST НЕ отправлен" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_allow_open_posts_and_reports_the_draft_it_created(capsys):
    async with respx.mock(base_url=BASE, assert_all_called=False) as router:
        post = _portal(router, create_page=httpx.Response(200, html=FORM),
                       post=httpx.Response(200, json={"status": "ok"}), drafts=(None, "777"))
        result = await _probe(allow_open=True)
        assert post.call_count == 1
    assert result["draft_before"] == (True, None) and result["draft_after"] == (True, "777")
    probe_preopen._print_post_result(result)
    printed = capsys.readouterr().out
    assert "черновик после пробы: ЕСТЬ, id 777" in printed and "POST оставил черновик" in printed


@pytest.mark.asyncio
async def test_login_redirects_never_count_as_open():
    # logged out between the reads and the POST: never even allow_open may post
    for create_page in (httpx.Response(302, headers={"location": "/ru/user/login"}),):
        async with respx.mock(base_url=BASE, assert_all_called=False) as router:
            post = _portal(router, create_page=create_page, post=httpx.Response(200, json={}))
            result = await _probe(allow_open=True)
            assert post.call_count == 0 and result["refused"] == "login"

    expired = httpx.Response(302, headers={"location": "/ru/user/login"})
    async with respx.mock(base_url=BASE, assert_all_called=False) as router:
        post = _portal(router, create_page=NOT_FOUND, post=expired)
        result = await _probe()
        assert post.call_count == 1
    assert result["sent"] is True and result["answer"] == "login" and "http_status" not in result


@pytest.mark.asyncio
async def test_declining_the_prompt_sends_nothing(monkeypatch, capsys):
    cfg = SimpleNamespace(ci_session_service="svc", base_url=BASE, proxy_url=None,
                          raw={"company": COMPANY})
    monkeypatch.setattr(probe_preopen.bot_config, "load", lambda path: cfg)
    monkeypatch.setattr(probe_preopen, "_cached_cookie", lambda cfg: "live-session")
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    async with respx.mock(base_url=BASE, assert_all_called=False) as router:
        assert await probe_preopen.post_probe(f"{BASE}/ru/announce/index/{ANN}",
                                              allow_open=False, assume_yes=False) == 1
        assert router.calls.call_count == 0
    assert "ничего не отправлено" in capsys.readouterr().out


def test_announce_card_fields():
    assert probe_preopen._announce_field(CARD, "Срок начала приема заявок") == "2026-12-01 09:00:00"
    assert probe_preopen._announce_field(CARD, "Наименование объявления") is None
