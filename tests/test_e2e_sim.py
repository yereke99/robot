"""End-to-end regression: the whole submit_bid chain against a simulated portal.

tools/sim_portal.py runs the REAL GoszakupClient and flow.submit_bid over a
stateful fake goszakup (respx) on a virtual clock: every request "takes" its
latency but nothing really sleeps. The fake portal enforces the ordering rules
the bot relies on (lots before docs, тех.спец pages saved one at a time with the
barrier between, add_encrypt -> save_gamma_signs -> priceoffers_next -> public,
the freshest salt, no duplicate create) and reports any violation.

These tests pin both the correctness of the orchestration and its time budget.
"""

from __future__ import annotations

import asyncio

import pytest

from tools import sim_portal

FAST = {"G": 0.7, "P": 0.35}     # page GET / ajax POST latency, seconds


def _run(cfg: dict) -> dict:
    loop = sim_portal.VirtualLoop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(sim_portal.run_scenario({"open_at": 30.0, **cfg}))
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _assert_submitted(result: dict) -> None:
    assert result["error"] is None, result["error"]
    assert result["step"] == "public_ok"
    assert result["violations"] == []


def test_clean_bid_runs_the_whole_chain_in_order():
    result = _run({**FAST, "lag": 0.0, "want_log": True})
    _assert_submitted(result)
    assert set(result["save_attempts"].values()) == {1}     # every doc saved exactly once
    assert result["encr_info_calls"] == 1
    assert result["from_open_s"] < 11.0
    agreement_path = f"/ru/application/show_doc/{sim_portal.ANN}/{sim_portal.APP}/3715"
    agreement_requests = [req for req in result["log"] if req["path"] == agreement_path]
    assert [(req["m"], req["status"]) for req in agreement_requests] == [("POST", 200), ("POST", 302)]


def test_status_only_create_discovers_the_draft_before_selecting_lots():
    result = _run({
        **FAST, "lag": 0.0, "want_log": True, "create_status_only": True,
        "create_post": 2.625, "lots_discovery_get": 0.704, "public_delay": 2.438,
    })
    _assert_submitted(result)

    ann, app = sim_portal.ANN, sim_portal.APP
    stage = [
        ("POST", f"/ru/application/ajax_create_application/{ann}"),
        ("GET", f"/ru/application/lots/{ann}"),
        ("POST", f"/ru/application/ajax_add_lots/{ann}/{app}"),
        ("POST", f"/ru/application/ajax_lots_next/{ann}/{app}"),
    ]
    requests = [req for req in result["log"] if (req["m"], req["path"]) in stage]
    assert [(req["m"], req["path"]) for req in requests] == stage
    assert all(previous["t1"] <= current["t0"] for previous, current in zip(requests, requests[1:]))
    create, discover = requests[:2]
    assert create["t1"] - create["t0"] == pytest.approx(2.625, abs=0.001)
    assert discover["t1"] - discover["t0"] == pytest.approx(0.704, abs=0.001)
    public = next(req for req in result["log"] if "/ajax_public_application/" in req["path"])
    assert public["t1"] - public["t0"] == pytest.approx(2.438, abs=0.001)
    assert result["from_open_s"] - result["to_public_request_s"] == pytest.approx(2.44, abs=0.01)


def test_slow_tumar_start_does_not_delay_creation_and_uses_actual_version():
    result = _run({**FAST, "open_at": 0.0, "lag": 0.0, "tumar_warmup": 6.0,
                   "tumar_version": "1.2.3.4", "lp_cache": True})
    _assert_submitted(result)
    assert result["t_create_post"] - result["t_open"] < 1.0
    assert result["encr_info_calls"] == 1


def test_lot_documents_start_while_docs_list_is_loading():
    result = _run({**FAST, "docs_get": 3.0, "want_log": True})
    _assert_submitted(result)
    requests = result["log"]
    docs = next(req for req in requests if "/application/docs/" in req["path"])
    mapping = next(req for req in requests if req["path"].endswith("/3334"))
    spec = next(req for req in requests if "/3334/85000001/1" in req["path"])
    beneficiary_checks = [req for req in requests if req["path"] == "/files/check_status/"]
    assert mapping["t1"] <= spec["t0"] < docs["t1"]
    assert len(beneficiary_checks) == 1
    assert beneficiary_checks[0]["t0"] < mapping["t1"]


