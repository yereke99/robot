"""Warmup of the signature cache using public tender files."""

from __future__ import annotations

import hashlib

import httpx
import pytest
import respx

from goszakup import flow, presign
from goszakup.client import GoszakupClient, reply_shape
from goszakup.local_signer import LocalSigner

BASE = "https://v3bl.goszakup.gov.kz"
ANN = "555"

# The shape of the announcement's file list (actionAjaxModalShowFiles/<ann>/3334):
# the тех.спец table, then "Дополнительные файл". Names of people and
# organisations in the real list are replaced here.
def _row(lot: str, fid: int, token: int, name: str) -> str:
    return (f'<tr> <td> {lot} </td> <td> <a href="{BASE}/files/download_file/{fid}/{token}/">'
            f"{name}</a> </td> <td> Автор </td> <td> Заказчик </td> <td> 2026-11-18 16:00:19 </td>"
            f' <td style="word-break: break-word;"> <a href="https://old.goszakup.gov.kz/ru/files/'
            f'signature/download_cms/m/{fid}">Скачать подпись</a> </td> </tr>')


HEAD = ('<table class="table table-bordered"> <tr> <th> Номер лота </th> <th> Документ </th>'
        " <th> Автор </th> <th> Организация </th> <th> Дата создания </th> <th> Подпись </th> </tr>")
LISTING = (
    HEAD + _row("88452090-ОК1", 319634510, 229603435, "techspec_88452090.pdf") + "</table>"
    "<br/><br/> <h4>Дополнительные файл</h4>" + HEAD
    + _row("88452090-ОК1", 319609420, 229603531, "DefAktV2_1-01-00-01_RU.xlsx")
    + _row("88452090-ОК1", 319609478, 229603571, "LS1-01-00-01_RU.xlsx")
    + "</table>")


class FakeKalkan:
    key_id = "key1"

    def __init__(self):
        self.signed: list[bytes] = []

    async def sign_bytes(self, data: bytes) -> str:
        self.signed.append(data)
        return "SIG-" + data.decode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- the file list
def test_listing_rows_become_files_of_their_lot():
    files = presign.parse_tender_files(LISTING, "v3bl.goszakup.gov.kz")
    assert [(f.lot, f.fid, f.path, f.name) for f in files] == [
        (88452090, 319634510, "/files/download_file/319634510/229603435/", "techspec_88452090.pdf"),
        (88452090, 319609420, "/files/download_file/319609420/229603531/", "DefAktV2_1-01-00-01_RU.xlsx"),
        (88452090, 319609478, "/files/download_file/319609478/229603571/", "LS1-01-00-01_RU.xlsx"),
    ]


def test_listing_ignores_foreign_hosts_tokenless_and_repeated_links():
    html = (HEAD
            + _row("1-ОК1", 10, 20, "a.pdf")
            + _row("1-ОК1", 10, 20, "a.pdf")                                   # the same file again
            + '<tr><td>1-ОК1</td><td><a href="https://elsewhere.example/files/download_file/11/21/">'
              "b.pdf</a></td></tr>"
            + '<tr><td>1-ОК1</td><td><a href="/files/download_file/12/">c.pdf</a></td></tr>'
            + '<tr><td>без номера</td><td><a href="/files/download_file/13/23/">d &amp; e.pdf</a></td></tr>'
            + "</table>")
    files = presign.parse_tender_files(html, "v3bl.goszakup.gov.kz")
    assert [(f.lot, f.fid, f.name) for f in files] == [(None, 10, "a.pdf"), (None, 13, "d & e.pdf")]
    # "1-ОК1" starts with one digit: too short to be a lot id, so no lot is claimed


# ------------------------------------------------------------------- presign.run
async def _presign(router, *, lot_ids=(88452090,), cache=None):
    cache = {} if cache is None else cache
    kalkan = FakeKalkan()
    async with GoszakupClient(ci_session="live-session", base_url=BASE) as gz:
        async with gz.clone_unauthenticated() as anonymous:
            signer = LocalSigner(kalkan_signer=kalkan, http_client=anonymous._http)
            await presign.run(anonymous, signer, announce_id=ANN, lot_ids=list(lot_ids),
                              doc_id=3334, sig_cache=cache, main_http=gz._http)
    return cache, kalkan


