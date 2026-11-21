"""Document save strategies.

serial (A, default): commit one save at a time on the shared session with a GET
barrier between — the proven, race-free order. The _SaveOp abstraction lets the
same prepared commit run on the shared session OR an isolated per-doc one.

parallel_sessions (B, experimental): each doc runs its full flow on a cloned session
and saves in parallel. These tests fake the prepare so they exercise only the
save orchestration (ordering, barriers, cookie reuse, recovery, fallback).
"""

from __future__ import annotations

import types

import pytest

from goszakup import flow
from goszakup.flow import _SaveOp, _save_serial


def _op(label: str, doc_id: int):
    async def run(c):
        c.events.append(("save", label))
    return _SaveOp(label, doc_id, run)


class _RecordingGz:
    def __init__(self):
        self.events: list[tuple] = []

    async def get_show_doc(self, ann, app_id, doc_id, suffix=""):
        self.events.append(("barrier", doc_id))
        return "<html></html>"


# --- serial (A) ------------------------------------------------------------------

async def test_save_serial_orders_save_then_barrier():
    gz = _RecordingGz()
    ops = [_op("doc_1253", 1253), _op("lot_3334_5_1", 3334)]
    await _save_serial(gz, "ann", "app", ops)
    assert gz.events == [
        ("save", "doc_1253"), ("barrier", 1253),
        ("save", "lot_3334_5_1"), ("barrier", 3334),
    ]


async def test_save_serial_barrier_failure_is_nonfatal():
    class _G(_RecordingGz):
        async def get_show_doc(self, *a, **k):
            raise RuntimeError("barrier boom")

    gz = _G()
    ran = []

    async def run(c):
        ran.append("x")

    await _save_serial(gz, "ann", "app", [_SaveOp("d", 1, run)])
    assert ran == ["x"]          # the save happened; the barrier error didn't bubble


# --- parallel_sessions (B) -------------------------------------------------------

class _FakeSib:
    def __init__(self):
        self.saved: list[int] = []
        self.barriers: list[int] = []
        self.logged_in = False
        self.logins = 0
        self.closed = False
        self._http = types.SimpleNamespace(cookies={})

    def adopt_ci_session(self, ci_session):
        self._http.cookies["ci_session"] = ci_session

    async def login(self, *, account_password, kalkan_signer):
        self.logged_in = True
        self.logins += 1
        self.adopt_ci_session("CI")
        return "CI"

    async def get_show_doc(self, ann, app_id, doc_id, suffix=""):
        self.barriers.append(doc_id)
        return ""

    async def aclose(self):
        self.closed = True


class _FakeMainGz:
    def __init__(self):
        self.siblings: list[_FakeSib] = []
        self._http = types.SimpleNamespace(cookies={"ci_session": "MAIN_CI"})

    def adopt_ci_session(self, ci_session):
        self._http.cookies["ci_session"] = ci_session

    def get_cookie(self, name):
        return self._http.cookies.get(name)

    def clone_unauthenticated(self):
        s = _FakeSib()
        self.siblings.append(s)
        return s


def _fake_prepare(monkeypatch):
    async def fake_prepare(gz, ns, params, app_id, app_lot_map, beneficiary, base_url, spec,
                           **kwargs):
        async def run(c):
            c.saved.append(spec.doc_id)
        return [_SaveOp(f"doc_{spec.doc_id}", spec.doc_id, run)]
    monkeypatch.setattr(flow, "_prepare_doc", fake_prepare)


def _params(**extra):
    p = dict(
        doc_plan=[flow.DocSpec(1253, "generate_sign"), flow.DocSpec(3334, "lot_sign")],
        auto_login_account_password="pw", signer=object(), announce_id="ann",
    )
    p.update(extra)
    return types.SimpleNamespace(**p)


