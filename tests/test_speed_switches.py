"""Unit checks for the hot-path speed switches (flow.speed_options & co)."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from goszakup import flow


def test_speed_options_defaults_and_parsing():
    assert flow.speed_options({}) == {
        "lot_barrier": "each", "early_app_lots": True,
        "lot_save_parallel": False, "gamma_parallel_saves": False,
        "hedged_reads": True, "presign_tech_spec": True,
    }
    opts = flow.speed_options({"lot_barrier": "last", "early_app_lots": "off",
                               "lot_save_parallel": "yes", "gamma_parallel_saves": 1,
                               "hedged_reads": "no", "presign_tech_spec": 0})
    assert opts == {"lot_barrier": "last", "early_app_lots": False,
                    "lot_save_parallel": True, "gamma_parallel_saves": True,
                    "hedged_reads": False, "presign_tech_spec": False}
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
        "hedged_reads": True, "presign_tech_spec": True}

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


def test_early_document_list_must_link_every_planned_document():
    """The list read while lots_next runs decides the id of Приложение 2, so a
    partial page (an AJAX fragment, another stage) must not stand in for it."""
    params = flow.AtagosBidParams(announce_id="555", tender_lot_ids=[1])

    def page(doc_ids):
        return "".join(f'<a href="/ru/application/show_doc/555/777/{d}">Перейти</a>' for d in doc_ids)

    full = page([1253, 3359, 3334, 3730, 3335, 3328])          # agreement under a third id
    assert flow._is_full_docs_page(full, params, "777")
    assert flow._is_full_docs_page(page([1253, 3359, 3334, 3715, 3335, 3328]), params, "777")
    assert not flow._is_full_docs_page(page([1253, 3359, 3334, 3335, 3328]), params, "777")  # no agreement row
    assert not flow._is_full_docs_page(page([1253, 3359, 3334, 3730, 3335]), params, "777")  # no deposit row
    assert not flow._is_full_docs_page(full.replace("/555/777/", "/555/778/"), params, "777")
    assert not flow._is_full_docs_page("<html>lots</html>", params, "777")
