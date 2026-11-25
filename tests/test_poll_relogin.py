"""Pre-T0 poll must survive a ci_session expiry while waiting for the tender.

The cabinet cookie lives ~5 min; a long pre-T0 wait outlives it and every probe
starts redirecting to /user/login. With a relogin callback the poller must
re-authenticate in place and KEEP polling (not crash with SessionExpired), then
proceed once the tender opens. Without a callback it raises as before.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from goszakup import client
from goszakup.client import GoszakupClient, PortalLoginRequired, SessionExpired


def _resp(*, status: int, url: str, text: str = ""):
    return types.SimpleNamespace(
        status_code=status, text=text, content=text.encode(), url=url, next_request=None,
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
    assert sib._transport is not gz._transport
    assert sib._ssl_context is gz._ssl_context
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


# --- the poll hands the open create form to create_application --------------------
# Between "tender is open" and "application created" there used to be an extra GET
# of /application/create/{id} (to read the valid address / IIK options) — the very
# page the poll probe had just downloaded. One whole round-trip at T0 for nothing.

import httpx
import respx

_BASE = "https://v3bl.goszakup.gov.kz"
_CREATE_FORM = (
    '<form action="ajax_create_application">'
    '<select name="subject_address"><option value="0">-</option>'
    '<option value="165932">Астана</option></select>'
    '<select name="iik"><option value="2589827">KZ..</option></select>'
    '<input name="contact_phone"></form>'
)


async def test_opening_poll_skips_not_found_but_follows_draft_redirects():
    async with respx.mock(base_url=_BASE, assert_all_called=False) as router:
        create = router.get("/ru/application/create/17905992").mock(side_effect=[
            httpx.Response(302, headers={"Location": "/ru/error_report/not_found"}),
            httpx.Response(302, headers={"Location": "/ru/application/docs/17905992/123"}),
        ])
        not_found = router.get("/ru/error_report/not_found").respond(404)
        draft = router.get("/ru/application/docs/17905992/123").respond(200)
        async with GoszakupClient(ci_session="dummy") as gz:
            await gz.poll_announce_ready("17905992", interval_s=0.001, max_wait_s=1.0)
        assert create.call_count == 2
        assert draft.call_count == 1
        assert not_found.call_count == 0


@pytest.mark.parametrize("intermediate", [False, True])
async def test_opening_poll_identifies_sso_without_visiting_it(intermediate):
    auth_url = "https://zakup.gov.kz/ru/user/login?state=diagnostic-secret"
    create_path = "/ru/application/create/17905992"
    bridge_path = "/ru/account/login_redirect"

    async def legacy_relogin():
        pytest.fail("Legacy ECP login must not run for a known SSO redirect")

    async with respx.mock(assert_all_called=False) as router:
        create = router.get(_BASE + create_path).respond(
            302, headers={"Location": bridge_path if intermediate else auth_url},
        )
        bridge = router.get(_BASE + bridge_path).respond(302, headers={"Location": auth_url})
        auth = router.get(auth_url).respond(200)
        async with GoszakupClient(ci_session="dummy") as gz:
            with pytest.raises(PortalLoginRequired) as exc:
                await asyncio.wait_for(
                    gz.poll_announce_ready("17905992", max_wait_s=600, relogin=legacy_relogin),
                    timeout=2,
                )
        assert "https://zakup.gov.kz/" in str(exc.value)
        assert "diagnostic-secret" not in str(exc.value)
        assert create.call_count == 1
        assert bridge.call_count == int(intermediate)
        assert not auth.called


async def test_opening_poll_reports_auth_migration_during_legacy_relogin():
    auth_url = "https://zakup.gov.kz/"
    async with respx.mock(assert_all_called=False) as router:
        router.get(_BASE + "/ru/application/create/17905992").respond(
            302, headers={"Location": "/ru/user/login"},
        )
        login = router.get(_BASE + "/ru/user/login").respond(
            302, headers={"Location": auth_url, "Set-Cookie": "ci_session=anonymous; Path=/"},
        )
        nonce = router.post(_BASE + "/ru/user/sendkey/kz").respond(200)
        password = router.post(_BASE + "/ru/user/auth_confirm").respond(200)
        auth = router.get(auth_url).respond(200)
        async with GoszakupClient(ci_session="stale") as gz:
            async def try_legacy_login():
                await gz.relogin_isolated(account_password="test", kalkan_signer=object())

            with pytest.raises(PortalLoginRequired):
                await asyncio.wait_for(
                    gz.poll_announce_ready("17905992", max_wait_s=600, relogin=try_legacy_login),
                    timeout=2,
                )
            assert gz.get_cookie("ci_session") == "stale"
        assert login.call_count == 1
        assert not auth.called
        assert not nonce.called
        assert not password.called


@pytest.mark.parametrize("status", [200, 401])
async def test_opening_poll_does_not_treat_explicit_expiry_as_an_unopened_tender(status):
    form = '<form action="/ru/user/login"><input name="username"></form>'
    async with respx.mock(base_url=_BASE) as router:
        router.get("/ru/application/create/17905992").respond(status, html=form)
        async with GoszakupClient(ci_session="stale") as gz:
            with pytest.raises(SessionExpired) as exc:
                await asyncio.wait_for(gz.poll_announce_ready("17905992", max_wait_s=600), timeout=2)
        assert not isinstance(exc.value, PortalLoginRequired)


async def test_login_does_not_follow_an_unrecognized_external_redirect():
    foreign_url = "https://zakup.gov.kz.example.org/ru/user/login"
    async with respx.mock(assert_all_called=False) as router:
        router.get(_BASE + "/ru/user/login").respond(302, headers={"Location": foreign_url})
        auth = router.get(foreign_url).respond(200)
        nonce = router.post(_BASE + "/ru/user/sendkey/kz").respond(200)
        async with GoszakupClient() as gz:
            with pytest.raises(RuntimeError, match="неизвестный адрес") as exc:
                await gz.login(account_password="test", kalkan_signer=object())
        assert not isinstance(exc.value, PortalLoginRequired)
        assert not auth.called
        assert not nonce.called


async def test_legacy_login_can_still_adopt_the_authenticated_session():
    class Signer:
        async def sign_xml(self, challenge):
            assert "0123456789abcdef" in challenge
            return "<signed>test</signed>"

    async with respx.mock(base_url=_BASE) as router:
        router.get("/ru/user/login").respond(200, html="<html>Login</html>")
        router.post("/ru/user/sendkey/kz").respond(200, text="0123456789abcdef")
        router.post("/user/sendsign/kz").respond(200)
        router.get("/ru/user/auth_confirm").respond(200)
        router.post("/ru/user/auth_confirm").respond(
            302, headers={"Location": "/ru/cabinet/profile"},
        )
        router.get("/ru/cabinet/profile").respond(
            200, headers={"Set-Cookie": "ci_session=authenticated; Path=/"},
        )
        async with GoszakupClient(ci_session="stale") as gz:
            assert await gz.relogin_isolated(account_password="test", kalkan_signer=Signer()) == "authenticated"
            assert gz.get_cookie("ci_session") == "authenticated"


async def test_create_application_reuses_the_form_the_poll_fetched():
    async with respx.mock(base_url=_BASE, assert_all_called=False) as r:
        form = r.get(url__regex=r".*/application/create/17905992").mock(
            return_value=httpx.Response(200, html=_CREATE_FORM))
        create = r.post(url__regex=r".*/ajax_create_application/17905992").mock(
            return_value=httpx.Response(200, json={"status": "ok", "application_id": 73859197}))
        async with GoszakupClient(ci_session="dummy") as gz:
            await gz.poll_announce_ready("17905992", interval_s=0.01, max_wait_s=2.0)
            gets_after_poll = form.call_count
            app_id = await gz.create_application(
                "17905992", subject_address="165932", iik="2589827")
        assert app_id == "73859197"
        assert create.call_count == 1
        assert form.call_count == gets_after_poll      # NOT fetched again for create


async def test_create_uses_decoded_portal_redirect_without_discovery_get():
    async with respx.mock(base_url=_BASE, assert_all_called=False) as router:
        router.get("/ru/application/create/17905992").respond(200, html=_CREATE_FORM)
        router.post("/ru/application/ajax_create_application/17905992").respond(
            200,
            content=rb'{"status":"ok","redirect":"\/ru\/application\/lots\/17905992\/73859197"}',
        )
        discover = router.get("/ru/application/lots/17905992").respond(404)
        async with GoszakupClient(ci_session="dummy") as gz:
            app_id = await gz.create_application("17905992", subject_address="165932", iik="2589827")
    assert app_id == "73859197"
    assert discover.call_count == 0


async def test_create_discovery_uses_redirect_without_loading_lots_page():
    async with respx.mock(base_url=_BASE, assert_all_called=False) as router:
        router.get("/ru/application/create/17905992").respond(200, html=_CREATE_FORM)
        create = router.post("/ru/application/ajax_create_application/17905992").respond(200, json={"status": "ok"})
        discover = router.get("/ru/application/lots/17905992").respond(
            302, headers={"Location": "/ru/application/lots/17905992/73859197"},
        )
        lots = router.get("/ru/application/lots/17905992/73859197").respond(200)
        async with GoszakupClient(ci_session="dummy") as gz:
            app_id = await gz.create_application("17905992", subject_address="165932", iik="2589827")
    assert app_id == "73859197"
    assert create.call_count == discover.call_count == 1
    assert lots.call_count == 0


@pytest.mark.parametrize("rotate_session", [False, True])
async def test_create_overlaps_at_most_one_id_read_after_the_baseline(monkeypatch, rotate_session):
    monkeypatch.setattr(client, "_EARLY_ID_PROBE_DELAY_S", 0.01)
    candidate_ready = asyncio.Event()
    speculative_reads = 0
    ann = "17905992"
    application = "73859197"
    other_application = "73859198"

    async def answer_create(request):
        await asyncio.wait_for(candidate_ready.wait(), 1)
        await asyncio.sleep(0)
        headers = {"Set-Cookie": "ci_session=rotated; Path=/"} if rotate_session else {}
        return httpx.Response(200, json={"status": "ok"}, headers=headers)

    def read_lots(request):
        nonlocal speculative_reads
        if request.headers.get("X-Requested-With") != "XMLHttpRequest":
            return httpx.Response(302, headers={"Location": f"/ru/application/lots/{ann}/{application}"})
        speculative_reads += 1
        if speculative_reads == 1:
            return httpx.Response(302, headers={"Location": f"/ru/application/create/{ann}"})
        candidate_ready.set()
        candidate = other_application if rotate_session else application
        return httpx.Response(200, html=f'<a href="/ru/application/lots/{ann}/{candidate}">Лоты</a>')

    async with respx.mock(base_url=_BASE) as router:
        router.get(f"/ru/application/create/{ann}").respond(200, html=_CREATE_FORM)
        create = router.post(f"/ru/application/ajax_create_application/{ann}").mock(side_effect=answer_create)
        read = router.get(f"/ru/application/lots/{ann}").mock(side_effect=read_lots)
        async with GoszakupClient(ci_session="dummy") as gz:
            app_id = await gz.create_application(
                ann, subject_address="165932", iik="2589827", early_id=True,
            )
    assert app_id == application
    assert create.call_count == 1
    assert speculative_reads == 2
    assert read.call_count == (3 if rotate_session else 2)


@pytest.mark.parametrize("target", [
    "/ru/user/login",
    "/ru/application/create/17905993",
    "https://example.org/ru/application/create/17905992",
])
async def test_unrelated_redirect_cannot_confirm_absence_of_an_earlier_draft(monkeypatch, target):
    async def redirect(path):
        return httpx.Response(302, headers={"Location": target})

    async with GoszakupClient(ci_session="dummy") as gz:
        monkeypatch.setattr(gz, "peek", redirect)
        assert await gz._read_application_id("17905992") == (False, None)


@pytest.mark.parametrize("target", [
    "/ru/application/lots/17905993/73859197",
    "https://example.org/ru/application/lots/17905992/73859197",
])
async def test_create_id_does_not_use_redirect_from_another_announcement_or_origin(target):
    async with GoszakupClient() as gz:
        assert gz._extract_app_id_from_response(httpx.Response(200, json={"url": target}), "17905992") is None
        assert gz._extract_app_id_from_response(httpx.Response(302, headers={"Location": target}), "17905992") is None


async def test_create_application_still_validates_options_from_the_polled_form():
    """The saved round-trip must not cost the safety net: a stale config address
    is still corrected from the form's real <option>s."""
    seen = {}

    async with respx.mock(base_url=_BASE, assert_all_called=False) as r:
        r.get(url__regex=r".*/application/create/17905992").mock(
            return_value=httpx.Response(200, html=_CREATE_FORM))

        @r.post(url__regex=r".*/ajax_create_application/17905992")
        def create(request):
            seen["body"] = request.content.decode()
            return httpx.Response(200, json={"application_id": 1})

        async with GoszakupClient(ci_session="dummy") as gz:
            await gz.poll_announce_ready("17905992", interval_s=0.01, max_wait_s=2.0)
            await gz.create_application("17905992", subject_address="999", iik="888")
    assert "subject_address=165932" in seen["body"]    # picked from the form
    assert "iik=2589827" in seen["body"]