async def test_parallel_sessions_spawns_when_no_pool(monkeypatch):
    """Empty pool -> a session is spawned per doc on the spot, used, and closed."""
    _fake_prepare(monkeypatch)
    gz = _FakeMainGz()
    ops = await flow._save_parallel_sessions(gz, _params(), "app", {7: 70}, None, "base", [])
    assert len(gz.siblings) == 2                          # one cloned session per doc
    assert all(s.logins == 0 and s.closed for s in gz.siblings)
    assert all(s._http.cookies["ci_session"] == "MAIN_CI" for s in gz.siblings)
    assert gz.siblings[0].barriers == []                  # single-save doc needs no GET
    assert gz.siblings[1].barriers == [3334]              # lot-sign commit barrier stays
    saved = sorted(d for s in gz.siblings for d in s.saved)
    assert saved == [1253, 3334]                          # each saved on its own session
    assert sorted(o.doc_id for o in ops) == [1253, 3334]


async def test_parallel_sessions_uses_prewarmed_pool(monkeypatch):
    """A pre-warmed pool is consumed as-is — no new logins, pool sessions NOT
    closed here (the AsyncExitStack owns them)."""
    _fake_prepare(monkeypatch)
    gz = _FakeMainGz()                                    # clone_unauthenticated would append here
    pool = [(_FakeSib(), object()), (_FakeSib(), object())]
    for sib, _ in pool:
        sib.adopt_ci_session("MAIN_CI")                   # prewarmed cookie clone
    ops = await flow._save_parallel_sessions(gz, _params(), "app", {7: 70}, None, "base", pool)
    assert gz.siblings == []                              # nothing spawned — pool reused
    assert all(sib.logins == 0 for sib, _ in pool)
    assert all(not sib.closed for sib, _ in pool)         # stack closes them, not us
    saved = sorted(d for sib, _ in pool for d in sib.saved)
    assert saved == [1253, 3334]
    assert sorted(o.doc_id for o in ops) == [1253, 3334]


@pytest.mark.parametrize("early", [False, True])
async def test_parallel_sessions_relogins_stale_pool_session(monkeypatch, early):
    """A pool session that expired during a long pre-T0 wait raises SessionExpired
    on first use -> we re-login it in place and retry, not crash."""
    state = {"raised": early}

    async def fake_prepare(gz, ns, params, app_id, app_lot_map, beneficiary, base_url, spec,
                           **kwargs):
        if not state["raised"]:
            state["raised"] = True
            raise flow.SessionExpired("ci_session died mid-wait")
        async def run(c):
            c.saved.append(spec.doc_id)
        return [_SaveOp(f"doc_{spec.doc_id}", spec.doc_id, run)]
    monkeypatch.setattr(flow, "_prepare_doc", fake_prepare)

    sib = _FakeSib()
    other_sib = _FakeSib()
    for client in (sib, other_sib):
        client.adopt_ci_session("STALE_CI")
    pool = [(sib, object()), (other_sib, object())]
    params = _params(doc_plan=[flow.DocSpec(1253, "generate_sign")])
    gz = _FakeMainGz()

    async def expired():
        raise flow.SessionExpired("ci_session died mid-wait")

    early_futures = {0: flow.asyncio.create_task(expired())} if early else None
    ops = await flow._save_parallel_sessions(
        gz, params, "app", {7: 70}, None, "base", pool, early_futures=early_futures)
    assert sib.logins == 1                                # re-logged in once after the expiry
    assert sib.saved == [1253]                            # retry succeeded
    assert other_sib.logins == 0
    assert all(client._http.cookies["ci_session"] == "CI"
               for client in (gz, sib, other_sib))         # propagate recovery to every client
    assert [o.doc_id for o in ops] == [1253]


async def test_prewarm_doc_pool_clones_cookie_without_new_logins():
    gz = _FakeMainGz()

    class _Stack:
        def __init__(self): self.entered = []
        async def enter_async_context(self, c): self.entered.append(c); return c

    stack = _Stack()
    params = _params(doc_plan=[flow.DocSpec(1253, "generate_sign"),
                               flow.DocSpec(3359, "beneficiary"),
                               flow.DocSpec(3334, "lot_sign")])
    pool = await flow._prewarm_doc_pool(gz, params, stack)
    assert len(pool) == 3                                  # one session per doc
    assert len(gz.siblings) == 3                          # no unused extra clones
    assert all(sib.logins == 0 for sib, _ in pool)
    assert all(sib._http.cookies["ci_session"] == "MAIN_CI" for sib, _ in pool)
    assert len(stack.entered) == 3                         # all registered for cleanup