def test_lost_agreement_response_continues_using_the_generated_file():
    doc = 3730
    result = _run({
        **FAST, "lag": 0.0, "want_log": True,
        "agreement_doc": doc, "agreement_disconnect": "after_commit",
    })
    _assert_submitted(result)

    ann, app = sim_portal.ANN, sim_portal.APP
    requests = result["log"]
    doc_path = f"/ru/application/show_doc/{ann}/{app}/{doc}"
    doc_requests = [req for req in requests if req["path"] == doc_path]
    assert [(req["m"], req["status"]) for req in doc_requests] == [
        ("POST", "RemoteProtocolError"), ("GET", 200), ("POST", 302),
    ]
    generate, confirm, sign = doc_requests
    fid = result["generated_files"][doc]
    download = next(req for req in requests if req["path"] == f"/files/download_file/{fid}/{fid + 7}/")
    validate = next(req for req in requests if "/ajax_docs_next/" in req["path"])
    publish = next(req for req in requests if "/ajax_public_application/" in req["path"])
    ordered = (generate, confirm, download, sign, validate, publish)
    assert all(previous["t1"] <= current["t0"] for previous, current in zip(ordered, ordered[1:]))
    assert sum("/ajax_create_application/" in req["path"] for req in requests) == 1
    assert result["save_attempts"][doc] == 1


@pytest.mark.parametrize(("disconnect", "status"), [("before_commit", 200), ("after_commit_redirect", 302)])
def test_unconfirmed_agreement_generation_stops_without_repeating_post(disconnect, status):
    doc = 3730
    result = _run({
        **FAST, "lag": 0.0, "want_log": True,
        "agreement_doc": doc, "agreement_disconnect": disconnect,
    })
    assert result["error"].startswith("DocumentGenerationUnconfirmed:")
    assert result["violations"] == []

    ann, app = sim_portal.ANN, sim_portal.APP
    requests = result["log"]
    doc_path = f"/ru/application/show_doc/{ann}/{app}/{doc}"
    generate, *checks = [req for req in requests if req["path"] == doc_path]
    assert (generate["m"], generate["status"]) == ("POST", "RemoteProtocolError")
    assert checks and all((req["m"], req["status"]) == ("GET", status) for req in checks)
    assert sum("/ajax_create_application/" in req["path"] for req in requests) == 1
    assert doc not in result["save_attempts"]
    assert not any("/ajax_docs_next/" in req["path"] or "/priceoffers/" in req["path"] for req in requests)
    assert result["encr_info_calls"] == 0
    assert result["to_public_request_s"] is None


@pytest.mark.parametrize("disconnect", ["before_commit", "after_commit"])
def test_lost_deposit_selection_response_retries_before_saving_once(disconnect):
    result = _run({
        **FAST, "lag": 0.0, "want_log": True, "deposit_add_disconnect": disconnect,
    })
    _assert_submitted(result)

    requests = result["log"]
    deposit_path = f"/ru/application/show_doc/{sim_portal.ANN}/{sim_portal.APP}/3328/85000001/1"
    select, retry, save = [req for req in requests if req["path"].startswith(deposit_path)]
    assert (select["m"], select["path"], select["status"]) == ("POST", deposit_path, "RemoteProtocolError")
    assert (retry["m"], retry["path"], retry["status"]) == ("POST", deposit_path, 200)
    assert (save["m"], save["path"], save["status"]) == ("POST", f"{deposit_path}/?show=3", 302)
    validate = next(req for req in requests if "/ajax_docs_next/" in req["path"])
    publish = next(req for req in requests if "/ajax_public_application/" in req["path"])
    ordered = (select, retry, save, validate, publish)
    assert all(previous["t1"] <= current["t0"] for previous, current in zip(ordered, ordered[1:]))
    assert sum("/ajax_create_application/" in req["path"] for req in requests) == 1


def test_deposit_selection_disconnects_twice_and_stops_before_save():
    result = _run({
        **FAST, "lag": 0.0, "want_log": True, "deposit_add_disconnect": "always_before_commit",
    })
    assert result["error"]
    assert result["violations"] == []

    requests = result["log"]
    deposit_path = f"/ru/application/show_doc/{sim_portal.ANN}/{sim_portal.APP}/3328/85000001/1"
    selects = [req for req in requests if req["path"].startswith(deposit_path)]
    assert [(req["m"], req["path"], req["status"]) for req in selects] == [
        ("POST", deposit_path, "RemoteProtocolError"),
        ("POST", deposit_path, "RemoteProtocolError"),
    ]
    assert not any("/ajax_docs_next/" in req["path"] for req in requests)
    assert result["encr_info_calls"] == 0
    assert result["to_public_request_s"] is None


