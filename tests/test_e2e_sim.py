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


STATUS_ONLY = {**FAST, "lag": 0.0, "want_log": True, "create_status_only": True,
               "create_post": 2.625, "lots_discovery_get": 0.704, "public_delay": 2.438}


def _create_stage(result):
    ann, app = sim_portal.ANN, sim_portal.APP
    log = result["log"]
    create = next(r for r in log if r["path"] == f"/ru/application/ajax_create_application/{ann}")
    reads = [r for r in log if (r["m"], r["path"]) == ("GET", f"/ru/application/lots/{ann}")]
    add = next(r for r in log if r["path"] == f"/ru/application/ajax_add_lots/{ann}/{app}")
    nxt = next(r for r in log if r["path"] == f"/ru/application/ajax_lots_next/{ann}/{app}")
    return create, reads, add, nxt


def test_status_only_create_discovers_the_draft_before_selecting_lots():
    """A portal that shows the draft only once the create POST has answered:
    every read sent meanwhile is redirected, the read sent with the answer names
    the draft — the same moment as before the reads were overlapped."""
    result = _run(STATUS_ONLY)
    _assert_submitted(result)

    create, reads, add, nxt = _create_stage(result)
    assert create["t1"] - create["t0"] == pytest.approx(2.625, abs=0.001)
    during = [r for r in reads if r["t0"] < create["t1"]]
    assert during and all(r.get("status") in (302, None) for r in during)   # None: dropped unanswered
    named = [r for r in reads if r.get("status") == 200]
    assert len(named) == 1 and named[0]["t0"] == pytest.approx(create["t1"], abs=0.001)
    assert named[0]["t1"] - named[0]["t0"] == pytest.approx(0.704, abs=0.001)
    assert add["t0"] == pytest.approx(named[0]["t1"], abs=0.001)
    assert add["t1"] <= nxt["t0"]
    public = next(req for req in result["log"] if "/ajax_public_application/" in req["path"])
    assert public["t1"] - public["t0"] == pytest.approx(2.438, abs=0.001)
    assert result["from_open_s"] - result["to_public_request_s"] == pytest.approx(2.44, abs=0.01)


def test_draft_id_is_read_while_the_create_post_is_still_running():
    """A portal that stores the draft before it answers: a read sent meanwhile
    names it, and add_lots goes out with the create answer — no read after it."""
    strict = _run(STATUS_ONLY)
    result = _run({**STATUS_ONLY, "read_at": 0.6, "commit_at": 0.85})
    _assert_submitted(result)

    create, reads, add, _nxt = _create_stage(result)
    named = [r for r in reads if r.get("status") == 200]
    assert named and named[0]["t0"] < create["t1"]            # sent while create ran
    assert add["t0"] == pytest.approx(max(create["t1"], named[0]["t1"]), abs=0.001)
    assert add["t0"] >= create["t1"]                           # never before the answer
    assert not [r for r in reads if r["t0"] >= create["t1"]]   # and no read after it
    assert result["from_open_s"] <= strict["from_open_s"] - 0.5


def test_a_draft_seen_before_create_is_never_taken_for_the_new_one():
    """An older draft of the tender is on the page before create commits. Its id
    looks exactly like a new one, so early reads are not trusted at all."""
    result = _run({**STATUS_ONLY, "read_at": 0.6, "commit_at": 0.85, "stale_app": "73000001"})
    _assert_submitted(result)                 # a request for 73000001 would be a violation
    create, reads, add, _nxt = _create_stage(result)
    after = [r for r in reads if r["t0"] >= create["t1"] and r.get("status") == 200]
    assert len(after) == 1 and add["t0"] == pytest.approx(after[0]["t1"], abs=0.001)


def test_overlapping_reads_can_be_switched_off():
    result = _run({**STATUS_ONLY, "defaults": {"hedged_reads": False}})
    _assert_submitted(result)
    create, reads, add, _nxt = _create_stage(result)
    assert len(reads) == 1 and reads[0]["t0"] >= create["t1"]
    assert not [r for r in result["log"] if "/priceoffers/" in r["path"] and r.get("status") != 200]


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
    create = next(req for req in requests if "/ajax_create_application/" in req["path"])
    lots = next(req for req in requests if "/ajax_lots_next/" in req["path"])
    attach = next(req for req in requests if "/beneficiary/ajax_save_info" in req["path"])
    beneficiary_checks = [req for req in requests if req["path"] == "/files/check_status/"]
    assert mapping["t1"] <= spec["t0"] < docs["t1"]
    assert len(beneficiary_checks) == 1
    assert beneficiary_checks[0]["t0"] < create["t1"]
    assert lots["t1"] <= attach["t0"]


