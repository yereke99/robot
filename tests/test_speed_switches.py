"""Unit checks for the hot-path speed switches (flow.speed_options & co)."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from websockets.protocol import State

from goszakup import flow, tumar


def test_speed_options_defaults_and_parsing():
    assert flow.speed_options({}) == {
        "lot_barrier": "each", "early_app_lots": True,
        "lot_save_parallel": False, "gamma_parallel_saves": False,
        "hedged_reads": True, "early_create_id": False, "presign_tech_spec": True,
    }
    opts = flow.speed_options({"lot_barrier": "last", "early_app_lots": "off",
                               "lot_save_parallel": "yes", "gamma_parallel_saves": 1,
                               "hedged_reads": "no", "early_create_id": "yes", "presign_tech_spec": 0})
    assert opts == {"lot_barrier": "last", "early_app_lots": False,
                    "lot_save_parallel": True, "gamma_parallel_saves": True,
                    "hedged_reads": False, "early_create_id": True, "presign_tech_spec": False}
    assert flow.speed_options(None)["early_app_lots"] is True
    flow.AtagosBidParams(announce_id="1", tender_lot_ids=[1], **opts)   # accepted keys


@pytest.mark.parametrize("body,verdict", [
    ({"status": "ok"}, "ok"), ({"status": 1}, "ok"), ({"result": "1"}, "ok"),
    ({"success": True}, "ok"), ({"status": "error", "message": "x"}, "error"),
    ({"status": 0}, "error"), ({"error": "no price"}, "error"),
    ({"code": "0"}, "unknown"), ([1], "unknown"),
])
def test_reply_verdict_trusts_only_a_recognisable_success(body, verdict):
    assert flow._reply_verdict(httpx.Response(200, json=body)) == verdict


def test_reply_verdict_non_json_and_redirect():
    assert flow._reply_verdict(httpx.Response(200, text="<html>")) == "unknown"
    assert flow._reply_verdict(httpx.Response(302, headers={"location": "/x"})) == "error"
    assert flow._reply_verdict(None) == "unknown"


class _Http:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def get(self, path, follow_redirects=True):
        self.calls.append((path, follow_redirects))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class _GZ:
    def __init__(self, response):
        self._http = _Http(response)


def _params(lots=(41000001,)):
    return flow.AtagosBidParams(announce_id="555", tender_lot_ids=list(lots))


def _probe(response, lots=(41000001,)):
    return asyncio.run(flow._probe_app_lots(_GZ(response), _params(lots), "777"))


def test_probe_reads_once_without_following_redirects():
    gz = _GZ(httpx.Response(302, headers={"location": "/ru/application/lots/555/777"}))
    assert asyncio.run(flow._probe_app_lots(gz, _params(), "777")) is None
    assert gz._http.calls == [("/ru/application/show_doc/555/777/3334", False)]


def test_probe_accepts_only_links_of_this_application():
    page = ('<table><tr><td>41000001</td><td><a href="/ru/application/show_doc/555/777/3334/'
            '85000001/1">x</a></td></tr></table>')
    assert _probe(httpx.Response(200, html=page)) == {41000001: 85000001}
    other_app = page.replace("/555/777/", "/555/778/")
    assert _probe(httpx.Response(200, html=other_app)) is None
    assert _probe(httpx.Response(200, html=page), lots=(41000001, 41000002)) is None
    assert _probe(httpx.ConnectError("boom")) is None


def test_resolve_prefers_the_early_mapping_and_races_otherwise(monkeypatch):
    calls = []

    async def discover(gz, params, app_id, docs_page_task=None):
        calls.append(app_id)
        await asyncio.sleep(0.05)
        return {1: 2}

    monkeypatch.setattr(flow, "_discover_app_lots", discover)

    async def main():
        hit = asyncio.get_running_loop().create_future()
        hit.set_result({1: 9})
        assert await flow._resolve_app_lots(None, None, "a", early=hit) == {1: 9}
        assert calls == []                                   # no second read

        async def slow_miss():
            await asyncio.sleep(0.2)
            return None
        miss = asyncio.ensure_future(slow_miss())
        assert await flow._resolve_app_lots(None, None, "b", early=miss) == {1: 2}
        assert calls == ["b"]                                # did not wait for the miss
        miss.cancel()

    asyncio.run(main())


# ── /config: the same switches at runtime ────────────────────────────────────
def test_config_command_seeds_and_overrides_the_speed_switches():
    from types import SimpleNamespace

    from goszakup.bot.handlers import settings

    raw = {"defaults": {"lot_barrier": "last", "poll_interval_ms": 200, "dry_run": False}}
    seeded = settings.seed_settings(raw)
    assert {k: seeded[k] for k in settings.SPEED_KEYS} == {
        "lot_save_parallel": False, "gamma_parallel_saves": False, "lot_barrier": "last",
        "hedged_reads": True, "early_create_id": False, "presign_tech_spec": True}

    ctx = SimpleNamespace(config=SimpleNamespace(raw=raw), bid_settings=dict(seeded))
    assert flow.speed_options(settings.speed_defaults(ctx))["lot_save_parallel"] is False
    ctx.bid_settings["lot_save_parallel"] = settings._coerce("bool", "on")
    ctx.bid_settings["lot_barrier"] = settings._coerce("barrier", " NONE ")
    opts = flow.speed_options(settings.speed_defaults(ctx))
    assert (opts["lot_save_parallel"], opts["lot_barrier"], opts["hedged_reads"]) == (True, "none", True)
    assert settings.speed_defaults(ctx)["poll_interval_ms"] == 200        # the rest is untouched
    with pytest.raises(ValueError):
        settings._coerce("barrier", "sometimes")
    assert set(settings.SPEED_KEYS) <= set(settings.SETTABLE)

    # a context that never ran /config (or a CLI run) just uses config.yaml
    bare = SimpleNamespace(config=SimpleNamespace(raw=raw), bid_settings={})
    assert flow.speed_options(settings.speed_defaults(bare))["lot_barrier"] == "last"


async def test_price_watch_probes_only_the_first_document_check(monkeypatch):
    monkeypatch.setattr(flow, "_PRICE_READ_DELAY_S", 0.0)
    watch = flow._PricePageWatch(None, "555", "777")
    probe = AsyncMock(return_value=None)
    monkeypatch.setattr(watch, "_probe", probe)

    watch.poll_started()
    assert await watch.early() is None
    assert watch.early() is None
    await watch.poll_rejected()
    watch.poll_started()
    assert watch.early() is None
    probe.assert_awaited_once()


async def test_price_watch_discards_page_loaded_during_session_change():
    async with flow.GoszakupClient(base_url="https://portal.test", ci_session="session") as client:
        async def peek(path):
            client.adopt_ci_session("new-session")
            return httpx.Response(
                200, request=httpx.Request("GET", f"https://portal.test{path}"),
                html='<input id="public_key" value="KEY"><input app_lot_id="300" buy_lot_point_id="400">',
            )

        client.peek = peek
        watch = flow._PricePageWatch(client, "555", "777")
        assert await watch._probe(1) is None


async def test_ready_price_page_uses_one_encryption_request(monkeypatch):
    provider = tumar.TumarCSP(api_key="unused", autofill=lambda _: False)
    provider._ws = SimpleNamespace(state=State.OPEN)
    provider.version = "2.0.0.0"
    monkeypatch.setattr(provider, "encrypt_offer_price", AsyncMock(
        return_value=tumar.EncryptResult(encrypted_data="ENC", session_key="KEY", info="SN", sign="SIGN")))
    signer = SimpleNamespace(sign_xml_batch=AsyncMock(return_value={400: "CMS"}))
    params = flow.AtagosBidParams(announce_id="555", tender_lot_ids=[1], tumar=provider)
    page = '<input id="public_key" value="KEY"><input app_lot_id="300" buy_lot_point_id="400">'
    prefetch = AsyncMock()
    monkeypatch.setattr(flow, "_prefetch_encr_info", prefetch)

    async with flow.GoszakupClient(base_url="https://portal.test", ci_session="session") as client:
        client._get_page = AsyncMock()
        client.get_encr_info = AsyncMock(return_value={"info": "5_400;100;80;", "salt": "fresh", "sign": "SIGN"})
        for method in ("add_encrypt", "save_gamma_signs", "priceoffers_next_gamma"):
            monkeypatch.setattr(client, method, AsyncMock(return_value=httpx.Response(200, json={"status": "ok"})))
        ready = asyncio.get_running_loop().create_future()
        ready.set_result(flow.FetchedPricePage(page, flow._price_session_cookies(client)))
        watch = SimpleNamespace(early=lambda: ready, aclose=AsyncMock())
        async with AsyncExitStack() as stack:
            assert await flow._gamma_price(
                client, signer, params, "777", stack, cached_lp_id=400, price_watch=watch) == ([400], {400: 80})

    watch.aclose.assert_awaited_once()
    client._get_page.assert_not_awaited()
    prefetch.assert_not_awaited()
    client.get_encr_info.assert_awaited_once_with("555", "777", lp_id=400, version="2.0.0.0")