def _with_session(router) -> list[str]:
    return [str(call.request.url) for call in router.calls
            if "ci_session" in call.request.headers.get("cookie", "")]


@pytest.mark.asyncio
async def test_run_signs_the_public_files_without_presenting_the_session():
    listing_url = f"{presign.PUBLIC_PORTAL}/ru/announce/actionAjaxModalShowFiles/{ANN}/3334"
    moved = f"https://old.goszakup.gov.kz/ru/announce/actionAjaxModalShowFiles/{ANN}/3334"
    cache: dict = {}
    async with respx.mock(assert_all_called=False) as router:
        router.get(listing_url).mock(return_value=httpx.Response(302, headers={"location": moved}))
        router.get(moved).mock(return_value=httpx.Response(200, html=LISTING))
        downloads = router.get(url__regex=rf"{BASE}/files/download_file/(\d+)/\d+/$").mock(
            side_effect=lambda request: httpx.Response(
                200, content=("bytes-of-" + request.url.path.split("/")[3]).encode()))
        _, kalkan = await _presign(router, cache=cache)
        _, repeat = await _presign(router, cache=cache)
        assert _with_session(router) == []
        assert downloads.call_count == 3

    signature = "SIG-bytes-of-319634510"
    assert cache[f"key1:{_sha(b'bytes-of-319634510')}"] == signature
    assert cache["key1:public_fid:319634510"] == signature
    assert "key1:fid:319634510" not in cache
    assert len(kalkan.signed) == 3
    assert repeat.signed == []


@pytest.mark.asyncio
async def test_run_reads_the_list_on_the_main_client_when_the_public_one_is_down():
    async with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://goszakup\.gov\.kz/.*").mock(side_effect=httpx.ConnectError("down"))
        cabinet = router.get(f"{BASE}/ru/announce/actionAjaxModalShowFiles/{ANN}/3334").mock(
            return_value=httpx.Response(200, html=LISTING))
        router.get(url__regex=rf"{BASE}/files/download_file/(\d+)/\d+/$").mock(
            return_value=httpx.Response(200, content=b"x"))
        cache, _kalkan = await _presign(router)
        assert sum(":public_fid:" in key for key in cache) == 3
        # the one session-bearing request went out on the MAIN client
        assert _with_session(router) == [str(cabinet.calls.last.request.url)]


@pytest.mark.asyncio
async def test_run_keeps_only_the_chosen_lots_and_skips_files_it_is_not_served():
    listing = (HEAD + _row("11111111-ОК1", 1, 2, "a.pdf") + _row("22222222-ОК1", 3, 4, "b.pdf")
               + _row("11111111-ОК1", 5, 6, "c.pdf") + "</table>")
    async with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://goszakup\.gov\.kz/.*").mock(
            return_value=httpx.Response(200, html=listing))
        router.get(f"{BASE}/files/download_file/1/2/").mock(return_value=httpx.Response(200, content=b"A"))
        other_lot = router.get(f"{BASE}/files/download_file/3/4/").mock(
            return_value=httpx.Response(200, content=b"B"))
        router.get(f"{BASE}/files/download_file/5/6/").mock(
            return_value=httpx.Response(302, headers={"location": "/ru/user/login"}))
        cache, kalkan = await _presign(router, lot_ids=(11111111,))

    assert not other_lot.called                            # the other lot's file is not fetched
    assert kalkan.signed == [b"A"]
    assert "key1:public_fid:1" in cache
    assert "key1:public_fid:5" not in cache


@pytest.mark.asyncio
async def test_run_without_any_list_leaves_nothing_presigned():
    async with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r".*actionAjaxModalShowFiles.*").mock(
            return_value=httpx.Response(302, headers={"location": "/ru/user/login"}))
        router.get(url__regex=r".*/ru/user/login$").mock(
            return_value=httpx.Response(200, html="<form>Вход</form>"))
        cache, kalkan = await _presign(router)
    assert not cache and not kalkan.signed