async def test_create_application_without_a_poll_fetches_the_form_itself():
    async with respx.mock(base_url=_BASE, assert_all_called=False) as r:
        form = r.get(url__regex=r".*/application/create/17905992").mock(
            return_value=httpx.Response(200, html=_CREATE_FORM))
        r.post(url__regex=r".*/ajax_create_application/17905992").mock(
            return_value=httpx.Response(200, json={"application_id": 1}))
        async with GoszakupClient(ci_session="dummy") as gz:
            await gz.create_application("17905992", subject_address="165932", iik="2589827")
        assert form.call_count == 1                      # unchanged fallback


# --- one session cookie, never a stale duplicate ---------------------------------
# httpx keeps cookies per (domain, path, name). The seeded ci_session used to sit
# under an empty domain, so the server's rotated cookie landed NEXT to it and both
# were sent — stale one first, which is the one PHP reads.

def _cookie_header(request) -> str:
    return request.headers.get("cookie", "")


async def test_rotated_session_cookie_replaces_the_seeded_one():
    sent = []
    async with respx.mock(base_url=_BASE, assert_all_called=False) as r:
        @r.get("/ru/cabinet/profile")
        def profile(request):
            sent.append(_cookie_header(request))
            return httpx.Response(200, headers={"set-cookie": "ci_session=NEW; Path=/"})

        async with GoszakupClient(ci_session="OLD") as gz:
            await gz._http.get("/ru/cabinet/profile")
            await gz._http.get("/ru/cabinet/profile")
            values = [c.value for c in gz._http.cookies.jar if c.name == "ci_session"]
    assert sent[0] == "ci_session=OLD"
    assert sent[1] == "ci_session=NEW"                   # only the fresh one, once
    assert values == ["NEW"]


