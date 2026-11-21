"""Pre-T0 poll must survive a ci_session expiry while waiting for the tender.

The cabinet cookie lives ~5 min; a long pre-T0 wait outlives it and every probe
starts redirecting to /user/login. With a relogin callback the poller must
re-authenticate in place and KEEP polling (not crash with SessionExpired), then
proceed once the tender opens. Without a callback it raises as before.
"""

from __future__ import annotations

import types

import pytest

from goszakup.client import GoszakupClient, SessionExpired


def _resp(*, status: int, url: str, text: str = ""):
    return types.SimpleNamespace(
        status_code=status, text=text, content=text.encode(), url=url,
    )


_LOGIN = "https://v3bl.goszakup.gov.kz/ru/user/login"
_READY_BODY = '<form><input name="subject_address"></form>'


class _FakeHttp:
    """Scripted GET responses: dead cookie -> /user/login, until relogin flips
    `logged_in`, after which probes see the open create form."""

    def __init__(self):
        self.logged_in = False
        self.gets = 0

    async def aclose(self):
        return None

    async def get(self, path, follow_redirects=True):
        self.gets += 1
        if not self.logged_in:
            return _resp(status=200, url=_LOGIN)  # redirected to login
        ready_url = "https://v3bl.goszakup.gov.kz/ru/application/create/17051323"
        return _resp(status=200, url=ready_url, text=_READY_BODY)


async def test_poll_reloggs_in_and_continues(monkeypatch):
    gz = GoszakupClient(base_url="https://v3bl.goszakup.gov.kz")
    fake = _FakeHttp()
    gz._http = fake
    calls = {"n": 0}

    async def relogin():
        calls["n"] += 1
        fake.logged_in = True  # cookie refreshed -> next probes succeed

    # tight cadence so the test is instant
    await gz.poll_announce_ready(
        "17051323", interval_s=0.001, max_wait_s=5.0, relogin=relogin,
    )
    assert calls["n"] == 1            # re-logged in exactly once
    assert fake.gets >= 2            # probed, hit login, then probed again -> ready
    await gz.aclose()


async def test_poll_without_relogin_raises(monkeypatch):
    gz = GoszakupClient(base_url="https://v3bl.goszakup.gov.kz")
    fake = _FakeHttp()  # stays logged_in=False forever
    gz._http = fake

    with pytest.raises(SessionExpired):
        await gz.poll_announce_ready(
            "17051323", interval_s=0.001, max_wait_s=5.0, relogin=None,
        )
    await gz.aclose()


async def test_poll_relogin_failure_is_fatal(monkeypatch):
    gz = GoszakupClient(base_url="https://v3bl.goszakup.gov.kz")
    fake = _FakeHttp()
    gz._http = fake

    async def relogin():
        raise RuntimeError("ECP signer down")

    with pytest.raises(SessionExpired):
        await gz.poll_announce_ready(
            "17051323", interval_s=0.001, max_wait_s=5.0, relogin=relogin,
        )
    await gz.aclose()


# --- relogin race fix: the multi-step login must run on an ISOLATED clone --------
# Bug: during an in-poll relogin the poll loop keeps firing probes on the shared
# gz._http; a probe 302-ing to /user/login Set-Cookies a fresh ci_session and
# clobbers the login mid-flow, so the password step 302s to access_denied.

async def test_clone_unauthenticated_isolates_jar():
    gz = GoszakupClient(ci_session="MAIN", agreement_cookie="AGREE",
                        base_url="https://v3bl.goszakup.gov.kz")
    sib = gz.clone_unauthenticated()
    assert sib is not gz
    assert sib._http is not gz._http                     # private jar/transport
    assert sib.base_url == gz.base_url
    assert sib._http.cookies.get("agreement_user_2024") == "AGREE"
    assert sib._http.cookies.get("ci_session") is None   # unauthenticated
    assert gz._http.cookies.get("ci_session") == "MAIN"  # original untouched
    await sib.aclose()
    await gz.aclose()


async def test_adopt_ci_session_replaces_cookie():
    gz = GoszakupClient(ci_session="OLD", base_url="https://v3bl.goszakup.gov.kz")
    gz.adopt_ci_session("NEW")
    assert gz._http.cookies.get("ci_session") == "NEW"
    names = [c.name for c in gz._http.cookies.jar if c.name == "ci_session"]
    assert names == ["ci_session"]                       # exactly one (old cleared)
    await gz.aclose()


async def test_relogin_isolated_logs_in_on_clone_then_adopts(monkeypatch):
    gz = GoszakupClient(ci_session="DEAD", agreement_cookie="AGREE",
                        base_url="https://v3bl.goszakup.gov.kz")
    seen = {}

    async def fake_login(self, *, account_password, kalkan_signer):
        seen["is_main"] = self is gz                     # which client logs in?
        seen["main_jar_touched"] = self._http is gz._http
        return "FRESH"

    monkeypatch.setattr(GoszakupClient, "login", fake_login)
    ci = await gz.relogin_isolated(account_password="pw", kalkan_signer=object())
    assert ci == "FRESH"
    assert seen["is_main"] is False                      # login ran on a clone
    assert seen["main_jar_touched"] is False             # shared jar never used
    assert gz._http.cookies.get("ci_session") == "FRESH"  # result adopted
    await gz.aclose()