def test_dry_run_prices_the_application_without_publishing():
    result = _run({**FAST, "dry_run": True})
    assert result["error"] is None
    assert result["step"] == "dry_run_complete"
    assert result["violations"] == []
    assert result["encr_info_calls"] == 1
    assert result["t_public"] is None
    assert result["to_public_request_s"] is None


def test_slow_publication_response_does_not_affect_request_start():
    normal = _run({**FAST, "lag": 1.0})
    delayed = _run({**FAST, "lag": 1.0, "public_delay": 5.31})
    _assert_submitted(normal)
    _assert_submitted(delayed)
    assert delayed["to_public_request_s"] == normal["to_public_request_s"]
    assert delayed["from_open_s"] - delayed["to_public_request_s"] == pytest.approx(5.31)


def test_prepared_application_resumes_at_price_with_fresh_data():
    result = _run({**FAST, "two_phase": True, "lp_cache": True})
    _assert_submitted(result)
    assert result["encr_info_calls"] == 1
    assert set(result["save_attempts"].values()) == {1}


def test_validation_lag_is_waited_out_not_answered_with_a_resave():
    """THE 44-second regression. While the portal is still validating the
    тех.спец it answers "Подпишите … Приложение 15". The old code re-committed
    every document on the first such answer; the bid must simply wait."""
    result = _run({**FAST, "lag": 3.0})
    _assert_submitted(result)
    assert set(result["save_attempts"].values()) == {1}     # nothing re-committed
    assert result["from_open_s"] < 14.0


def test_long_lag_still_submits():
    result = _run({**FAST, "lag": 5.0})
    _assert_submitted(result)
    assert set(result["save_attempts"].values()) == {1}


def test_lost_signature_is_healed_by_resaving_only_that_document():
    result = _run({**FAST, "lag": 0.0, "drop_first_save": [3359]})
    _assert_submitted(result)
    attempts = result["save_attempts"]
    assert attempts[3359] == 2                               # the lost one, re-saved once
    assert all(n == 1 for doc, n in attempts.items() if doc != 3359)


@pytest.mark.parametrize("variant", [
    {"lots": 2},
    {"ts_pages": 3},
    {"mode": "serial"},
    {"open_at": 0.0},                  # tender already open when the bid starts
    {"no_password": True},             # bot running on a pasted cookie
    {"lp_cache": True},                # second bid on the same tender
    {"drop_ts_first_save": [2]},       # a тех.спец page save is lost once
    {"bankruptcy_post_has_link": True},
    {"session_lock": True},            # portal serialises one session's requests
])
def test_variants_submit_without_protocol_violations(variant):
    _assert_submitted(_run({**FAST, "lag": 1.0, **variant}))


def test_slow_portal_budget():
    """Latencies as observed on the live portal (~1.6s pages, ~0.7s ajax)."""
    result = _run({"G": 1.6, "P": 0.7, "lag": 2.0})
    _assert_submitted(result)
    assert result["from_open_s"] < 23.0


# ── Speed switches (docs/PERFORMANCE_AUDIT.md, «Трасса #17947316») ──────────
# Latencies of the live trace: create 2.75 s, ~1 s per POST, ~1.2 s per page.
LIVE = {"G": 1.2, "P": 1.0, "create_post": 2.75, "lots_discovery_get": 0.66,
        "public_delay": 2.73, "docs_get": 1.6, "dl_base": 1.0, "dl_rate": 50e6,
        "lag": 0.5, "tsa": 0.31, "popup": 0.30}
LIVE_OFF = {"lot_barrier": "last", "early_app_lots": False}
ALL_ON = {**LIVE_OFF, "early_app_lots": True, "lot_save_parallel": True,
          "gamma_parallel_saves": True}


def _paths(result, needle):
    return [req for req in result["log"] if needle in req["path"]]


def test_early_app_lots_reads_the_mapping_while_lots_next_runs():
    off = _run({**LIVE, "ts_overlap": "accept", "defaults": LIVE_OFF})
    on = _run({**LIVE, "ts_overlap": "accept", "early_show_doc": "serve", "want_log": True,
               "defaults": {**LIVE_OFF, "early_app_lots": True}})
    _assert_submitted(off)
    _assert_submitted(on)
    lots_next = _paths(on, "/ajax_lots_next/")[0]
    parent = [r for r in on["log"] if r["path"].endswith("/3334") and r["m"] == "GET"][0]
    assert parent["t0"] < lots_next["t1"]                     # read overlaps lots_next
    first_write = min(r["t0"] for r in on["log"] if r["m"] == "POST"
                      and ("/show_doc/" in r["path"] or "ajax_save_info" in r["path"]))
    assert first_write >= lots_next["t1"]                     # nothing written early
    assert on["from_open_s"] <= off["from_open_s"] - 0.8