async def test_process_documents_falls_back_to_serial_without_creds(monkeypatch):
    _fake_prepare(monkeypatch)

    class _G:
        def __init__(self):
            self.saved: list[int] = []

        def clone_unauthenticated(self):
            raise AssertionError("must not spawn a session without creds")

        async def get_show_doc(self, *a, **k):
            return ""

    # prepare appends to gz.saved via the op's run(c=gz)
    async def fake_prepare(gz, ns, params, app_id, app_lot_map, beneficiary, base_url, spec,
                           **kwargs):
        async def run(c):
            c.saved.append(spec.doc_id)
        return [_SaveOp(f"doc_{spec.doc_id}", spec.doc_id, run)]
    monkeypatch.setattr(flow, "_prepare_doc", fake_prepare)

    gz = _G()
    params = types.SimpleNamespace(
        doc_plan=[flow.DocSpec(1253, "generate_sign")],
        auto_login_account_password=None,               # no creds -> can't do B
        signer=None, parallel_docs=True,
        doc_save_mode="parallel_sessions", announce_id="ann",
    )
    ops = await flow._process_documents(gz, None, params, "app", {7: 70}, None, "base")
    assert gz.saved == [1253]                            # ran serially on the main gz
    assert ops and ops[0].doc_id == 1253


# --- single-flight relogin ---------------------------------------------------------

async def test_parallel_sessions_relogin_is_single_flight(monkeypatch):
    """Every document hits SessionExpired at once (the cookie died during the
    wait). The portal keeps ONE session per account, so only one document may log
    in — a login per document would kill the session the previous one obtained."""
    failed: set[int] = set()

    async def fake_prepare(gz, ns, params, app_id, app_lot_map, beneficiary, base_url, spec,
                           **kwargs):
        if spec.doc_id not in failed:
            failed.add(spec.doc_id)
            await flow.asyncio.sleep(0)            # let every doc reach its first call
            raise flow.SessionExpired("ci_session died mid-wait")
        async def run(c):
            c.saved.append(spec.doc_id)
        return [_SaveOp(f"doc_{spec.doc_id}", spec.doc_id, run)]
    monkeypatch.setattr(flow, "_prepare_doc", fake_prepare)

    pool = [(_FakeSib(), object()) for _ in range(3)]
    params = _params(doc_plan=[flow.DocSpec(1253, "generate_sign"),
                               flow.DocSpec(3715, "generate_sign"),
                               flow.DocSpec(3335, "bankruptcy")])
    gz = _FakeMainGz()
    ops = await flow._save_parallel_sessions(gz, params, "app", {7: 70}, None, "base", pool)
    assert sum(sib.logins for sib, _ in pool) == 1        # exactly one login for all three
    assert all(sib._http.cookies["ci_session"] == "CI" for sib, _ in pool)
    assert gz._http.cookies["ci_session"] == "CI"
    assert sorted(o.doc_id for o in ops) == [1253, 3335, 3715]


# --- тех.спец pages are committed as they become ready ------------------------------

async def test_lot_discovery_reuses_the_docs_page_on_fallback():
    calls = []
    docs_path = "/ru/application/docs/1/2"

    class _Gz:
        async def _get_page(self, path):
            calls.append(path)
            if path == docs_path:
                return '<tr><td>7</td><td data-app-lot-id="70"></td></tr>'
            return "<html></html>"

    gz = _Gz()
    docs_page_task = flow.asyncio.create_task(gz._get_page(docs_path))
    params = _params(announce_id="1", tender_lot_ids=[7])
    result = await flow._discover_app_lots(gz, params, "2", docs_page_task=docs_page_task)

    assert result == {7: 70}
    assert calls.count(docs_path) == 1