@pytest.mark.asyncio
async def test_peek_sends_the_session_but_never_stores_a_new_one():
    anonymous = {"Set-Cookie": "ci_session=anonymous-visitor; Path=/"}
    async with respx.mock(base_url=BASE, assert_all_called=False) as router:
        route = router.get("/ru/application/lots/1").mock(
            return_value=httpx.Response(302, headers={**anonymous, "location": "/ru/user/login"}))
        async with GoszakupClient(ci_session="live-session", base_url=BASE) as gz:
            reply = await gz.peek("/ru/application/lots/1")
            sent = route.calls.last.request
            assert reply.status_code == 302 and str(reply.url).endswith("/ru/application/lots/1")
            assert "ci_session=live-session" in sent.headers["cookie"]
            assert sent.headers["x-requested-with"] == "XMLHttpRequest"   # never rotates the id
            assert gz.get_cookie("ci_session") == "live-session"          # the jar is untouched

            await gz._http.get("/ru/application/lots/1")                 # an ordinary read would
            assert gz.get_cookie("ci_session") == "anonymous-visitor"     # have lost the session


@pytest.mark.asyncio
@pytest.mark.parametrize(("reply", "expected"), [
    (httpx.Response(302, headers={"location": "/ru/application/create/1"}), (True, None)),
    (httpx.Response(302, headers={"location": "/ru/application/lots/1/77"}), (True, "77")),
    (httpx.Response(302, headers={"location": "/ru/application/lots/2/77"}), (False, None)),
    (httpx.Response(200, html='<a href="/ru/application/lots/1/77">x</a>'
                              '<form action="/ru/application/ajax_add_lots/1/77">'), (True, "77")),
    (httpx.Response(200, html="<h1>Заявок нет</h1>"), (False, None)),
    (httpx.Response(200, html='<a href="/ru/application/lots/1/77"></a>'
                              '<a href="/ru/application/docs/1/78"></a>'), (False, None)),
    (httpx.Response(503), (False, None)),
])
async def test_reading_the_draft_id(reply, expected):
    async with respx.mock(base_url=BASE, assert_all_called=False) as router:
        router.get("/ru/application/lots/1").mock(return_value=reply)
        async with GoszakupClient(ci_session="s", base_url=BASE) as gz:
            assert await gz._read_application_id("1") == expected


@pytest.mark.asyncio
async def test_reading_the_draft_id_survives_a_network_error():
    async with respx.mock(base_url=BASE, assert_all_called=False) as router:
        router.get("/ru/application/lots/1").mock(side_effect=httpx.ConnectError("x"))
        async with GoszakupClient(ci_session="s", base_url=BASE) as gz:
            assert await gz._read_application_id("1") == (False, None)


# --------------------------------------------------- what the report may carry
@pytest.mark.parametrize(("response", "shape"), [
    (httpx.Response(200, json={"status": "ok"}), "status-ok"),
    (httpx.Response(200, json={"status": 1}), "status-1"),
    (httpx.Response(200, json={"status": 0}), "status-0"),
    (httpx.Response(200, json={"status": "error", "message": "Подпишите Приложение 15"}),
     "status-error.message"),
    (httpx.Response(200, json={"status": "ok", "application_id": 70000001}), "status-ok.application_id"),
    (httpx.Response(200, json={"errors": True, "токен": "abc"}), "errors-true.key"),
    (httpx.Response(200, json=[1, 2, 3]), "list3"),
    (httpx.Response(200, html="<html>login</html>"), "html"),
    (httpx.Response(200, text=""), "empty"),
    (httpx.Response(302, headers={"location": "/ru/user/login"}), "http302"),
    (object(), "unknown"),
])
def test_reply_shape_keeps_codes_and_never_values(response, shape):
    assert reply_shape(response) == shape
    assert all(ch.isalnum() or ch in "_.-" for ch in shape) and len(shape) <= 40


@pytest.mark.parametrize(("page", "context"), [
    ('<input type="hidden" name="lot" value="85000001">', "attr.value"),
    ('<tr data-app-lot-id="85000001">', "attr.data-app-lot-id"),
    ('<a href="/ru/application/show_doc/1/2/3334/85000001/1">', "url.show_doc"),
    ("<script>removeLot(85000001)</script>", "call.removeLot"),
    ("<td> 85000001 </td>", "text"),
    ("<td>185000001</td>", "none"),                       # part of a longer number
])
def test_id_context_names_where_an_id_sits(page, context):
    assert flow._id_context(page, 85000001) == context