def test_early_app_lots_falls_back_when_the_page_is_stage_gated():
    off = _run({**LIVE, "ts_overlap": "accept", "defaults": LIVE_OFF})
    on = _run({**LIVE, "ts_overlap": "accept", "early_show_doc": "redirect",
               "defaults": {**LIVE_OFF, "early_app_lots": True}})
    _assert_submitted(on)
    assert on["from_open_s"] <= off["from_open_s"] + 0.01      # costs one GET, no time


def test_parallel_tech_spec_saves_overlap_and_keep_one_barrier():
    off = _run({**LIVE, "ts_overlap": "accept", "defaults": LIVE_OFF})
    on = _run({**LIVE, "ts_overlap": "accept", "want_log": True,
               "defaults": {**LIVE_OFF, "lot_save_parallel": True}})
    _assert_submitted(on)
    saves = [r for r in on["log"] if r["m"] == "POST" and "/3334/85000001/" in r["path"]]
    assert len(saves) == 2 and saves[1]["t0"] < saves[0]["t1"]
    barrier = [r for r in on["log"] if r["m"] == "GET" and r["path"].endswith("/3334")
               and r["t0"] >= max(s["t1"] for s in saves)]
    assert len(barrier) == 1
    assert set(on["ts_attempts"].values()) == {1}
    assert on["from_open_s"] <= off["from_open_s"] - 1.0


def test_parallel_tech_spec_saves_are_healed_when_the_portal_drops_one():
    result = _run({**LIVE, "ts_overlap": "clobber",
                   "defaults": {**LIVE_OFF, "lot_save_parallel": True}})
    _assert_submitted(result)
    assert max(result["ts_attempts"].values()) == 2           # the lost page re-saved


def test_gamma_signs_go_with_add_encrypt_when_accepted():
    off = _run({**LIVE, "ts_overlap": "accept", "defaults": LIVE_OFF})
    on = _run({**LIVE, "ts_overlap": "accept", "signs_before_encrypt": "accept",
               "want_log": True, "defaults": {**LIVE_OFF, "gamma_parallel_saves": True}})
    _assert_submitted(on)
    add = _paths(on, "/ajax_add_encrypt/")[0]
    signs = _paths(on, "/ajax_save_gamma_signs/")
    nxt = _paths(on, "/ajax_priceoffers_next/")[0]
    assert len(signs) == 1 and signs[0]["t0"] < add["t1"]
    assert nxt["t0"] >= max(add["t1"], signs[0]["t1"])
    assert on["from_open_s"] <= off["from_open_s"] - 0.6


def test_gamma_signs_are_resent_after_add_encrypt_when_refused():
    off = _run({**LIVE, "ts_overlap": "accept", "defaults": LIVE_OFF})
    on = _run({**LIVE, "ts_overlap": "accept", "signs_before_encrypt": "reject",
               "want_log": True, "defaults": {**LIVE_OFF, "gamma_parallel_saves": True}})
    _assert_submitted(on)
    add = _paths(on, "/ajax_add_encrypt/")[0]
    signs = _paths(on, "/ajax_save_gamma_signs/")
    assert on["signs_posts"] == 2 and signs[1]["t0"] >= add["t1"]
    # Worst case: the old order plus the TSA signature the first try waited on.
    assert on["from_open_s"] <= off["from_open_s"] + LIVE["tsa"] + 0.01


def test_all_switches_on_live_latencies_and_tolerant_portal():
    result = _run({**LIVE, "ts_overlap": "accept", "early_show_doc": "serve",
                   "signs_before_encrypt": "accept", "defaults": ALL_ON})
    _assert_submitted(result)
    assert result["from_open_s"] < 21.0


@pytest.mark.parametrize("lots", [1, 2])
def test_all_switches_multi_lot_strict_portal_still_submits(lots):
    result = _run({**FAST, "lots": lots, "lag": 1.0, "ts_overlap": "accept",
                   "early_show_doc": "redirect", "signs_before_encrypt": "reject",
                   "defaults": ALL_ON})
    _assert_submitted(result)