async def test_domain_scoped_rotation_does_not_leave_a_stale_duplicate():
    sent = []
    async with respx.mock(base_url=_BASE, assert_all_called=False) as r:
        @r.get("/ru/cabinet/profile")
        def profile(request):
            sent.append(_cookie_header(request))
            return httpx.Response(200, headers={
                "set-cookie": "ci_session=NEW; Domain=.goszakup.gov.kz; Path=/"})

        async with GoszakupClient(ci_session="OLD") as gz:
            await gz._http.get("/ru/cabinet/profile")
            await gz._http.get("/ru/cabinet/profile")
            assert gz.get_cookie("ci_session") == "NEW"
            values = [c.value for c in gz._http.cookies.jar if c.name == "ci_session"]
    assert sent[1].count("ci_session=") == 1
    assert "ci_session=NEW" in sent[1]
    assert values == ["NEW"]


async def test_full_cookie_header_seed_is_bound_to_the_host():
    gz = GoszakupClient(
        ci_session="ci_session=S1; BNES_ci_session=B1; csrf_cookie=C1",
        base_url=_BASE)
    jar = {c.name: (c.value, c.domain, c.path) for c in gz._http.cookies.jar}
    assert jar == {
        "ci_session": ("S1", "v3bl.goszakup.gov.kz", "/"),
        "BNES_ci_session": ("B1", "v3bl.goszakup.gov.kz", "/"),
        "csrf_cookie": ("C1", "v3bl.goszakup.gov.kz", "/"),
    }
    await gz.aclose()