async def test_lot_discovery_detects_a_draft_already_at_price():
    class _Gz:
        async def _get_page(self, path):
            assert path == "/ru/application/show_doc/1/2/3334"
            return '<script>current_method = "priceoffers"</script>'

    params = _params(announce_id="1", tender_lot_ids=[7])
    with pytest.raises(flow.AlreadyAtPriceStage):
        await flow._discover_app_lots(_Gz(), params, "2")


class _LotGz:
    """Serves doc 3334 sub-pages and records the order of every request."""

    def __init__(self, pages):
        self.pages = pages
        self.events: list[tuple] = []

    async def get_show_doc(self, ann, app_id, doc_id, suffix=""):
        if not suffix:
            self.events.append(("barrier", doc_id))
            return ""
        num = int(suffix.rsplit("/", 1)[-1])
        self.events.append(("get", num))
        if num not in self.pages:
            raise RuntimeError("no such page")
        return self.pages[num]

    async def sign_lot_doc(self, ann, app_id, doc_id, app_lot, num, signatures):
        self.events.append(("save", num))


async def test_lot_pages_are_saved_without_waiting_for_slower_pages():
    """Page 1 is small, page 2 carries the multi-MB files. Page 1 must be saved
    (and its barrier fetched) while page 2 is still downloading — the old code
    idled until the slowest page of the probe window was done."""
    pad = "<div>" + "x" * 6000 + "</div>"
    gz = _LotGz({
        1: '<a href="/files/download_file/111/">f</a>' + pad,
        2: '<a href="/files/download_file/222/">f</a>' + pad,
        3: '<form id="form_uploading"><input type="file"></form>' + pad,
    })
    release_big_file = flow.asyncio.Event()
    signed: list[str] = []

    class _Ns:
        async def sign_file(self, url, *, sig_cache=None, referer=None, cache_key=None):
            if "/222/" in url:
                # the big file keeps "downloading" until page 1 has been saved
                await release_big_file.wait()
            signed.append(url)
            return "SIG"

    orig_sign_lot_doc = gz.sign_lot_doc

    async def sign_lot_doc(ann, app_id, doc_id, app_lot, num, signatures):
        await orig_sign_lot_doc(ann, app_id, doc_id, app_lot, num, signatures)
        if num == 1:
            assert not any("/222/" in u for u in signed)   # page 2 still in flight
            release_big_file.set()
    gz.sign_lot_doc = sign_lot_doc

    params = types.SimpleNamespace(announce_id="ann", guarantee_type=3)
    ops = await flow._run_doc_in_session(
        None, gz, _Ns(), params, "app", {7: 70}, None, "https://x",
        flow.DocSpec(3334, "lot_sign"))

    commits = [e for e in gz.events if e[0] in ("save", "barrier")]
    assert commits == [("save", 1), ("barrier", 3334), ("save", 2), ("barrier", 3334)]
    assert [op.label for op in ops] == ["lot_3334_70_1", "lot_3334_70_2"]


async def test_lot_commit_order_survives_prefetching_the_next_lot(monkeypatch):
    next_lot_started = flow.asyncio.Event()
    probes = []

    async def probe(gz, ns, ann, app_id, doc_id, app_lot, num, base_url, **kwargs):
        probes.append((app_lot, num))
        if (app_lot, num) == (80, 1):
            next_lot_started.set()
        return ("file", {app_lot * 10 + num: "sig"}) if num == 1 else ("end", None)

    class _Gz(_RecordingGz):
        async def sign_lot_doc(self, ann, app_id, doc_id, app_lot, num, signatures):
            if app_lot == 70:
                await next_lot_started.wait()
            self.events.append(("save", app_lot, num))

    monkeypatch.setattr(flow, "_probe_lot_page", probe)
    gz = _Gz()
    spec = flow.DocSpec(3334, "lot_sign")
    ops = await flow.asyncio.wait_for(
        flow._run_doc_in_session(
            gz, gz, None, _params(), "app", {7: 70, 8: 80}, None, "base", spec),
        timeout=1.0,
    )
    assert probes.count((80, 1)) == 1
    assert gz.events == [
        ("save", 70, 1), ("barrier", 3334), ("save", 80, 1), ("barrier", 3334),
    ]
    assert [op.label for op in ops] == ["lot_3334_70_1", "lot_3334_80_1"]