def test_beneficiary_file_is_rechecked_if_creation_rotates_the_session():
    result = _run({**FAST, "rotate_session_on_create": True, "want_log": True})
    _assert_submitted(result)

    requests = result["log"]
    create = next(req for req in requests if "/ajax_create_application/" in req["path"])
    lots = next(req for req in requests if "/ajax_lots_next/" in req["path"])
    attach = next(req for req in requests if "/beneficiary/ajax_save_info" in req["path"])
    precheck, recheck = [req for req in requests if req["path"] == "/files/check_status/"]
    assert precheck["t0"] < create["t1"] <= lots["t1"] <= recheck["t0"]
    assert recheck["t1"] <= attach["t0"]


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
# The newer defaults (overlapping reads, pre-signed тех.спец) are off here, so
# each test below still measures the one switch it names.
LIVE_OFF = {"lot_barrier": "last", "early_app_lots": False,
            "hedged_reads": False, "presign_tech_spec": False}
ALL_ON = {"lot_barrier": "last", "early_app_lots": True, "lot_save_parallel": True,
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


# ── Pre-signed тех.спец and reads that overlap a stage POST ──────────────────
# (docs/PERFORMANCE_AUDIT.md, «Трасса #17983518»)
def _ts_saves(result):
    return [r for r in result["log"] if r["m"] == "POST" and "/3334/85000001/" in r["path"]]


def _copies(result):
    """Downloads of the application's own copies of the tender files."""
    return [r for r in result["log"] if "/files/download_file/7" in r["path"]]


def test_presigned_tech_spec_is_committed_while_its_copy_is_still_downloading():
    off = _run({**FAST, "lag": 0.0, "want_log": True, "defaults": {"presign_tech_spec": False}})
    on = _run({**FAST, "lag": 0.0, "want_log": True})
    _assert_submitted(off)
    _assert_submitted(on)

    # before: a page is saved only after its files have been downloaded
    assert min(s["t0"] for s in _ts_saves(off)) >= min(c["t1"] for c in _copies(off))
    # now: the saves go out as soon as the page names the files ...
    saves, copies = _ts_saves(on), _copies(on)
    assert len(saves) == 2 and len(copies) == 3
    assert max(s["t0"] for s in saves) < max(c["t1"] for c in copies)
    # ... and docs_next still waits until every copy has been compared
    docs_next = _paths(on, "/ajax_docs_next/")[0]
    assert max(c["t1"] for c in copies) <= docs_next["t0"]
    assert set(on["ts_attempts"].values()) == {1} and on["docs_next_posts"] == 1
    # (page 1 was already being committed while page 2 downloaded, so the gain
    # is the rest of the largest download, not all of it)
    assert on["from_open_s"] <= off["from_open_s"] - 0.5


@pytest.mark.parametrize("portal_verifies", [True, False])
def test_copy_that_differs_from_the_tender_file_is_resaved_before_docs_next(portal_verifies):
    """The early signature belongs to the tender's file. If the application's
    copy is not that file, the page is signed again from the real bytes and
    saved again — whether or not the portal would have noticed by itself."""
    result = _run({**FAST, "lag": 0.0, "ts_copy_differs": [2], "want_log": True,
                   "verify_signatures": portal_verifies})
    _assert_submitted(result)                   # publishing a wrong signature is a violation
    assert result["ts_attempts"] == {"85000001/1": 1, "85000001/2": 2}
    assert result["docs_next_posts"] == 1       # corrected first, not found out by a rejection
    resave = _ts_saves(result)[-1]
    assert resave["path"].endswith("/2") and resave["t1"] <= _paths(result, "/ajax_docs_next/")[0]["t0"]


@pytest.mark.parametrize("variant", [
    {"listing": "session"},            # the public file list is unreachable
    {"listing": "public"},
    {"listing": "none"},               # no list at all: the usual download -> sign -> save
    {"ts_names": False},               # pages do not name their files: nothing to match
    {"open_at": 0.0},                  # already open: pre-signing races the bid itself
    {"open_at": 0.0, "ts_copy_differs": [1, 2]},
    {"lots": 2, "ts_pages": 3},
    {"mode": "serial"},
    {"defaults": {"lot_save_parallel": True}, "ts_overlap": "accept"},
    {"defaults": {"lot_save_parallel": True}, "ts_overlap": "accept", "ts_copy_differs": [2]},
    {"defaults": {"lot_barrier": "none"}, "ts_overlap": "accept"},
    {"drop_ts_first_save": [1]},       # a pre-signed page's save is lost once
])
def test_presign_variants_submit_without_protocol_violations(variant):
    result = _run({**FAST, "lag": 1.0, **variant})
    _assert_submitted(result)


def test_without_file_names_pages_fall_back_to_download_then_save():
    result = _run({**FAST, "lag": 0.0, "ts_names": False, "want_log": True})
    _assert_submitted(result)
    assert min(s["t0"] for s in _ts_saves(result)) >= min(c["t1"] for c in _copies(result))


SOFT = {"read_at": 0.6, "commit_at": 0.9}      # the portal looks late and stores early


def test_stage_pages_are_read_while_the_stage_post_is_still_in_flight():
    base = {**LIVE, "ts_overlap": "accept", "want_log": True, "defaults": {"lot_barrier": "last"}}
    strict = _run(base)
    soft = _run({**base, **SOFT})
    _assert_submitted(strict)
    _assert_submitted(soft)

    def first_served(result, needle):
        return next(r for r in result["log"] if r["m"] == "GET" and r.get("status") == 200
                    and r["path"].endswith(needle))

    lots_next = _paths(soft, "/ajax_lots_next/")[0]
    docs_next = _paths(soft, "/ajax_docs_next/")[0]
    app = sim_portal.APP
    assert first_served(soft, f"/show_doc/{sim_portal.ANN}/{app}/3334")["t0"] < lots_next["t1"]
    assert first_served(soft, f"/priceoffers/{sim_portal.ANN}/{app}")["t0"] < docs_next["t1"]
    # reads only: every write keeps its place
    writes = [r["t0"] for r in soft["log"] if r["m"] == "POST"
              and ("/show_doc/" in r["path"] or "ajax_save_info" in r["path"])]
    assert min(writes) >= lots_next["t1"]
    assert _paths(soft, "/ajax_get_encr_info/")[0]["t0"] >= docs_next["t1"]
    assert soft["encr_info_calls"] == 1
    assert soft["from_detection_s"] <= strict["from_detection_s"] - 1.2


def test_overlapping_reads_cost_no_time_on_a_portal_that_never_serves_them_early():
    base = {**LIVE, "ts_overlap": "accept", "create_status_only": True}
    off = _run({**base, "defaults": {"lot_barrier": "last", "hedged_reads": False}})
    on = _run({**base, "defaults": {"lot_barrier": "last"}})
    _assert_submitted(off)
    _assert_submitted(on)
    assert on["from_detection_s"] <= off["from_detection_s"] + 0.01
    assert on["requests_after_open"] - off["requests_after_open"] <= 16     # the price: extra reads


# Every route's latency as measured in the live report of bid #17983518.
TRACE_17983518 = {
    "G": 1.0, "P": 1.0, "create_post": 2.72, "lots_discovery_get": 1.45, "create_status_only": True,
    "public_delay": 1.70, "docs_get": 1.02, "dl_base": 0.85, "dl_rate": 50e6,
    "lag": 0.0, "tsa": 0.5, "popup": 0.16,
    "ts_overlap": "accept", "signs_before_encrypt": "accept",
    "lat": {"add_lots": 1.03, "lots_next": 1.67, "lot_parent": 1.00, "doc_get": 0.95,
            "gen_1253": 1.67, "save_1253": 0.86, "gen_3335": 1.20, "save_3335": 1.02,
            "gen_3715": 1.66, "save_3715": 1.59, "gen_3359": 1.38, "save_3359": 1.16,
            "deposit_add": 1.61, "deposit_save": 1.05, "ben_save": 0.86, "check_status": 0.87,
            "ts_page_1": 1.70, "ts_page_2": 1.25, "ts_page_3": 1.02,
            "ts_save_1": 1.27, "ts_save_2": 1.81, "docs_next": 1.12, "price_page": 0.89,
            "encr_info": 0.86, "add_encrypt": 1.00, "save_signs": 0.91, "price_next": 0.89,
            "lots_page": 1.45, "listing": 1.0},
}
AS_DEPLOYED = {"lot_barrier": "last", "early_app_lots": True,
               "hedged_reads": False, "presign_tech_spec": False}


def test_replay_of_the_live_trace_17983518():
    """The model against the live number (22.14 s from the opening being seen
    to the publish answer), and what the same latencies give now."""
    then = _run({**TRACE_17983518, "defaults": AS_DEPLOYED})
    now_strict = _run({**TRACE_17983518, "defaults": {"lot_barrier": "last"}})
    now_soft = _run({**TRACE_17983518, **SOFT, "defaults": {"lot_barrier": "last"}})
    all_on = _run({**TRACE_17983518, **SOFT, "defaults": {
        "lot_barrier": "last", "lot_save_parallel": True, "gamma_parallel_saves": True}})
    for result in (then, now_strict, now_soft, all_on):
        _assert_submitted(result)
    assert then["from_detection_s"] == pytest.approx(22.14, abs=0.3)      # the model fits
    assert now_strict["from_detection_s"] <= 21.3
    assert now_soft["from_detection_s"] <= 19.4
    assert all_on["from_detection_s"] <= 17.6