async def test_sync_cookies_gives_a_sibling_the_whole_session():
    """A per-document sibling must present what the main client presents — the
    session AND the WAF companions (BNES_*), not a bare ci_session."""
    gz = GoszakupClient(
        ci_session="ci_session=S1; BNES_ci_session=B1; csrf_cookie=C1",
        agreement_cookie="AGREE", base_url=_BASE)
    sib = gz.clone_unauthenticated()
    sib.adopt_ci_session("STALE")                        # an older clone
    gz.sync_cookies_to(sib)
    got = sorted((c.name, c.value) for c in sib._http.cookies.jar)
    assert got == [("BNES_ci_session", "B1"), ("agreement_user_2024", "AGREE"),
                   ("ci_session", "S1"), ("csrf_cookie", "C1")]
    await sib.aclose()
    await gz.aclose()


async def test_warm_connection_never_touches_the_session():
    """Pre-opening a connection must not present our cookies (PHP would load —
    and could rotate — the session) nor absorb a Set-Cookie into the jar."""
    seen = {}
    async with respx.mock(base_url=_BASE, assert_all_called=False) as r:
        @r.head("/ru/")
        def head(request):
            seen["cookie"] = request.headers.get("cookie")
            return httpx.Response(200, headers={"set-cookie": "ci_session=ANON; Path=/"})

        async with GoszakupClient(ci_session="MAIN") as gz:
            assert await gz.warm_connection() is True
            assert gz.get_cookie("ci_session") == "MAIN"
    assert seen["cookie"] is None