# --- targeted self-heal -------------------------------------------------------------

_DOCS_PAGE = (
    "<table>"
    '<tr><td>Приложение 1 (Сведения о бенефициарном собственнике)</td>'
    '<td><a href="/ru/application/show_doc/1/2/3359">Перейти</a></td></tr>'
    '<tr><td>Приложение 15 (Техническая спецификация)</td>'
    '<td><a href="/ru/application/show_doc/1/2/3334">Перейти</a></td></tr>'
    '<tr><td>Приложение 2 (Соглашение об участии)</td>'
    '<td><a href="/ru/application/show_doc/1/2/3715">Перейти</a></td></tr>'
    "</table>"
)
_PLAN = [flow.DocSpec(1253, "generate_sign"), flow.DocSpec(3359, "beneficiary"),
         flow.DocSpec(3334, "lot_sign"), flow.DocSpec(3715, "generate_sign"),
         flow.DocSpec(3335, "bankruptcy"), flow.DocSpec(3328, "deposit")]


def test_reject_message_is_mapped_to_the_named_document():
    titles = flow._doc_titles(_DOCS_PAGE)
    assert set(titles) == {3359, 3334, 3715}
    named = flow._docs_named_in_reject
    # "Приложение 1" must NOT also match "Приложение 15"
    assert named("Подпишите прикрепленный документ в категории Приложение 1 у лота № 5",
                 _PLAN, titles) == {3359}
    assert named("Подпишите ... Приложение 15 (Техническая спецификация) у лота № 5",
                 _PLAN, titles) == {3334}
    assert named("Прикрепите документ Приложение 2", _PLAN, titles) == {3715}
    # no docs page available -> the known appendix numbers still resolve
    assert named("Подпишите ... Приложение 15 ...", _PLAN, None) == {3334}
    assert named("Подпишите ... Приложение 1 ...", _PLAN, {}) == {3359}
    assert named("Подпишите ... Приложение 19 ...", _PLAN, {}) == {3328}
    # nothing recognisable -> empty (caller falls back to the full re-commit)
    assert named("Подпишите документ", _PLAN, titles) == set()
    assert named(None, _PLAN, titles) == set()


async def test_heal_level0_recommits_only_the_named_document(monkeypatch):
    gz = _RecordingGz()
    tech_spec = []

    async def fake_resave_tech_spec(*a, **k):
        tech_spec.append(1)
    monkeypatch.setattr(flow, "_resave_tech_spec", fake_resave_tech_spec)

    ops = [_op("doc_1253", 1253), _op("beneficiary_3359", 3359),
           _op("lot_3334_70_1", 3334), _op("lot_3334_70_2", 3334)]
    params = types.SimpleNamespace(announce_id="ann", doc_plan=_PLAN)
    await flow._heal_rejected_docs(
        gz, None, params, "app", {7: 70}, "base", ops,
        "Подпишите прикрепленный документ в категории Приложение 1 у лота № 5",
        0, flow._doc_titles(_DOCS_PAGE))
    assert gz.events == [("save", "beneficiary_3359"), ("barrier", 3359)]
    assert tech_spec == []                       # тех.спец untouched


async def test_heal_level1_recommits_everything(monkeypatch):
    gz = _RecordingGz()
    tech_spec = []

    async def fake_resave_tech_spec(*a, **k):
        tech_spec.append(1)
    monkeypatch.setattr(flow, "_resave_tech_spec", fake_resave_tech_spec)

    ops = [_op("doc_1253", 1253), _op("beneficiary_3359", 3359),
           _op("lot_3334_70_1", 3334)]
    params = types.SimpleNamespace(announce_id="ann", doc_plan=_PLAN)
    await flow._heal_rejected_docs(
        gz, None, params, "app", {7: 70}, "base", ops,
        "Подпишите ... Приложение 1 ...", 1, {})
    saved = [e[1] for e in gz.events if e[0] == "save"]
    assert saved == ["doc_1253", "beneficiary_3359"]   # every non-lot doc, serially
    assert tech_spec == [1]                             # + the тех.спец redone fresh


