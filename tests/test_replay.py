"""HAR-replay tests for the AJAX layer.

Loads the recorded HAR fixture and verifies that GoszakupClient produces
the exact same wire-level requests (path, headers, body shape) as Chrome
did during a real submission. No NCALayer, no live network.

The HAR's response bodies are mostly empty (DevTools didn't save content
for most entries), so we can't assert on response parsing here -- the
live test covers that. What we CAN check is request fidelity.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qsl, unquote

import httpx
import pytest
import respx

from goszakup.client import GoszakupClient, LotDocumentSaveUnconfirmed

HAR_FILE = Path(__file__).parent / "fixtures" / "v3bl.goszakup.gov.kz.har"


def _load_posts() -> list[dict]:
    har = json.loads(HAR_FILE.read_text())
    entries = har["log"]["entries"]
    return [
        e
        for e in entries
        if "goszakup.gov.kz" in e["request"]["url"]
        and e["request"]["method"] == "POST"
    ]


def _body_dict(text: str) -> dict[str, list[str]]:
    """Parse application/x-www-form-urlencoded into a dict with list values."""
    out: dict[str, list[str]] = {}
    for k, v in parse_qsl(text, keep_blank_values=True):
        out.setdefault(k, []).append(v)
    return out


@pytest.fixture
def har_posts():
    # The HAR carries live cookies, so it's gitignored and absent on most
    # checkouts. Skip (don't error) the request-shape replays when it isn't
    # there; copy a capture to tests/fixtures/v3bl.goszakup.gov.kz.har to run them.
    if not HAR_FILE.is_file():
        pytest.skip(f"HAR fixture not present: {HAR_FILE}")
    return _load_posts()


async def _run_with_mock(routes_setup, fn):
    async with respx.mock(base_url="https://v3bl.goszakup.gov.kz", assert_all_called=False) as r:
        routes_setup(r)
        async with GoszakupClient(ci_session="dummy") as gz:
            return await fn(gz)


async def test_lost_lot_signature_response_reports_unconfirmed_save():
    path = "/ru/application/show_doc/12345678/23456789/3334/34567890/1"
    async with respx.mock(base_url="https://v3bl.goszakup.gov.kz") as router:
        route = router.post(path).mock(side_effect=httpx.ReadTimeout(""))
        async with GoszakupClient(ci_session="dummy") as client:
            with pytest.raises(LotDocumentSaveUnconfirmed, match="3334.*страница 1.*ReadTimeout") as exc:
                await client.sign_lot_doc("12345678", "23456789", 3334, 34567890, 1, signatures={42: "CMS"})

    assert exc.value.app_id == "23456789"
    assert route.call_count == 1


async def test_create_application_request_shape(har_posts) -> None:
    har = next(e for e in har_posts if "ajax_create_application" in e["request"]["url"])
    expected = _body_dict(har["request"]["postData"]["text"])

    captured = {}

    def setup(r):
        @r.post(url__regex=r".*/ajax_create_application/\d+")
        def handler(request):
            captured["body"] = _body_dict(request.content.decode())
            captured["referer"] = request.headers.get("Referer")
            captured["x-requested-with"] = request.headers.get("X-Requested-With")
            return httpx.Response(200, json={"status": "ok", "application_id": 71087438})

    async def call(gz):
        return await gz.create_application(
            "16941324",
            subject_address="799063",
            iik="4346988",
            contact_phone="+77777777777",
        )

    await _run_with_mock(setup, call)
    assert captured["body"] == expected
    assert captured["x-requested-with"] == "XMLHttpRequest"
    assert "/ru/application/create/16941324" in captured["referer"]


async def test_add_lots_request_shape(har_posts) -> None:
    har = next(e for e in har_posts if "ajax_add_lots" in e["request"]["url"])
    expected = _body_dict(har["request"]["postData"]["text"])

    captured = {}

    def setup(r):
        @r.post(url__regex=r".*/ajax_add_lots/\d+/\d+")
        def handler(request):
            captured["body"] = _body_dict(request.content.decode())
            return httpx.Response(200, text="")

    async def call(gz):
        return await gz.add_lots("16941324", "71087438", [41670316])

    await _run_with_mock(setup, call)
    assert captured["body"] == expected


async def test_lots_next_request_shape(har_posts) -> None:
    har = next(e for e in har_posts if "ajax_lots_next" in e["request"]["url"])
    expected = _body_dict(har["request"]["postData"]["text"])

    captured = {}

    def setup(r):
        @r.post(url__regex=r".*/ajax_lots_next/\d+/\d+")
        def handler(request):
            captured["body"] = _body_dict(request.content.decode())
            return httpx.Response(200, text="")

    async def call(gz):
        return await gz.lots_next("16941324", "71087438")

    await _run_with_mock(setup, call)
    assert captured["body"] == expected


async def test_public_application_request_shape(har_posts) -> None:
    har = next(e for e in har_posts if "ajax_public_application" in e["request"]["url"])
    expected = _body_dict(har["request"]["postData"]["text"])

    captured = {}

    def setup(r):
        @r.post(url__regex=r".*/ajax_public_application/\d+/\d+")
        def handler(request):
            captured["body"] = _body_dict(request.content.decode())
            return httpx.Response(200, json={"status": "ok"})

    async def call(gz):
        return await gz.public_application("16941324", "71087438")

    result = await _run_with_mock(setup, call)
    assert captured["body"] == expected
    assert result["status"] == "ok"


async def test_post_form_raises_session_expired_on_login_redirect() -> None:
    """Critical: POST helpers must surface dead cookie as SessionExpired,
    not let downstream parsers read the login HTML as ajax JSON.
    """
    from goszakup.client import SessionExpired

    def setup(r):
        @r.post(url__regex=r".*/ajax_create_application/\d+")
        def handler(request):
            # CodeIgniter-style: dead cookie → 302 to login
            return httpx.Response(302, headers={"location": "/ru/user/login"})

    async def call(gz):
        return await gz.create_application(
            "16941324",
            subject_address="x", iik="x", contact_phone="x",
        )

    with pytest.raises(SessionExpired):
        await _run_with_mock(setup, call)


async def test_post_form_raises_session_expired_on_200_login_html() -> None:
    """Some endpoints return 200 with login HTML instead of 302."""
    from goszakup.client import SessionExpired

    login_html = b"<!doctype html><html><body><form action='/ru/user/login'>"
    login_html += b"<input name=\"username\" /><input name=\"password\" />" + b"x" * 1024
    login_html += b"</form></body></html>"

    def setup(r):
        @r.post(url__regex=r".*/ajax_create_application/\d+")
        def handler(request):
            return httpx.Response(
                200, content=login_html,
                headers={"content-type": "text/html; charset=utf-8"},
            )

    async def call(gz):
        return await gz.create_application(
            "16941324",
            subject_address="x", iik="x", contact_phone="x",
        )

    with pytest.raises(SessionExpired):
        await _run_with_mock(setup, call)


async def test_priceoffer_xml_structure(har_posts) -> None:
    """The XML signed in add_priceoffers must match the format observed."""
    from goszakup.pipeline import _build_priceoffer_xml

    xml = _build_priceoffer_xml(
        announce_id="16941324",
        tender_lot_id=41670316,
        point_id=41126151,
        price=0,
    )

    har = next(e for e in har_posts if "ajax_add_priceoffers" in e["request"]["url"])
    har_body = _body_dict(har["request"]["postData"]["text"])
    # xmlData[41126151] is URL-encoded; unquote and compare
    xml_keys = [k for k in har_body if k.startswith("xmlData")]
    assert xml_keys, "HAR should contain xmlData fields"
    har_xml = har_body[xml_keys[0]][0]

    # The actual values use point 41126151 in this HAR; our test built the
    # same. We compare a normalized form.
    def norm(s: str) -> str:
        return s.replace(" ", "").replace("\n", "")

    assert norm(xml) == norm(har_xml), (
        f"\nGOT:      {norm(xml)}"
        f"\nEXPECTED: {norm(har_xml)}"
    )


# ---------------------------------------------------------------------------
# poll_announce_ready — pre-T0 waiting behaviour
#
# Empirically (tools/probe_preopen.py), a tender whose приём-заявок window
# hasn't opened yet returns 404 → /ru/error_report/not_found for
# /ru/application/create/{id}, and flips to 200+form the moment it opens.
# The poll MUST keep waiting through the 404 phase, not fast-fail.
# ---------------------------------------------------------------------------

async def test_poll_waits_through_error_report_not_found() -> None:
    """Regression: a not-yet-open tender 404s to /error_report/not_found.
    Previously the poll raised RuntimeError on the first attempt, so the bot
    never waited for the tender to open. It must now keep polling and bound
    on the deadline (TimeoutError), not RuntimeError.
    """
    def setup(r):
        @r.get(url__regex=r".*/application/create/16941324")
        def create(request):
            return httpx.Response(302, headers={"location": "/ru/error_report/not_found"})

        @r.get(url__regex=r".*/error_report/not_found")
        def err(request):
            return httpx.Response(404, html="<h1>404</h1>")

    async def call(gz):
        return await gz.poll_announce_ready("16941324", interval_s=0.01, max_wait_s=0.1)

    with pytest.raises(TimeoutError):
        await _run_with_mock(setup, call)


async def test_poll_returns_when_tender_opens() -> None:
    """Pre-T0 the create page 404s; at T0 it flips to 200+form. The poll
    must detect that flip and return cleanly."""
    calls = {"n": 0}

    def setup(r):
        @r.get(url__regex=r".*/application/create/16941324")
        def create(request):
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(302, headers={"location": "/ru/error_report/not_found"})
            return httpx.Response(200, html='<form><input name="subject_address"></form>')

        @r.get(url__regex=r".*/error_report/not_found")
        def err(request):
            return httpx.Response(404, html="not found")

    async def call(gz):
        return await gz.poll_announce_ready("16941324", interval_s=0.01, max_wait_s=2.0)

    await _run_with_mock(setup, call)   # returns None (ready), no raise
    assert calls["n"] >= 3


async def test_poll_fast_fails_on_access_denied() -> None:
    """Genuine ineligibility (access_denied) never clears by waiting — must
    fail fast with RuntimeError, not poll until the deadline."""
    def setup(r):
        @r.get(url__regex=r".*/application/create/16941324")
        def create(request):
            return httpx.Response(302, headers={"location": "/ru/error_report/access_denied"})

        @r.get(url__regex=r".*/error_report/access_denied")
        def err(request):
            return httpx.Response(403, html="denied")

    async def call(gz):
        return await gz.poll_announce_ready("16941324", interval_s=0.01, max_wait_s=2.0)

    with pytest.raises(RuntimeError):
        await _run_with_mock(setup, call)