async def test_warm_connection_failure_is_swallowed():
    async with respx.mock(base_url=_BASE, assert_all_called=False) as r:
        r.head("/ru/").mock(side_effect=httpx.ConnectError("refused"))
        async with GoszakupClient(ci_session="MAIN") as gz:
            assert await gz.warm_connection() is False


# --- тех.спец signature cache -----------------------------------------------------

class _CountingHttp:
    base_url = _BASE

    def __init__(self):
        self.gets = 0

    async def get(self, url, headers=None):
        self.gets += 1
        return httpx.Response(200, content=b"PDF-BYTES", request=httpx.Request("GET", _BASE + url))


class _FakeKalkan:
    def __init__(self, key_id):
        self.key_id = key_id
        self.signed = 0

    async def sign_bytes(self, data):
        self.signed += 1
        return f"SIG-{self.key_id}-{self.signed}"


async def test_sign_file_skips_the_download_for_a_known_file_id():
    from goszakup.local_signer import LocalSigner
    http, kalkan, cache = _CountingHttp(), _FakeKalkan("cert-a"), {}
    ns = LocalSigner(http_client=http, kalkan_signer=kalkan)
    url = _BASE + "/files/download_file/111/"

    first = await ns.sign_file(url, sig_cache=cache, cache_key="fid:111")
    second = await ns.sign_file(url, sig_cache=cache, cache_key="fid:111")
    assert first == second == "SIG-cert-a-1"
    assert http.gets == 1                                # 2nd call: no download at all
    assert kalkan.signed == 1


async def test_signature_cache_is_not_shared_across_certificates():
    """After the ECP is renewed, signatures made with the old certificate must not
    be served — they'd be rejected by the portal right at T0."""
    from goszakup.local_signer import LocalSigner
    http, cache = _CountingHttp(), {}
    url = _BASE + "/files/download_file/111/"

    old = LocalSigner(http_client=http, kalkan_signer=_FakeKalkan("cert-old"))
    await old.sign_file(url, sig_cache=cache, cache_key="fid:111")

    new_kalkan = _FakeKalkan("cert-new")
    new = LocalSigner(http_client=http, kalkan_signer=new_kalkan)
    sig = await new.sign_file(url, sig_cache=cache, cache_key="fid:111")
    assert sig == "SIG-cert-new-1"                       # re-signed with the new key
    assert new_kalkan.signed == 1


async def test_sign_file_without_cache_key_still_downloads():
    """Generated documents (a new file each time) pass no cache_key — they must
    always be fetched; only the content-hash lookup applies."""
    from goszakup.local_signer import LocalSigner
    http, kalkan, cache = _CountingHttp(), _FakeKalkan("cert-a"), {}
    ns = LocalSigner(http_client=http, kalkan_signer=kalkan)
    url = _BASE + "/files/download_file/111/"
    await ns.sign_file(url, sig_cache=cache)
    await ns.sign_file(url, sig_cache=cache)
    assert http.gets == 2                                # downloaded both times
    assert kalkan.signed == 1                            # same content -> signed once