async def test_heal_level0_falls_back_to_full_when_document_unknown(monkeypatch):
    gz = _RecordingGz()
    tech_spec = []

    async def fake_resave_tech_spec(*a, **k):
        tech_spec.append(1)
    monkeypatch.setattr(flow, "_resave_tech_spec", fake_resave_tech_spec)

    ops = [_op("doc_1253", 1253)]
    params = types.SimpleNamespace(announce_id="ann", doc_plan=_PLAN)
    await flow._heal_rejected_docs(
        gz, None, params, "app", {7: 70}, "base", ops, "Подпишите документ", 0, {})
    assert ("save", "doc_1253") in gz.events
    assert tech_spec == [1]


# --- per-phase timing ---------------------------------------------------------------

def test_timing_breakdown_separates_waiting_from_submission():
    ts = {"start": 100.0, "t0": 130.0, "created": 130.8, "lots_done": 132.0,
          "docs_signed": 138.5, "docs_accepted": 139.5, "gamma_start": 139.5,
          "gamma_done": 143.0, "signed": 143.0, "public_start": 143.0,
          "public_done": 143.9}
    tb = flow.timing_breakdown(ts)
    assert tb["wait"] == 30.0                      # waited 30s for the tender to open
    assert tb["from_open"] == 13.9                 # ...which is NOT part of the bid time
    assert tb["create_lots"] == 2.0
    assert tb["docs"] == 7.5
    assert tb["price"] == 3.5
    assert tb["submit"] == 0.9
    assert tb["to_publish_request"] == 13.0
    assert tb["publish_request"] == 0.9


def test_timing_breakdown_tolerates_partial_and_dry_runs():
    assert flow.timing_breakdown({}) == {}
    assert flow.timing_breakdown(None) == {}
    dry = flow.timing_breakdown({"start": 0.0, "t0": 1.0, "lots_done": 3.0,
                                 "docs_accepted": 8.0, "gamma_done": 11.0,
                                 "signed": 11.0, "dry_run_complete": 11.0})
    assert dry["from_open"] == 10.0
    assert "submit" not in dry                     # nothing was submitted
    assert "to_publish_request" not in dry


# --- experimental lot_barrier switch -------------------------------------------------

@pytest.mark.parametrize("mode, expected", [
    ("each", [("save", 1), ("barrier", 3334), ("save", 2), ("barrier", 3334)]),
    ("last", [("save", 1), ("save", 2), ("barrier", 3334)]),
    ("none", [("save", 1), ("save", 2)]),
    ("garbage", [("save", 1), ("barrier", 3334), ("save", 2), ("barrier", 3334)]),
])
async def test_lot_barrier_modes(mode, expected):
    """Default stays the proven save->barrier->save->barrier; "last"/"none" are an
    opt-in experiment; an unknown value falls back to the proven order."""
    pad = "<div>" + "x" * 6000 + "</div>"
    gz = _LotGz({
        1: '<a href="/files/download_file/111/">f</a>' + pad,
        2: '<a href="/files/download_file/222/">f</a>' + pad,
        3: '<form id="form_uploading"><input type="file"></form>' + pad,
    })

    class _Ns:
        async def sign_file(self, url, *, sig_cache=None, referer=None, cache_key=None):
            return "SIG"

    params = types.SimpleNamespace(announce_id="ann", guarantee_type=3, lot_barrier=mode)
    await flow._run_doc_in_session(
        None, gz, _Ns(), params, "app", {7: 70}, None, "https://x",
        flow.DocSpec(3334, "lot_sign"))
    assert [e for e in gz.events if e[0] in ("save", "barrier")] == expected
