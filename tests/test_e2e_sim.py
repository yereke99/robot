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
    result = _run({**FAST, "lag": 0.0})
    _assert_submitted(result)
    assert set(result["save_attempts"].values()) == {1}     # every doc saved exactly once
    assert result["encr_info_calls"] == 1
    assert result["from_open_s"] < 11.0


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
